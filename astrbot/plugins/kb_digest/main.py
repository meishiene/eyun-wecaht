import asyncio
import json
import re
import shutil
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from . import kb_core as kb
    from . import nl_intent as nl
except ImportError:  # 本地测试
    import kb_core as kb
    import nl_intent as nl

PLACEHOLDER = "[图片]"
ROOT = Path("data/kb")

# 白话入口：以 @11 开头的消息；以及“是/取消”这类对确认的回复（没有待确认时不处理，照常当素材）
_TALK_RE = (r"(?i)^\s*(?:[@＠]\s*" + re.escape(nl.WAKE_NAME) + r"(?!\d)|(?:"
            + "|".join(re.escape(w) for w in sorted(nl.YES | nl.NO, key=len, reverse=True)) + r")[\s。.!！~]*$)")


class _NLEvent:
    """把白话翻译出来的指令正文当作 message_str，其余全部透传给真实事件，这样原有指令函数不用改。"""
    def __init__(self, ev, text: str):
        self._ev = ev
        self.message_str = text

    def __getattr__(self, name):
        return getattr(self._ev, name)

IMG_PROMPT = ("这是一张中转站（大模型 API 转发）业务相关的微信群聊图片。请用中文说明：1）图中文字（尽量完整转录，"
              "保留报错、状态码、rid、模型名、数字）；2）画面内容概述。不超过 250 字。")

EXTRACT_PROMPT = """你是「大模型中转站」客服知识库的整理员。下面是一次从微信群复制/粘贴来的聊天内容，只对应【一个问题】。
请严格输出一个 JSON 对象（不要任何多余文字、不要代码块），字段如下：
{{
 "title": "问题标题，20字内，概括现象",
 "category": "只能从下面分类树里选一个【细类编号】，如 4.6；实在放不进去填 0",
 "new_cat": null 或 {{"name":"建议新增的细类名","major":"所属大类编号","reason":"为什么放不进现有细类"}},
 "status": "已解决|未解决|待跟进|需人工核验 之一",
 "source": "官方文档|后台核验|经验判断|知识库提炼|待验证 之一（依据是谁给出的结论）",
 "tags": ["最多 12 个，必须优先从下面【标签词表】里原样挑规范名（可多类多选）；词表里确实没有、但对检索有价值的才新写（新写的会进待确认，不要硬凑）"],
 "brief": "简要：1~3句，说清问题是什么、结论是什么",
 "phenomenon": "现象",
 "elements": {{"status_code":"","message":"报错最后一句","status_field":"","rid":"","model_path_group":"模型/接口路径/分组"}},
 "cause": "原因（原文没说就写：原文未给出）",
{new_fields},
 "solution": "解决方案",
 "reply_script": "可直接发给客户的答复口径；原文没有就根据结论拟一段，不要编造事实",
 "retry_helps": "有用|无效|视情况|未说明",
 "need_verify": "是|否",
 "verify_materials": "若需人工核验，写要收集哪些材料，否则空",
 "impact": "影响范围",
 "evidence": "关键依据出自原文哪几句（简短引用）"
}}
规则：1）不得编造原文没有的事实，不确定就写“原文未提及”；2）输出里不要出现完整的 API Key、手机号、邮箱，需要时用 **** 代替；
3）内容里的 [图片k：…] 是第k张图片的识别结果。
4）{boundary}

分类树：
{tree}

标签词表（每类一行）：
{tags}

聊天内容：
{content}
"""

DEDUPE_PROMPT = """判断【新问题】与下面【已有问题】是否是同一个问题（同一现象、同一原因，只是又被问了一次或补充了信息）。
只输出 JSON：{{"same": "已有问题编号或null", "similar": ["相似但不确定是同一个的编号"]}}。
只有非常确定是同一个问题才填 same；拿不准的放 similar；都不是就都留空。

新问题：{title}｜{brief}

已有问题：
{cands}
"""

MERGE_PROMPT = """下面是知识库里已有的一个问题（JSON）和同一问题的新补充信息（JSON）。
请合并成一份更完整的报告，只输出一个 JSON 对象，字段和已有问题完全一致（title、category、status、source、tags、brief、phenomenon、elements、cause、principle、owner_side、routine、solution、reply_script、retry_helps、need_verify、verify_materials、impact、evidence）。
规则：保留已有的有效信息，用新信息补充或更正；不得编造；category 保持不变。

已有：
{old}

新补充：
{new}
"""


@register("kb_digest", "local", "中转站问答知识库", "1.0.0")
class KbDigest(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        ROOT.mkdir(parents=True, exist_ok=True)
        self.tree = kb.load_tree(ROOT)
        self.lock = asyncio.Lock()
        self.buffers: dict[str, list] = {}
        self.nl_pending: dict[str, dict] = {}      # 等用户回“是”的白话指令：sid -> {"it":…, "at":…}
        self._load_buffers()

    # ---------- 缓冲区（落盘，重启不丢） ----------
    @staticmethod
    def _sk(sid: str) -> str:
        return re.sub(r"[^0-9A-Za-z_@.\-]+", "_", sid)

    def _bdir(self, sid: str) -> Path:
        return ROOT / ".buffers" / self._sk(sid)

    def _save_buf(self, sid: str):
        d = self._bdir(sid)
        d.mkdir(parents=True, exist_ok=True)
        items = [[k, v if k == "text" else Path(v).name] for k, v in self.buffers.get(sid, [])]
        (d / "state.json").write_text(json.dumps({"sid": sid, "items": items}, ensure_ascii=False), encoding="utf-8")

    def _load_buffers(self):
        base = ROOT / ".buffers"
        if not base.exists():
            return
        for st in base.glob("*/state.json"):
            try:
                d = json.loads(st.read_text(encoding="utf-8"))
                self.buffers[d["sid"]] = [(k, v if k == "text" else st.parent / v) for k, v in d["items"]]
            except Exception as e:
                logger.error(f"[kb] 读取缓冲区失败 {st}: {e}")

    def _drop_buf(self, sid: str):
        self.buffers.pop(sid, None)
        shutil.rmtree(self._bdir(sid), ignore_errors=True)

    @staticmethod
    def voc():
        """每次现读词表（文件很小），这样服务器上手改词表也立刻生效。"""
        return kb.load_vocab(ROOT, create=True)

    @staticmethod
    def _bot(event: AstrMessageEvent) -> str:
        try:
            return event.get_platform_id()
        except Exception:
            return event.unified_msg_origin.split(":")[0]

    @staticmethod
    def _args(event: AstrMessageEvent) -> list:
        parts = (event.message_str or "").strip().split(None, 1)
        return parts[1].split() if len(parts) > 1 else []

    @staticmethod
    def _rest(event: AstrMessageEvent) -> str:
        parts = (event.message_str or "").strip().split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    # ---------- LLM ----------
    async def _llm(self, provider, prompt: str, images=None) -> str:
        r = await provider.text_chat(prompt=prompt, session_id=None, image_urls=images)
        return (r.completion_text or "").strip()

    async def _llm_json(self, provider, prompt: str):
        for _ in range(2):
            obj = kb.parse_json(await self._llm(provider, prompt))
            if isinstance(obj, dict):
                return obj
        return None

    async def _describe(self, provider, path: Path, sem):
        async with sem:
            try:
                t = await self._llm(provider, IMG_PROMPT, [str(Path(path).resolve())])
                return t.replace("\n", " ") or "（无识别结果）"
            except Exception as e:
                logger.error(f"[kb] 识图失败 {path}: {e}")
                return "（识别失败）"

    # ---------- 收集 ----------
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def collect(self, event: AstrMessageEvent):
        sid = event.unified_msg_origin
        got = False
        for comp in event.get_messages():
            name = type(comp).__name__
            if name == "Plain":
                t = comp.text
                if nl.strip_wake(t)[0] or (self._live_pending(sid) and nl.confirm_answer(t) is not None):
                    continue                      # 白话指令或对确认的回复，不是素材
                if t.strip() and not t.strip().startswith("/"):
                    self.buffers.setdefault(sid, []).append(("text", t))
                    got = True
            elif name == "Image":
                try:
                    src = await comp.convert_to_file_path()
                    d = self._bdir(sid)
                    d.mkdir(parents=True, exist_ok=True)
                    dst = d / f"img_{int(time.time() * 1000)}{Path(src).suffix or '.jpg'}"
                    shutil.copy(src, dst)
                    self.buffers.setdefault(sid, []).append(("img", dst))
                    got = True
                except Exception as e:
                    logger.error(f"[kb] 保存图片失败: {e}")
            elif name == "File":
                logger.info(f"[kb] 暂不处理文件: {getattr(comp, 'name', '')}")
            else:
                logger.info(f"[kb] 未处理的消息组件: {name}"[:300])
        if got:
            self._save_buf(sid)
            event.stop_event()

    @filter.command("条数")
    async def count(self, event: AstrMessageEvent):
        """查看已收集（还没整理）的条数"""
        items = self.buffers.get(event.unified_msg_origin, [])
        t = sum(1 for k, _ in items if k == "text")
        i = len(items) - t
        ph = sum(v.count(PLACEHOLDER) for k, v in items if k == "text")
        tip = f"；文字里有 {ph} 个[图片]占位符，已收到 {i} 张图" if ph or i else ""
        pend = len(kb.list_pending(ROOT))
        yield event.plain_result(f"已收集文字 {t} 段、图片 {i} 张{tip}。待入库 {pend} 个问题。\n"
                                 f"发 /整理 [标题] 把这批整理成一个问题。")

    @filter.command("清空")
    async def clear(self, event: AstrMessageEvent):
        """丢弃已收集、还没整理的内容"""
        sid = event.unified_msg_origin
        n = len(self.buffers.get(sid, []))
        self._drop_buf(sid)
        yield event.plain_result(f"已清空 {n} 条（不影响已整理的问题）。")

    # ---------- 整理 ----------
    @filter.command("整理")
    async def digest(self, event: AstrMessageEvent, title: str = ""):
        """把已收集的内容整理成一个待入库问题：/整理 [标题]"""
        sid = event.unified_msg_origin
        items = list(self.buffers.get(sid, []))
        if not items:
            yield event.plain_result("还没有收集到内容，先把消息粘贴/图片发给我。")
            return
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("未配置大模型提供商。")
            return
        user_title = self._rest(event)
        texts = [v for k, v in items if k == "text"]
        imgs = [v for k, v in items if k == "img"]
        yield event.plain_result(f"收到文字 {len(texts)} 段、图片 {len(imgs)} 张，整理中（约半分钟）…")

        descs = []
        if imgs:
            sem = asyncio.Semaphore(3)
            descs = await asyncio.gather(*[self._describe(provider, p, sem) for p in imgs])
        queue = list(range(len(imgs)))

        def fill(_m):
            if not queue:
                return PLACEHOLDER
            k = queue.pop(0)
            return f"[图片{k + 1}：{descs[k]}]"

        body = "\n".join(re.sub(re.escape(PLACEHOLDER), fill, t) for t in texts)
        if queue:
            body += "\n\n【未对应到占位符的图片】\n" + "\n".join(f"[图片{k + 1}：{descs[k]}]" for k in queue)
        if user_title:
            body = f"（用户指定标题：{user_title}）\n" + body

        try:
            vocab = self.voc()
            data = await self._llm_json(provider, EXTRACT_PROMPT.format(
                tree=kb.tree_text(self.tree), tags=vocab.prompt_text(), content=body,
                new_fields=kb.NEW_FIELDS_SCHEMA, boundary=kb.BOUNDARY_RULE))
        except Exception as e:
            logger.error(f"[kb] 整理失败: {e}")
            yield event.plain_result(f"大模型调用失败：{e}\n已收集的内容保留，可稍后重试 /整理。")
            return
        if not data:
            yield event.plain_result("大模型返回的格式无法解析，已收集的内容保留，可重试 /整理。")
            return

        cat = str(data.get("category", "0")).strip()
        _, sub = kb.find_cat(self.tree, cat)
        if sub is None:
            cat = "0"
        status = data.get("status") if data.get("status") in kb.STATUSES else "待跟进"
        source = data.get("source") if data.get("source") in kb.SOURCES else "待验证"
        title = (user_title or str(data.get("title") or "未命名")).strip()[:40]

        merge_into, suspect = None, []
        try:
            cands = [m for m in kb.list_problems(ROOT, cat) + kb.list_pending(ROOT) if m.get("category") == cat][:40]
            if cands and cat != "0":
                lines = "\n".join(f"{m['qid']}｜{m['title']}｜{m.get('brief', '')[:80]}" for m in cands)
                dd = await self._llm_json(provider, DEDUPE_PROMPT.format(
                    title=title, brief=data.get("brief", ""), cands=lines))
                ok = {m["qid"] for m in cands}
                if dd:
                    if dd.get("same") in ok:
                        merge_into = dd["same"]
                    suspect = [q for q in (dd.get("similar") or []) if q in ok and q != merge_into]
        except Exception as e:
            logger.error(f"[kb] 查重失败（忽略，按新问题处理）: {e}")

        async with self.lock:
            qid = kb.next_qid(ROOT)
            d = kb.pending_dir(ROOT) / f"{qid}-{kb.slug(title)}"
            d.mkdir(parents=True, exist_ok=True)
            kb.write_raw(d / "原文", items, self._bot(event), sid)
            if descs:
                (d / "识图结果.md").write_text(
                    "\n\n".join(f"## 图片{k + 1}\n{t}" for k, t in enumerate(descs)), encoding="utf-8")
            new_cat = data.get("new_cat") if cat == "0" and isinstance(data.get("new_cat"), dict) else None
            tags, new_tags = vocab.normalize(data.get("tags") or [])
            data["tags"] = tags
            kb.add_pending_tags(ROOT, qid, new_tags)
            meta = {"qid": qid, "title": title, "category": cat, "status": status, "source": source,
                    "tags": tags,
                    "brief": kb.redact(str(data.get("brief", ""))), "related": suspect[:],
                    "suspect_dup": suspect, "merge_into": merge_into, "new_cat": new_cat,
                    "bot": self._bot(event), "sid": sid, "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
            data["title"] = title
            kb.apply_fields(meta, data, self.tree, cat)          # 原理说明、归属侧、常态、关联分类
            (d / "报告.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            (d / "简要.md").write_text(meta["brief"] + "\n", encoding="utf-8")
            (d / "报告.md").write_text(kb.render_report(data, meta), encoding="utf-8")
            kb.write_meta(d, meta)
            self._drop_buf(sid)

        _, s = kb.find_cat(self.tree, cat)
        catname = f"{cat} {s['name']}" if s else "0 待分类"
        out = [f"✅ 已整理为待入库：{qid}", f"标题：{title}", f"建议归类：{catname}", f"状态：{status}｜口径：{source}",
               f"归属侧：{meta.get('owner_side', '待判断')}｜类型：{meta.get('routine', '待判断')}",
               f"简要：{meta['brief']}"]
        if meta.get("related_cats"):
            out.append("🔗 同时牵涉：" + "、".join(self._cat_name(c) for c in meta["related_cats"]))
        if meta.get("routine") == "非常态":
            out.append(kb.SUPPORT_HINT)
        if merge_into:
            out.append(f"🔁 判定为已有问题 {merge_into} 的补充，入库时会合并进去。")
        elif suspect:
            out.append(f"⚠️ 疑似重复：{'、'.join(suspect)}（已标记，入库为新问题，待你确认）")
        if new_cat:
            out.append(f"💡 放不进现有细类，建议新增：{new_cat.get('name')}（{new_cat.get('reason', '')}）")
        out.append(f"标签：{kb.tags_text(vocab, tags)}")
        if new_tags:
            out.append(f"🏷 词表里没有：{'、'.join(new_tags)}（已记入待确认，/同意新标签 查看）")
        out.append(f"\n归类不对用 /改类 {qid} 细类编号；没问题发 /入库。")
        yield event.plain_result("\n".join(out))

    # ---------- 入库 ----------
    @filter.command("入库")
    async def commit(self, event: AstrMessageEvent):
        """把所有待入库问题归档进知识库并 git 提交"""
        provider = self.context.get_using_provider()
        done, notes = [], []
        pend = []
        async with self.lock:
            pend = kb.list_pending(ROOT)
            for m in pend:
                try:
                    tgt = kb.find_problem(ROOT, m["merge_into"]) if m.get("merge_into") else None
                    if tgt and tgt["_dir"].parent != kb.pending_dir(ROOT):
                        await self._merge_into(provider, m, tgt)
                        done.append(f"🔁 {m['qid']} → 合并进 {tgt['qid']}")
                    else:
                        dst = kb.commit_pending_item(ROOT, self.tree, m)
                        s = kb.find_cat(self.tree, m["category"])[1]
                        done.append(f"📥 {m['qid']} {m['title']} → {m['category']} {s['name'] if s else '待分类'}")
                except Exception as e:
                    logger.error(f"[kb] 入库失败 {m['qid']}: {e}")
                    notes.append(f"❌ {m['qid']} 入库失败：{e}")
            h = "无变更"
            if done:
                try:
                    h = kb.git_commit(ROOT, f"入库 {len(done)} 条: " + "; ".join(x.split(' ', 2)[1] for x in done))
                except Exception as e:
                    h = f"git 提交失败：{e}"
        if not pend:
            yield event.plain_result("没有待入库的问题。")
            return
        yield event.plain_result(f"入库完成（版本 {h}）：\n" + "\n".join(done + notes))

    async def _merge_into(self, provider, m: dict, tgt: dict):
        """把待入库问题 m 合并进已入库问题 tgt：原文追加；报告用大模型合并，失败则追加一节。"""
        pd, td = m["_dir"], tgt["_dir"]
        kb.merge_raw(pd / "原文", td / "原文")
        newj = json.loads((pd / "报告.json").read_text(encoding="utf-8"))
        oldp = td / "报告.json"
        oldj = json.loads(oldp.read_text(encoding="utf-8")) if oldp.exists() else {}
        meta = kb.read_meta(td)
        merged = None
        if provider and oldj:
            try:
                merged = await self._llm_json(provider, MERGE_PROMPT.format(
                    old=json.dumps(oldj, ensure_ascii=False), new=json.dumps(newj, ensure_ascii=False)))
            except Exception as e:
                logger.error(f"[kb] 合并报告失败，改为追加: {e}")
        if merged:
            merged["title"] = meta["title"]
            meta["brief"] = kb.redact(str(merged.get("brief", meta.get("brief", ""))))
            if merged.get("status") in kb.STATUSES:
                meta["status"] = merged["status"]
            meta["tags"], _ = self.voc().normalize([*meta.get("tags", []), *m.get("tags", [])])
            merged["tags"] = meta["tags"]
            for k in ("principle", "owner_side", "routine"):      # 大模型漏写时保留旧值，不让合并把它们丢了
                if not merged.get(k) and oldj.get(k):
                    merged[k] = oldj[k]
            kb.apply_fields(meta, merged, self.tree, meta["category"])
            kb.retarget_pending_tags(ROOT, m["qid"], meta["qid"])
            oldp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
            (td / "简要.md").write_text(meta["brief"] + "\n", encoding="utf-8")
            (td / "报告.md").write_text(kb.render_report(merged, meta), encoding="utf-8")
        else:
            add = f"\n\n## 追加记录（{time.strftime('%Y-%m-%d %H:%M')}，来自 {m['qid']}）\n" + kb.redact(
                (pd / "报告.md").read_text(encoding="utf-8"))
            with open(td / "报告.md", "a", encoding="utf-8") as f:
                f.write(add)
        meta.setdefault("merged_from", []).append(m["qid"])
        kb.write_meta(td, meta)
        shutil.rmtree(pd)

    # ---------- 维护 ----------
    @filter.command("待入库")
    async def pending(self, event: AstrMessageEvent):
        """列出待入库的问题"""
        pend = kb.list_pending(ROOT)
        if not pend:
            yield event.plain_result("没有待入库的问题。")
            return
        lines = []
        for m in pend:
            s = kb.find_cat(self.tree, m["category"])[1]
            flag = f" 🔁合并→{m['merge_into']}" if m.get("merge_into") else (" ⚠️疑似重复" if m.get("suspect_dup") else "")
            lines.append(f"{m['qid']}｜{m['title']}｜{m['category']} {s['name'] if s else '待分类'}{flag}")
        yield event.plain_result(f"待入库 {len(pend)} 个：\n" + "\n".join(lines))

    @filter.command("改类")
    async def recat(self, event: AstrMessageEvent, qid: str = "", code: str = ""):
        """修改归类：/改类 Q编号 细类编号"""
        if not qid or not code:
            yield event.plain_result("用法：/改类 编号 细类编号，例如 /改类 41 4.1\n细类编号见 /树。编号可以只写序号。")
            return
        async with self.lock:
            msg = None
            m, err = kb.resolve_qid(ROOT, qid)
            if not m:
                msg = err
            else:
                try:
                    kb.move_problem(ROOT, self.tree, m["qid"], code)
                except ValueError as e:
                    msg = str(e)
                if msg is None:
                    if not kb.is_pending(ROOT, m):
                        await self._commit_git(f"改类 {m['qid']} → {code}")
                    s_ = kb.find_cat(self.tree, code)[1]
                    msg = f"已把 {m['qid']} 归到 {code} {s_['name'] if s_ else '待分类'}。"
        yield event.plain_result(msg)

    async def _commit_git(self, msg: str):
        try:
            return kb.git_commit(ROOT, msg)
        except Exception as e:
            logger.error(f"[kb] git 提交失败: {e}")
            return f"git 提交失败：{e}"

    # ---------- 阶段 3：检索与维护 ----------
    @staticmethod
    def _one_line(m: dict, n: int = 46) -> str:
        b = (m.get("brief") or "").replace("\n", " ")
        return b[:n] + ("…" if len(b) > n else "")

    def _cat_name(self, code: str) -> str:
        mj, sub = kb.find_cat(self.tree, code)
        return f"{code} {(sub or mj)['name']}" if mj else str(code)

    def _hit_lines(self, hits) -> list:
        out = []
        for _, m in hits:
            flag = ("【待入库】" if kb.is_pending(ROOT, m) else "") + ("【非常态】" if m.get("routine") == "非常态" else "")
            out.append(f"{m['qid']}｜{m.get('category', '0')}｜{m.get('status', '')}｜{flag}{m['title']}\n    {self._one_line(m)}")
        return out

    @filter.command("查")
    async def search(self, event: AstrMessageEvent):
        """搜索知识库：/查 关键词 [#标签]"""
        q = self._rest(event)
        if not q:
            yield event.plain_result("用法：/查 关键词（多个词都要命中）\n过滤标签：/查 限流 #号池\n只看标签：/查 #gemini（写别名也行，厂商包含它的模型）\n限定类：/查 #渠道:Azure（类名可简写）。标签清单见 /标签")
            return
        total, hits = kb.search(ROOT, q, limit=8, vocab=self.voc())
        if not total:
            yield event.plain_result(f"没有找到「{q}」。换个说法，或用 /树 按分类找。")
            return
        lines = [f"「{q}」共 {total} 条" + ("，显示前 8 条" if total > 8 else "")] + self._hit_lines(hits)
        lines.append("\n看详情：/看 编号（序号也行，如 /看 41）")
        if total > 8:
            lines[-1] += "；结果太多可加关键词缩小"
        yield event.plain_result("\n".join(lines))

    @filter.command("看")
    async def show(self, event: AstrMessageEvent):
        """查看某个问题：/看 编号"""
        arg = self._rest(event)
        m, err = kb.resolve_qid(ROOT, arg)
        if not m:
            yield event.plain_result(err if arg else "用法：/看 编号，例如 /看 Q20261102-041 或 /看 41")
            return
        s_ = kb.find_cat(self.tree, m.get("category", "0"))[1]
        d = m["_dir"]
        raws = len(list((d / "原文").glob("原文-*.md"))) if (d / "原文").exists() else 0
        lines = [f"{m['qid']}｜{m.get('status', '')}｜口径：{m.get('source', '')}" + ("｜【待入库】" if kb.is_pending(ROOT, m) else ""),
                 f"标题：{m['title']}",
                 f"归类：{m.get('category', '0')} {s_['name'] if s_ else '待分类'}",
                 f"标签：{kb.tags_text(self.voc(), m.get('tags', []))}",
                 f"归属侧：{m.get('owner_side') or '待判断'}｜类型：{m.get('routine') or '待判断'}｜审核：{kb.audit_label(m)}",
                 f"简要：{m.get('brief', '')}", f"原文批次：{raws}"]
        if m.get("related_cats"):
            lines.append("🔗 同时牵涉：" + "、".join(self._cat_name(c) for c in m["related_cats"]))
        if m.get("routine") == "非常态":
            lines.append(kb.SUPPORT_HINT)
        if m.get("ai_filled"):
            lines.append("⚠️ 原理说明、归属侧、类型由 AI 补全，待审核：/审 编号 分数 [校正说明]")
        if m.get("suspect_dup"):
            lines.append("⚠️ 疑似重复：" + "、".join(m["suspect_dup"]))
        if m.get("merge_into"):
            lines.append(f"🔁 入库时将合并进：{m['merge_into']}")
        if m.get("new_cat"):
            lines.append(f"💡 建议新增细类：{m['new_cat'].get('name')}（/同意新类 {m['new_cat'].get('name')}）")
        if m.get("merged_from"):
            lines.append("已合并：" + "、".join(m["merged_from"]))
        yield event.plain_result("\n".join(lines))
        rp = (d / "报告.md").resolve()
        if rp.exists():
            yield event.chain_result([Comp.File(file=str(rp), name=f"{m['qid']}-报告.md")])

    @filter.command("树")
    async def show_tree(self, event: AstrMessageEvent):
        """显示分类树和各类问题数"""
        yield event.plain_result(kb.tree_summary(ROOT, self.tree))

    @filter.command("状态")
    async def set_status(self, event: AstrMessageEvent, qid: str = "", status: str = ""):
        """修改问题状态：/状态 编号 已解决|未解决|待跟进|需人工核验"""
        if not qid or not status:
            yield event.plain_result("用法：/状态 编号 状态\n状态可选：" + "、".join(kb.STATUSES) + "\n（看已收集条数请用 /条数）")
            return
        async with self.lock:
            msg = None
            m, err = kb.resolve_qid(ROOT, qid)
            if not m:
                msg = err
            else:
                try:
                    old = kb.set_status(ROOT, m["qid"], status)
                    if not kb.is_pending(ROOT, m):
                        await self._commit_git(f"状态 {m['qid']}：{old} → {status}")
                    msg = f"{m['qid']} 状态：{old} → {status}"
                except ValueError as e:
                    msg = str(e)
        yield event.plain_result(msg)

    @filter.command("审")
    async def audit(self, event: AstrMessageEvent):
        """人工审核：/审 编号 分数(1~5) [校正说明]；只写编号则看审核情况"""
        parts = self._rest(event).split(None, 2)
        if not parts:
            yield event.plain_result(
                f"用法：/审 编号 分数 [校正说明]\n分数 1~5，{kb.AUDIT_PASS} 分及以上算可用，更低算需校正。\n"
                "写了校正说明就存成“基准回答”（AI 原稿保留作对照）。只写编号可看这个问题的审核情况；待审清单：/待审")
            return
        async with self.lock:
            msg = None
            m, err = kb.resolve_qid(ROOT, parts[0])
            if not m:
                msg = err
            elif len(parts) == 1:
                msg = f"{m['qid']}《{m['title']}》审核：{kb.audit_label(m)}" + (
                    f"\n校正说明：{m['audit_note']}" if m.get("audit_note") else "") + (
                    "\n⚠️ AI 补全的内容还没人审过" if m.get("ai_filled") else "")
            else:
                try:
                    old = kb.set_audit(ROOT, m["qid"], parts[1], parts[2] if len(parts) > 2 else "")
                    new = kb.audit_label(kb.read_meta(m["_dir"]))
                    if not kb.is_pending(ROOT, m):
                        await self._commit_git(f"审核 {m['qid']}：{new}")
                    msg = f"{m['qid']} 审核：{old} → {new}" + ("，校正说明已存为基准回答（AI 原稿保留）。" if len(parts) > 2 else "。")
                except ValueError as e:
                    msg = str(e)
        yield event.plain_result(msg)

    @filter.command("待审")
    async def to_review(self, event: AstrMessageEvent):
        """待审核清单：AI 补全还没人看过的，和审核打了低分需要校正的"""
        items = [m for m in kb.list_problems(ROOT) if kb.needs_review(m)]
        if not items:
            yield event.plain_result("没有待审核的问题。")
            return
        ai = sum(1 for m in items if m.get("ai_filled"))
        lines = [f"待审核 {len(items)} 个（AI 补全未审 {ai}，低分需校正 {len(items) - ai}），显示前 15 个："]
        for m in items[:15]:
            lines.append(f"{m['qid']}｜{'AI补全未审' if m.get('ai_filled') else kb.audit_label(m)}｜{m['title']}")
        lines.append("\n审核：/审 编号 分数 [校正说明]")
        yield event.plain_result("\n".join(lines))

    @filter.command("标签")
    async def tags(self, event: AstrMessageEvent):
        """标签清单：/标签 看各类数量；/标签 厂商 看这一类下的全部标签；/标签 树 看 厂商→模型→错误码"""
        v = self.voc()
        stats = kb.tag_stats(ROOT, v)
        a = self._rest(event)
        if a == "树":
            yield event.plain_result(kb.tag_hierarchy_text(kb.tag_hierarchy(ROOT, v)))
            return
        pend = kb.load_pending_tags(ROOT)
        if not a:
            lines = ["标签分类（类：标签数 / 问题涉及次数）："]
            for g in [*v.groups, kb.UNGROUPED]:
                if g in stats:
                    lines.append(f"- {g}：{len(stats[g])} 个 / {sum(stats[g].values())} 次")
            if pend:
                lines.append(f"- 待确认新标签：{len(pend)} 个（/同意新标签）")
            lines.append("\n看某一类：/标签 厂商　　看层级：/标签 树　　查问题：/查 #标签名")
            yield event.plain_result("\n".join(lines))
            return
        g = v.group_match(a) or (kb.UNGROUPED if a == kb.UNGROUPED else None)
        if not g:
            yield event.plain_result("类要写：" + "、".join(v.groups))
            return
        cnt = stats.get(g, {})
        if g == "模型":
            by = {}
            for n, c in cnt.items():
                by.setdefault(v.vendor_of(n) or "（无厂商）", []).append((n, c))
            lines = [f"【模型】共 {len(cnt)} 个"]
            for vend, items in sorted(by.items(), key=lambda x: -sum(c for _, c in x[1])):
                lines.append(f"{vend}：" + "、".join(f"{n}({c})" for n, c in sorted(items, key=lambda x: -x[1])))
        else:
            ordered = sorted(cnt.items(), key=lambda x: (-x[1], x[0]))
            lines = [f"【{g}】共 {len(cnt)} 个", "、".join(f"{n}({c})" for n, c in ordered) or "（暂无问题用到）"]
        yield event.plain_result("\n".join(lines))

    @filter.command("同意新标签")
    async def approve_tag(self, event: AstrMessageEvent):
        """确认新标签：/同意新标签 名称 类 [厂商]"""
        v = self.voc()
        toks = self._args(event)
        pend = kb.load_pending_tags(ROOT)
        if not toks:
            if not pend:
                yield event.plain_result("目前没有待确认的新标签。")
                return
            lines = ["待确认的新标签："]
            for n, x in pend.items():
                lines.append(f"- {n}（{len(x['qids'])} 个问题：{'、'.join(x['qids'][:5])}{'…' if len(x['qids']) > 5 else ''}）")
            lines.append("\n同意请发：/同意新标签 名称 类　类可选：" + "、".join(v.groups)
                         + "\n模型可再加厂商：/同意新标签 Claude 4.8 模型 Anthropic"
                         + "\n其实是已有标签的另一种写法：/加别名 规范名 这个写法")
            yield event.plain_result("\n".join(lines))
            return
        name, grp, vendor = kb.split_group_args(v, toks)
        if not grp:
            yield event.plain_result("请带上类：/同意新标签 名称 类　类可选：" + "、".join(v.groups))
            return
        async with self.lock:
            try:
                c, done = kb.approve_new_tag(ROOT, v, name, grp, vendor)
                h = await self._commit_git(f"新增标签 {grp}：{c}")
                msg = f"已把「{c}」加入「{grp}」，补到 {len(done)} 个问题" + (f"：{'、'.join(done)}" if done else "") + f"。（版本 {h}）"
            except ValueError as e:
                msg = str(e)
        yield event.plain_result(msg)

    @filter.command("加别名")
    async def add_alias(self, event: AstrMessageEvent):
        """给标签补别名：/加别名 规范名 别名"""
        v = self.voc()
        name, alias = kb.split_alias_args(v, self._args(event))
        if not name or not alias:
            yield event.plain_result("用法：/加别名 规范名 别名，例如 /加别名 Gemini 3.1 Pro g3p\n规范名见 /标签。补完后，之后整理出来的这个写法会自动归到规范名。")
            return
        async with self.lock:
            try:
                c, done = kb.add_alias(ROOT, v, name, alias)
                h = await self._commit_git(f"标签别名 {c} ← {alias}")
                msg = f"已给「{c}」加别名「{alias}」" + (f"，并补到待确认里的 {len(done)} 个问题：{'、'.join(done)}" if done else "") + f"。（版本 {h}）"
            except ValueError as e:
                msg = str(e)
        yield event.plain_result(msg)

    @filter.command("同意新类")
    async def approve_cat(self, event: AstrMessageEvent):
        """确认新增细类：/同意新类 名称 [大类编号]"""
        a = self._args(event)
        if not a:
            sug = kb.new_cat_suggestions(ROOT)
            if not sug:
                yield event.plain_result("目前没有待确认的新增细类建议。")
                return
            lines = ["待确认的新增细类建议："]
            for x in sug:
                lines.append(f"- {x['name']}（建议放在大类 {x['major'] or '?'}）：{x['reason']}\n  涉及 {len(x['qids'])} 个问题：{'、'.join(x['qids'])}")
            lines.append("\n同意请发：/同意新类 名称；想放别的大类：/同意新类 名称 大类编号")
            yield event.plain_result("\n".join(lines))
            return
        async with self.lock:
            try:
                code, moved = kb.approve_new_cat(ROOT, self.tree, a[0], a[1] if len(a) > 1 else None)
                h = await self._commit_git(f"新增细类 {code} {a[0]}")
                msg = f"已新增细类 {code} {a[0]}，移入 {len(moved)} 个问题" + (f"：{'、'.join(moved)}" if moved else "") + f"。（版本 {h}）"
            except ValueError as e:
                msg = str(e)
        yield event.plain_result(msg)

    @filter.command("合并")
    async def merge(self, event: AstrMessageEvent, src: str = "", dst: str = ""):
        """把问题 A 合并进已入库的问题 B：/合并 A B"""
        if not src or not dst:
            yield event.plain_result("用法：/合并 被合并的编号 保留的编号\n例如 /合并 110 41：把 110 的原文和信息并进 41，110 随后消失。\n保留的那个必须已入库。")
            return
        provider = self.context.get_using_provider()
        a, e1 = kb.resolve_qid(ROOT, src)
        b, e2 = kb.resolve_qid(ROOT, dst)
        if not a or not b:
            yield event.plain_result(e1 or e2)
            return
        if a["qid"] == b["qid"]:
            yield event.plain_result("两个编号相同，没有可合并的。")
            return
        if kb.is_pending(ROOT, b):
            yield event.plain_result(f"{b['qid']} 还在待入库，请先 /入库，再把它作为保留的那个。")
            return
        yield event.plain_result(f"合并中：{a['qid']} → {b['qid']}（约几秒到半分钟）…")
        async with self.lock:
            a, _ = kb.resolve_qid(ROOT, a["qid"])        # 拿到锁后重新确认两者还在
            b, _ = kb.resolve_qid(ROOT, b["qid"])
            if not a or not b:
                msg = "合并中途其中一个问题已被改动或不存在，已取消。"
            else:
                try:
                    await self._merge_into(provider, a, b)
                    h = await self._commit_git(f"合并 {a['qid']} → {b['qid']}")
                    msg = f"已把 {a['qid']} 合并进 {b['qid']}《{b['title']}》（版本 {h}）。"
                except Exception as e:
                    logger.error(f"[kb] 合并失败: {e}")
                    msg = f"合并失败：{e}"
        yield event.plain_result(msg)

    # ---------- 白话 ----------
    def _live_pending(self, sid: str):
        p = self.nl_pending.get(sid)
        if p and time.time() - p["at"] <= nl.CONFIRM_TTL:
            return p
        self.nl_pending.pop(sid, None)
        return None

    @filter.regex(_TALK_RE)
    async def talk(self, event: AstrMessageEvent):
        """白话指令：@11 帮我整理刚发的 / @11 找一下计费错误的问题"""
        sid = event.unified_msg_origin
        text = event.message_str or ""
        woke, body = nl.strip_wake(text)
        pend = None
        if not woke:                                   # 只可能是对确认的回复
            pend = self._live_pending(sid)
            if not pend or nl.confirm_answer(text) is None:
                return                                 # 没有待确认的事：照常当素材，由 collect 收集
        async for r in self._talk(event, sid, text, woke, body, pend):
            yield r
        # 注意顺序：AstrBot 在事件被终止后不会再走“发送”阶段，所以必须等回复都发出去之后才能终止，
        # 这样既不会让别的处理器（收集、默认的大模型聊天）再处理这句话，回复也不会丢。
        event.stop_event()

    async def _talk(self, event, sid: str, text: str, woke: bool, body: str, pend):
        if not woke:
            self.nl_pending.pop(sid, None)
            if not nl.confirm_answer(text):
                yield event.plain_result("好的，已取消。")
                return
            async for r in self._run_intent(event, pend["it"]):
                yield r
            return
        self.nl_pending.pop(sid, None)                 # 新的一句话作废旧的待确认
        if not body:
            yield event.plain_result("我在。直接说想做什么，比如：\n@11 帮我整理刚发的\n@11 找一下计费错误的问题\n指令列表发 /帮助")
            return
        provider = self.context.get_using_provider()
        if not provider:
            yield event.plain_result("未配置大模型提供商，白话暂时用不了，请用 /帮助 里的指令。")
            return
        vocab = self.voc()
        try:
            obj = await self._llm_json(provider, nl.build_prompt(body, self.tree, vocab))
        except Exception as e:
            logger.error(f"[kb] 白话解析失败: {e}")
            yield event.plain_result("我这会儿没法理解白话（大模型调用失败），请用 /帮助 里的指令。")
            return
        it = nl.validate(obj, self.tree, vocab)
        if it["intent"] == "unknown":
            yield event.plain_result((it.get("say") or "我没听懂。") + "\n可以这样说：@11 帮我整理刚发的 / @11 找一下限流的问题 / @11 入库；完整指令发 /帮助")
            return
        if it["intent"] in nl.WRITE_INTENTS:
            buf = self.buffers.get(sid, [])
            desc = nl.describe(it, pend=len(kb.list_pending(ROOT)),
                               buffered=f"文字 {sum(1 for k, _ in buf if k == 'text')} 段、图片 {sum(1 for k, _ in buf if k == 'img')} 张")
            self.nl_pending[sid] = {"it": it, "at": time.time()}
            yield event.plain_result(f"我理解为：{desc}。\n回复「是」执行，「取消」放弃（{nl.CONFIRM_TTL // 60} 分钟内有效）。")
            return
        async for r in self._run_intent(event, it):
            yield r

    async def _run_intent(self, event, it: dict):
        n, a = it["intent"], it["args"]
        if n == "search":
            async for r in self._nl_search(event, a):
                yield r
            return
        ev = _NLEvent(event, nl.command_text(it))
        h = {"digest": lambda: self.digest(ev, a.get("title", "")), "commit": lambda: self.commit(ev),
             "count": lambda: self.count(ev), "clear": lambda: self.clear(ev), "pending": lambda: self.pending(ev),
             "show": lambda: self.show(ev), "tree": lambda: self.show_tree(ev), "tags": lambda: self.tags(ev),
             "set_status": lambda: self.set_status(ev, a.get("qid", ""), a.get("status", "")),
             "recat": lambda: self.recat(ev, a.get("qid", ""), a.get("code", "")),
             "merge": lambda: self.merge(ev, a.get("src", ""), a.get("dst", "")),
             "approve_cat": lambda: self.approve_cat(ev), "approve_tag": lambda: self.approve_tag(ev),
             "add_alias": lambda: self.add_alias(ev), "help": lambda: self.help(ev)}[n]
        async for r in h():
            yield r

    async def _nl_search(self, event, a: dict):
        v = self.voc()
        total, hits = kb.search_any(ROOT, a["keywords"], a["categories"], a["tags"], v, limit=8)
        parts = []
        for c in a["categories"]:
            mj, sub = kb.find_cat(self.tree, c)
            parts.append(f"分类 {c} {(sub or mj)['name']}")
        if a["tags"]:
            parts.append("标签 " + "、".join(a["tags"]))
        if a["keywords"]:
            parts.append("含 " + "/".join(a["keywords"]) + " 之一")
        if not total:
            yield event.plain_result(f"我理解为：找 {'，'.join(parts)} 的问题。没有找到。换个说法，或用 /树 按分类找。")
            return
        lines = [f"我理解为：找 {'，'.join(parts)} 的问题。共 {total} 条" + ("，显示前 8 条" if total > 8 else "")] + self._hit_lines(hits)
        lines.append("\n看详情：@11 看一下 41，或 /看 41" + ("；结果太多可以说得更具体些" if total > 8 else ""))
        yield event.plain_result("\n".join(lines))

    @filter.command("帮助")
    async def help(self, event: AstrMessageEvent):
        """指令列表"""
        yield event.plain_result(
            "【收集】直接粘贴文字、发图片即可，不会回复\n"
            "/整理 [标题]　把这一批整理成一个待入库问题\n"
            "/入库　把所有待入库问题归档（自动 git 记录）\n"
            "/条数　已收集多少、待入库多少\n"
            "/清空　丢弃还没整理的内容\n"
            "【查询】\n"
            "/查 关键词 [#标签]　多个词都要命中；#标签 写别名也行，#渠道:Azure 可限定类\n"
            "/标签 [类|树]　标签分类和数量；/标签 树 看 厂商→模型→错误码\n"
            "/看 编号　简要+状态+报告文件（编号可只写序号）\n"
            "/树　分类树和各类数量\n"
            "/待入库　待入库列表\n"
            "【维护】\n"
            "/改类 编号 细类编号　　/状态 编号 已解决\n"
            "/审 编号 分数(1~5) [校正说明]　人工审核，写了说明就存成基准回答　　/待审　待审核清单\n"
            "/同意新类 [名称 [大类]]　　/合并 被并编号 保留编号\n"
            "/同意新标签 [名称 类 [厂商]]　　/加别名 规范名 别名\n"
            "【白话】以 @11 开头，用平时说话的方式，例如：\n"
            "@11 帮我整理刚发的　　@11 找一下计费错误的问题有什么\n"
            "@11 41 号改成已解决（会改动知识库的先让你回「是」确认）")
