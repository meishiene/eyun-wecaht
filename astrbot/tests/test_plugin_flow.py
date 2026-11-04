"""端到端：用桩替代 AstrBot 与大模型，跑 整理 → 入库 → 合并 → 改类。"""
import asyncio, json, os, sys, tempfile, types, shutil
from pathlib import Path

# ---- AstrBot 桩 ----
def mod(name, **kw):
    m = types.ModuleType(name); m.__dict__.update(kw); sys.modules[name] = m; return m
class _F:
    class EventMessageType: PRIVATE_MESSAGE = 1
    @staticmethod
    def command(n): return lambda f: f
    @staticmethod
    def event_message_type(t): return lambda f: f
    @staticmethod
    def regex(p): return lambda f: f
class Star:
    def __init__(self, context): self.context = context
def register(*a, **k): return lambda c: c
mod("astrbot"); mod("astrbot.api", logger=types.SimpleNamespace(info=print, error=print, warning=print))
mod("astrbot.api.event", AstrMessageEvent=object, filter=_F)
mod("astrbot.api.message_components", File=lambda **k: ("FILE", k))
mod("astrbot.api.star", Context=object, Star=Star, register=register)

root = Path(__file__).resolve().parent.parent / "plugins" / "kb_digest"
sys.path.insert(0, str(root))
import importlib.util
sys.modules["astrbot.api"].message_components = sys.modules["astrbot.api.message_components"]
spec = importlib.util.spec_from_file_location("main", root / "main.py"); M = importlib.util.module_from_spec(spec)
sys.modules["kb_core"] = importlib.import_module("kb_core"); spec.loader.exec_module(M)

work = Path(tempfile.mkdtemp()); os.chdir(work); M.ROOT = Path("data/kb")

CALLS = []
class Resp:  # 模拟 LLM
    def __init__(s, t): s.completion_text = t
class Provider:
    def __init__(s): s.mode = "new"
    async def text_chat(s, prompt, session_id=None, image_urls=None):
        CALLS.append(prompt[:12])
        if prompt.startswith("这是一张"): return Resp("图中文字：429 RPM")
        if prompt.startswith("判断【新问题】"):
            return Resp(json.dumps({"same": "Q-SAME" if s.mode == "same" else None, "similar": []}))
        if prompt.startswith("下面是知识库里已有"):
            return Resp(json.dumps({"title": "x", "category": "4.6", "status": "已解决", "source": "官方文档", "tags": ["GPT"],
                "brief": "合并后简要 手机13812345678", "phenomenon": "合并现象", "elements": {}, "cause": "c", "solution": "s",
                "reply_script": "r", "retry_helps": "有用", "need_verify": "否", "verify_materials": "", "impact": "i", "evidence": "e"}))
        return Resp(json.dumps({"title": "网关前限流无日志", "category": "4.6", "status": "已解决", "source": "经验判断",
            "tags": ["429", "号池"], "brief": "客户 sk-abcdefghijklmnop1234 报429，网关前限流无日志属正常。",
            "phenomenon": "429 RPM", "elements": {"status_code": "429", "rid": "R-9"}, "cause": "网关前限流",
            "solution": "降频", "reply_script": "请降频", "retry_helps": "有用", "need_verify": "否", "impact": "个别客户", "evidence": "原文"}))

class Img:
    def __init__(s, p): s.p = p
    async def convert_to_file_path(s): return s.p
class Plain:
    def __init__(s, t): s.text = t
Plain.__name__ = "Plain"; Img.__name__ = "Image"
class Ctx:
    def __init__(s): s.p = Provider()
    def get_using_provider(s): return s.p
class Ev:
    def __init__(s, msg="", comps=None, sid="bot1:FriendMessage:u1"):
        s.message_str = msg; s._c = comps or []; s.unified_msg_origin = sid; s.stopped = False
    def get_messages(s): return s._c
    def get_platform_id(s): return "bot1"
    def stop_event(s): s.stopped = True
    def plain_result(s, t): return t
    def chain_result(s, c): return c

async def run(gen):
    return [x async for x in gen]

async def main():
    ctx = Ctx(); p = M.KbDigest(ctx)
    png = work / "a.png"; png.write_bytes(b"\x89PNG")
    raw1 = "客户A：报429\n客户A：[图片]\n我：这是网关前限流 sk-abcdefghijklmnop1234"
    e = Ev(comps=[Plain(raw1)]); await p.collect(e); assert e.stopped
    await p.collect(Ev(comps=[Img(str(png))]))
    await p.collect(Ev(comps=[Plain("/整理 不该被收集")]))       # 指令文本不被收集
    assert "文字 1 段、图片 1 张" in (await run(p.count(Ev())))[0]
    out = await run(p.digest(Ev("整理 我的标题"), "我的标题"))
    print("\n".join(out))
    assert "已整理为待入库" in out[-1] and "4.6 限流（429）" in out[-1]
    pend = M.kb.list_pending(M.ROOT); assert len(pend) == 1
    d = pend[0]["_dir"]
    assert (d / "简要.md").exists() and (d / "报告.md").exists() and (d / "识图结果.md").exists()
    assert "abcdefghijklmnop1234" not in (d / "简要.md").read_text() + (d / "报告.md").read_text()
    raw = next((d / "原文").glob("原文-*.md")).read_text()
    assert "sk-abcdefghijklmnop1234" in raw and "[图片]" in raw          # 原文原样
    assert not p.buffers.get("bot1:FriendMessage:u1")                    # 缓冲已清
    qid1 = pend[0]["qid"]
    out = await run(p.commit(Ev())); print("\n".join(out))
    assert "入库完成" in out[0] and not M.kb.list_pending(M.ROOT)
    assert M.kb.find_problem(M.ROOT, qid1)["_dir"].parent.name == "4.6-限流（429）"
    # 第二批：判定为同一问题 → 合并
    ctx.p.mode = "same"
    orig = M.kb.list_problems
    async def second():
        await p.collect(Ev(comps=[Plain("客户B：又报429 手机13900001111")]))
        return await run(p.digest(Ev("整理"), ""))
    # 让桩的 same 指向真实编号
    Provider.text_chat_orig = Provider.text_chat
    async def patched(s, prompt, session_id=None, image_urls=None):
        r = await Provider.text_chat_orig(s, prompt, session_id, image_urls)
        r.completion_text = r.completion_text.replace("Q-SAME", qid1); return r
    Provider.text_chat = patched
    out = await second(); print("\n".join(out)); assert "已有问题" in out[-1] and qid1 in out[-1]
    out = await run(p.commit(Ev())); print("\n".join(out)); assert "合并进" in out[0]
    td = M.kb.find_problem(M.ROOT, qid1)["_dir"]
    assert len(list((td / "原文").glob("原文-*.md"))) == 2               # 原文只追加
    assert "13900001111" not in (td / "报告.md").read_text() and "13812345678" not in (td / "简要.md").read_text()
    assert not M.kb.list_pending(M.ROOT)
    # 改类
    out = await run(p.recat(Ev(), qid1, "4.7")); print(out[0])
    assert M.kb.find_problem(M.ROOT, qid1)["_dir"].parent.name.startswith("4.7")
    assert "不存在" in (await run(p.recat(Ev(), qid1, "9.9")))[0]
    assert "找不到" in (await run(p.recat(Ev(), "Q0", "4.7")))[0]
    # 重启后缓冲区仍在
    await p.collect(Ev(comps=[Plain("重启前的消息")]))
    p2 = M.KbDigest(ctx); assert p2.buffers["bot1:FriendMessage:u1"][0] == ("text", "重启前的消息")
    # 失败路径：LLM 返回垃圾 → 缓冲保留
    async def bad(s, prompt, session_id=None, image_urls=None): return Resp("不是json")
    Provider.text_chat = bad
    out = await run(p2.digest(Ev("整理"), "")); assert "无法解析" in out[-1] and p2.buffers["bot1:FriendMessage:u1"]
    # ======== 阶段 3 指令 ========
    ctx.p.mode = "new"; Provider.text_chat = Provider.text_chat_orig
    G = lambda g: run(g)
    # /查
    out = await G(p.search(Ev("查 429")));  print(out[0]); assert qid1 in out[0] and "共" in out[0]
    assert "用法" in (await G(p.search(Ev("查"))))[0]
    assert "没有找到" in (await G(p.search(Ev("查 完全无关xyz"))))[0]
    # /看（含文件、短编号、找不到）
    out = await G(p.show(Ev(f"看 {qid1}")))
    assert qid1 in out[0] and "归类：4.7" in out[0] and "原文批次：2" in out[0]
    assert out[1][0][0] == "FILE" and out[1][0][1]["name"] == f"{qid1}-报告.md" and Path(out[1][0][1]["file"]).is_absolute()
    assert "找不到" in (await G(p.show(Ev("看 Q0"))))[0] and "用法" in (await G(p.show(Ev("看"))))[0]
    short = str(int(qid1[-3:]))
    assert qid1 in (await G(p.show(Ev(f"看 {short}"))))[0]
    # /树
    t = (await G(p.show_tree(Ev())))[0]; print(t.split("\n")[0]); assert "共 1 个问题" in t and "4.7 服务端错误与断流（5xx）  1" in t
    # /状态
    assert "用法" in (await G(p.set_status(Ev(), "", "")))[0]
    assert "已解决 → 需人工核验" in (await G(p.set_status(Ev(), qid1, "需人工核验")))[0]
    assert M.kb.find_problem(M.ROOT, qid1)["status"] == "需人工核验"
    assert "状态只能是" in (await G(p.set_status(Ev(), qid1, "乱写")))[0]
    assert "找不到" in (await G(p.set_status(Ev(), "Q0", "已解决")))[0]
    # /帮助、/条数 不受影响
    assert "/合并" in (await G(p.help(Ev())))[0]
    # /同意新类：先造两个“待分类+建议新类”的问题
    class P2(Provider):
        async def text_chat(s, prompt, session_id=None, image_urls=None):
            if prompt.startswith("你是「大模型中转站」"):
                return Resp(json.dumps({"title": "蒸馏市场占比", "category": "0", "status": "需人工核验", "source": "待验证", "tags": [],
                    "brief": "无可复算数据", "new_cat": {"name": "市场规模与份额", "major": "7", "reason": "无对应细类"}}))
            return await Provider.text_chat(s, prompt, session_id, image_urls)
    ctx.p = P2()
    await p.collect(Ev(comps=[Plain("蒸馏市场占比多少")])); out = await G(p.digest(Ev("整理"), ""))
    assert "建议新增：市场规模与份额" in out[-1]
    qn = M.kb.list_pending(M.ROOT)[0]["qid"]
    assert "市场规模与份额" in (await G(p.approve_cat(Ev("同意新类"))))[0]          # 无参数：列出建议
    assert "指定大类" in (await G(p.approve_cat(Ev("同意新类 不存在的建议"))))[0]
    out = (await G(p.approve_cat(Ev("同意新类 市场规模与份额"))))[0]; print(out)
    assert "新增细类 7.4" in out and qn in out
    assert M.kb.find_problem(M.ROOT, qn)["category"] == "7.4" and "目前没有" in (await G(p.approve_cat(Ev("同意新类"))))[0]
    assert "7.4 市场规模与份额" in (await G(p.show_tree(Ev())))[0]
    assert M.kb.load_tree(M.ROOT)[6]["subs"][-1]["name"] == "市场规模与份额"        # 树已落盘
    # /合并：先入库 qn，再把它并进 qid1
    await G(p.commit(Ev()))
    assert "用法" in (await G(p.merge(Ev(), "", "")))[0]
    assert "相同" in (await G(p.merge(Ev(), qid1, qid1)))[0]
    assert "找不到" in (await G(p.merge(Ev(), "Q0", qid1)))[0]
    ctx.p = Provider()
    out = await G(p.merge(Ev(), qn, qid1)); print("\n".join(out))
    assert "合并中" in out[0] and "已把" in out[1]
    assert M.kb.find_problem(M.ROOT, qn) is None
    assert len(list((M.kb.find_problem(M.ROOT, qid1)["_dir"] / "原文").glob("原文-*.md"))) == 3   # 2 + 1，原文只追加
    # 待入库作为保留方 → 拒绝
    await p.collect(Ev(comps=[Plain("又一个")])); await G(p.digest(Ev("整理"), ""))
    qp = M.kb.list_pending(M.ROOT)[0]["qid"]
    assert "先 /入库" in (await G(p.merge(Ev(), qid1, qp)))[0]
    # /改类 用短编号 + 待入库改类
    assert "已把" in (await G(p.recat(Ev(), short, "4.6")))[0] and M.kb.find_problem(M.ROOT, qid1)["category"] == "4.6"
    assert "不存在" in (await G(p.recat(Ev(), short, "9.9")))[0]
    # ======== 标签词表 ========
    class P3(Provider):
        async def text_chat(s, prompt, session_id=None, image_urls=None):
            if prompt.startswith("你是「大模型中转站」"):
                assert "标签词表" in prompt and "渠道形态：" in prompt and "Gemini 3.1 Pro" in prompt     # 词表进了提示词
                return Resp(json.dumps({"title": "gemini 报 429", "category": "4.6", "status": "已解决", "source": "经验判断",
                    "tags": ["gemini-3.1-pro", "VT", "vt", "429", "Brand New Thing"], "brief": "x", "elements": {}}))
            return await Provider.text_chat(s, prompt, session_id, image_urls)
    ctx.p = P3()
    await p.collect(Ev(comps=[Plain("gemini 又报 429")])); out = (await G(p.digest(Ev("整理"), "")))[-1]; print(out)
    qt = out.split("已整理为待入库：")[1].split("\n")[0].strip()
    assert "厂商：Google" in out and "模型：Gemini 3.1 Pro" in out and "渠道形态：Vertex" in out and "错误码：429" in out   # 归一+补厂商
    assert "词表里没有：Brand New Thing" in out
    meta = M.kb.find_problem(M.ROOT, qt)
    assert meta["tags"] == ["Google", "Gemini 3.1 Pro", "Vertex", "429"]                                # 别名/大小写合并，新标签不进 tags
    assert "Brand New Thing" not in (meta["_dir"] / "报告.md").read_text()                              # 没确认的新标签不写进报告
    assert M.kb.load_pending_tags(M.ROOT) == {"Brand New Thing": {"qids": [qt]}}
    # /查：别名、厂商包含模型、限定类
    assert qt in (await G(p.search(Ev("查 #3pro"))))[0] and qt in (await G(p.search(Ev("查 #google"))))[0]
    assert qt in (await G(p.search(Ev("查 #渠道:vt"))))[0] and "没有找到" in (await G(p.search(Ev("查 #厂商:Vertex"))))[0]
    # /看
    assert "模型：Gemini 3.1 Pro" in (await G(p.show(Ev(f"看 {qt}"))))[0]
    # /标签：只统计已入库的问题
    assert "【模型】共 0 个" in (await G(p.tags(Ev("标签 模型"))))[0]
    await G(p.commit(Ev()))
    assert "待确认新标签：1 个" in (await G(p.tags(Ev("标签"))))[0] and "- 模型：1 个" in (await G(p.tags(Ev("标签"))))[0]
    out = (await G(p.tags(Ev("标签 模型"))))[0]; print(out); assert "Google：Gemini 3.1 Pro(1)" in out
    assert "429(" in (await G(p.tags(Ev("标签 错误码"))))[0]
    assert "类要写" in (await G(p.tags(Ev("标签 乱写"))))[0]
    # /同意新标签
    assert "Brand New Thing" in (await G(p.approve_tag(Ev("同意新标签"))))[0]
    assert "请带上类" in (await G(p.approve_tag(Ev("同意新标签 Brand New Thing"))))[0]
    assert "待确认里没有" in (await G(p.approve_tag(Ev("同意新标签 不存在 其他"))))[0]
    out = (await G(p.approve_tag(Ev("同意新标签 Brand New Thing 渠道"))))[0]; print(out)    # 类名可简写
    assert "加入「渠道形态」" in out and qt in out
    assert "Brand New Thing" in M.kb.find_problem(M.ROOT, qt)["tags"]
    assert "Brand New Thing" in (M.kb.find_problem(M.ROOT, qt)["_dir"] / "报告.md").read_text()
    assert "目前没有" in (await G(p.approve_tag(Ev("同意新标签"))))[0] and not M.kb.load_pending_tags(M.ROOT)
    # /加别名（规范名带空格）
    assert "用法" in (await G(p.add_alias(Ev("加别名 Gemini"), )))[0]
    out = (await G(p.add_alias(Ev("加别名 Gemini 3.1 Pro g3p"))))[0]; print(out); assert "加别名「g3p」" in out
    assert "已经是" in (await G(p.add_alias(Ev("加别名 Gemini 3.1 Pro 3pro"))))[0]
    assert "没有「乱写」" in (await G(p.add_alias(Ev("加别名 乱写 abc"))))[0]
    assert qt in (await G(p.search(Ev("查 #g3p"))))[0]
    # 手改词表文件立即生效
    v = json.loads((M.ROOT / "标签词表.json").read_text(encoding="utf-8"))
    assert "标签词表.json" in M.kb.git(M.ROOT, "-c", "core.quotepath=off", "ls-files") and "g3p" in json.dumps(v, ensure_ascii=False)
    assert "/标签" in (await G(p.help(Ev())))[0] and "/同意新标签" in (await G(p.help(Ev())))[0]
    # ======== 白话（@11）========
    SID = "bot1:FriendMessage:u1"
    class P4(Provider):
        script = {}
        async def text_chat(s, prompt, session_id=None, image_urls=None):
            if prompt.startswith("你是微信机器人「@11」"):
                said = prompt.split("用户说的话：")[1].strip()
                assert "分类树" in prompt and "标签词表" in prompt and "4.6 限流（429）" in prompt        # 分类树和词表进了提示词
                r = next((v for k, v in P4.script.items() if k in said), None)
                if r == "BOOM": raise RuntimeError("llm down")
                return Resp(r if isinstance(r, str) else json.dumps(r or {"intent": "unknown", "say": "没听懂"}))
            return await Provider.text_chat(s, prompt, session_id, image_urls)
    ctx.p = P4(); Provider.text_chat = Provider.text_chat_orig
    await G(p.clear(Ev("清空")))
    # 1. 白话指令不当素材；没带唤醒词的“帮我查一下余额”照常当素材
    e = Ev(comps=[Plain("@11 帮我整理刚发的")]); await p.collect(e)
    assert not e.stopped and not p.buffers.get(SID)
    e = Ev(comps=[Plain("帮我查一下余额")]); await p.collect(e)
    assert e.stopped and p.buffers[SID] == [("text", "帮我查一下余额")]
    # 2. 整理（不需要确认）
    P4.script = {"整理": {"intent": "digest", "args": {}}}
    e = Ev("@11 帮我整理刚发的"); out = await G(p.talk(e)); print(out[-1][:60])
    assert e.stopped and "已整理为待入库" in out[-1] and not p.buffers.get(SID)
    qn1 = M.kb.list_pending(M.ROOT)[-1]["qid"]
    # 3. 白话查询：关键词 OR + 限定分类；只给分类
    P4.script = {"找一下限流": {"intent": "search", "args": {"keywords": ["限流", "不存在的词"], "categories": ["4"]}},
                 "4.6里有什么": {"intent": "search", "args": {"categories": ["4.6"]}},
                 "乱分类": {"intent": "search", "args": {"categories": ["99"], "keywords": []}}}
    out = (await G(p.talk(Ev("@11 找一下限流的问题"))))[0]; print(out)
    assert out.startswith("我理解为：找 分类 4 报错与故障") and "含 限流/不存在的词 之一" in out and "网关前限流无日志" in out and "共 2 条" in out
    out = (await G(p.talk(Ev("@11 4.6里有什么"))))[0]; assert "分类 4.6 限流（429）" in out and "含" not in out and "网关前限流无日志" in out
    assert "想查什么" in (await G(p.talk(Ev("@11 乱分类"))))[0]                                        # 分类不存在、又没关键词 → 当没听懂
    # 4. 会改动知识库的：先复述，回“是”才执行
    P4.script = {"改成待跟进": {"intent": "set_status", "args": {"qid": short, "status": "待跟进"}}}
    out = (await G(p.talk(Ev(f"@11 把 {short} 改成待跟进"))))[0]; print(out)
    assert "我理解为：把" in out and "回复「是」" in out and M.kb.find_problem(M.ROOT, qid1)["status"] != "待跟进"
    e = Ev(comps=[Plain("是")]); await p.collect(e)
    assert not e.stopped and not p.buffers.get(SID)                                                      # “是”不被当成素材
    out = (await G(p.talk(Ev("是"))))[0]; assert "→ 待跟进" in out and M.kb.find_problem(M.ROOT, qid1)["status"] == "待跟进"
    assert not p.nl_pending
    # 5. 取消 / 过期 / 被新话作废 / 没有待确认时的“是”
    P4.script = {"入库": {"intent": "commit", "args": {}}, "查429": {"intent": "search", "args": {"keywords": ["429"]}}}
    out = (await G(p.talk(Ev("@11 入库吧"))))[0]; assert f"把 1 个待入库问题归档入库" in out or "待入库问题" in out
    assert "已取消" in (await G(p.talk(Ev("取消"))))[0] and M.kb.list_pending(M.ROOT)
    await G(p.talk(Ev("@11 入库吧"))); p.nl_pending[SID]["at"] -= 500                                  # 过期
    assert await G(p.talk(Ev("是"))) == [] and M.kb.list_pending(M.ROOT)
    e = Ev(comps=[Plain("是")]); await p.collect(e); assert e.stopped and p.buffers[SID] == [("text", "是")]  # 过期后“是”照常是素材
    await G(p.clear(Ev("清空")))
    await G(p.talk(Ev("@11 入库吧"))); await G(p.talk(Ev("@11 查429")))                                  # 新的话作废旧的待确认
    assert not p.nl_pending and await G(p.talk(Ev("是"))) == [] and M.kb.list_pending(M.ROOT)
    await G(p.clear(Ev("清空")))
    # 6. 确认入库真的执行
    await G(p.talk(Ev("@11 入库吧"))); out = await G(p.talk(Ev("好的")))
    assert "入库完成" in out[0] and not M.kb.list_pending(M.ROOT) and M.kb.find_problem(M.ROOT, qn1)
    # 7. 听不懂 / 参数不合法 / 大模型挂了 / 只喊了名字 / 乱编的意图
    P4.script = {"乱七八糟": {"intent": "unknown", "say": "不太明白你想做什么"},
                 "状态乱改": {"intent": "set_status", "args": {"qid": short, "status": "随便"}},
                 "编号乱写": {"intent": "show", "args": {"qid": "drop table"}},
                 "炸了": "BOOM", "非json": "好的我来处理", "删库": {"intent": "delete_all", "args": {}}}
    assert "不太明白" in (await G(p.talk(Ev("@11 乱七八糟"))))[0]
    assert "改状态要说清" in (await G(p.talk(Ev("@11 状态乱改"))))[0] and "没看出要看哪个" in (await G(p.talk(Ev("@11 编号乱写"))))[0]
    assert "没法理解白话" in (await G(p.talk(Ev("@11 炸了"))))[0] and "没听懂" in (await G(p.talk(Ev("@11 非json"))))[0]
    assert "没听懂" in (await G(p.talk(Ev("@11 删库"))))[0] and not p.nl_pending
    assert "我在" in (await G(p.talk(Ev("@11"))))[0] and "我在" in (await G(p.talk(Ev("＠11，"))))[0]
    assert "@11" in (await G(p.help(Ev())))[0]
    assert not p.buffers.get(SID) and not p.nl_pending
    # 8. 事件终止的时机：AstrBot 在事件终止后不再发送回复，所以每条回复产出时事件必须还没被终止，全部发完后才终止
    async def talk_checked(msg):
        e, out = Ev(msg), []
        async for r in p.talk(e):
            assert not e.stopped, "回复产出时事件已被终止，AstrBot 会丢掉这条回复"
            out.append(r)
        return e, out
    P4.script = {"整理": {"intent": "digest", "args": {}}, "找一下": {"intent": "search", "args": {"keywords": ["限流"]}},
                 "入库吧": {"intent": "commit", "args": {}}, "乱七八糟": {"intent": "unknown", "say": "没懂"}}
    await p.collect(Ev(comps=[Plain("又一批素材 429")]))
    for msg in ["@11 帮我整理刚发的", "@11 找一下限流", "@11 入库吧", "是", "@11 乱七八糟", "@11"]:
        e, out = await talk_checked(msg)
        assert out and e.stopped, msg                                    # 有回复，且处理完后终止（不再被当素材、不触发默认聊天）
    e, out = await talk_checked("是"); assert out == [] and not e.stopped      # 没有待确认的事：不回复、不终止，交给 collect 当素材
    e, out = await talk_checked("普通的一句话"); assert out == [] and not e.stopped
    # ======== 新字段：原理说明 / 归属侧 / 常态 / 关联分类 / 审核 / 三层标签 ========
    await G(p.clear(Ev("清空"))); await G(p.commit(Ev()))
    class P5(Provider):
        reply = {}
        async def text_chat(s, prompt, session_id=None, image_urls=None):
            if prompt.startswith("你是「大模型中转站」"):
                for need in ("principle", "owner_side", "routine", "related_cat", "以【根因】决定主分类", "1.3"):
                    assert need in prompt, need                                  # 新字段和边界规则进了提示词
                return Resp(json.dumps(P5.reply))
            return await Provider.text_chat(s, prompt, session_id, image_urls)
    ctx.p = P5()
    P5.reply = {"title": "号被封后报401", "category": "4.1", "status": "已解决", "source": "经验判断", "tags": ["Claude 5", "401"],
                "brief": "账号被上游封禁后返回 401", "elements": {}, "reply_script": "AI原稿：请稍等我们切换账号",
                "principle": "上游把这个账号封了，所以请求被拒绝。对客户来说就是暂时不能用，联系 13812345678 处理。",
                "owner_side": "供应侧", "routine": "非常态", "related_cat": "1.3"}
    await p.collect(Ev(comps=[Plain("客户反馈 401，账号被封")])); out = (await G(p.digest(Ev("整理"), "")))[-1]; print(out)
    assert "归属侧：供应侧｜类型：非常态" in out and "同时牵涉：1.3 账号失效、封禁与风控" in out and "建议转技术支持" in out
    nf = M.kb.list_pending(M.ROOT)[0]; qf = nf["qid"]
    assert nf["owner_side"] == "供应侧" and nf["routine"] == "非常态" and nf["related_cats"] == ["1.3"] and nf["category"] == "4.1"
    rep = (nf["_dir"] / "报告.md").read_text(); rj = json.loads((nf["_dir"] / "报告.json").read_text())
    assert "## 原理说明（给非技术人员看）" in rep and "上游把这个账号封了" in rep and "13812345678" not in rep + json.dumps(rj, ensure_ascii=False)
    assert "归属侧：供应侧　类型：非常态　审核：未审核" in rep and "相关分类：1.3" in rep and "related_cat" not in rj
    await G(p.commit(Ev()))
    # 看 / 查：非常态提示；关联分类也查得到
    out = (await G(p.show(Ev(f"看 {qf}"))))[0]; print(out)
    assert "归属侧：供应侧｜类型：非常态｜审核：未审核" in out and "同时牵涉：1.3" in out and "建议转技术支持" in out
    assert "【非常态】" in (await G(p.search(Ev("查 401"))))[0]
    assert qf in [m["qid"] for _, m in M.kb.search_any(M.ROOT, cats=["1.3"])[1]] and qf in [m["qid"] for _, m in M.kb.search_any(M.ROOT, cats=["1"])[1]]
    # 大模型乱填：枚举退回待判断，不存在的/等于主分类的关联分类丢掉
    P5.reply = {**P5.reply, "title": "乱填的", "owner_side": "不知道哪边", "routine": "也许", "related_cat": "9.9", "principle": ""}
    await p.collect(Ev(comps=[Plain("另一个问题")])); await G(p.digest(Ev("整理"), ""))
    bad = M.kb.list_pending(M.ROOT)[0]; qb = bad["qid"]
    assert bad["owner_side"] == "待判断" and bad["routine"] == "待判断" and bad["related_cats"] == []
    assert "## 原理说明（给非技术人员看）\n（待补全）" in (bad["_dir"] / "报告.md").read_text()      # 空的不冒充“原文未提及”
    await G(p.commit(Ev()))
    # 审核
    assert "用法" in (await G(p.audit(Ev("审"))))[0]
    assert "1~5" in (await G(p.audit(Ev(f"审 {qf} 9"))))[0] and "1~5" in (await G(p.audit(Ev(f"审 {qf} 好"))))[0]
    assert "找不到" in (await G(p.audit(Ev("审 Q0 3"))))[0]
    assert "未审核" in (await G(p.audit(Ev(f"审 {qf}"))))[0]
    out = (await G(p.audit(Ev(f"审 {qf} 3 应先确认账号状态，再说明联系 13900001111 处理"))))[0]; print(out)
    assert "未审核 → 3 分（需校正）" in out and "基准回答" in out
    fm = M.kb.find_problem(M.ROOT, qf); rj2 = json.loads((fm["_dir"] / "报告.json").read_text())
    assert fm["audit_score"] == 3 and "13900001111" not in fm["baseline_reply"] and "应先确认账号状态" in fm["baseline_reply"]
    assert rj2["reply_script"] == "AI原稿：请稍等我们切换账号"                      # AI 原稿保留
    rep = (fm["_dir"] / "报告.md").read_text()
    assert "## 基准回答（人工校正" in rep and "AI原稿：请稍等我们切换账号" in rep and "审核：3 分（需校正）" in rep
    assert qf in (await G(p.to_review(Ev("待审"))))[0] and qb not in (await G(p.to_review(Ev("待审"))))[0]   # 低分进待审，没审过的普通问题不算
    assert "3 分（需校正） → 5 分（可用）" in (await G(p.audit(Ev(f"审 {qf} 5"))))[0]
    assert M.kb.find_problem(M.ROOT, qf)["baseline_reply"] and "没有待审核" in (await G(p.to_review(Ev("待审"))))[0]   # 只改分数，基准回答保留
    assert f"审核 {qf}：5 分（可用）" in M.kb.git(M.ROOT, "log", "--oneline", "-3")          # 审核进了 git 历史
    # AI 补全待审标记：补全后进待审，审核后消失
    d = M.kb.find_problem(M.ROOT, qb)["_dir"]; mm = M.kb.read_meta(d); mm["ai_filled"] = True; M.kb.write_meta(d, mm); M.kb.refresh_report(d, mm)
    assert "AI 补全" in (d / "报告.md").read_text() and qb in (await G(p.to_review(Ev("待审"))))[0]
    assert "AI 补全" in (await G(p.show(Ev(f"看 {qb}"))))[0]
    await G(p.audit(Ev(f"审 {qb} 4")))
    assert not M.kb.find_problem(M.ROOT, qb)["ai_filled"] and "没有待审核" in (await G(p.to_review(Ev("待审"))))[0]
    # 改类到关联分类 → 关联里不再重复
    await G(p.recat(Ev(), qf, "1.3"))
    fm = M.kb.find_problem(M.ROOT, qf); assert fm["category"] == "1.3" and fm["related_cats"] == []
    # 三层标签 + 帮助
    out = (await G(p.tags(Ev("标签 树"))))[0]; print(out)
    assert "【Anthropic】" in out and "Claude 5(" in out and "401(" in out
    h = (await G(p.help(Ev())))[0]; assert "/审" in h and "/待审" in h and "标签 树" in h
    # 合并时新字段不丢
    P5.reply = {"title": "补充", "category": "1.3", "status": "已解决", "source": "经验判断", "brief": "补充信息", "elements": {}}   # 没有新字段
    class P6(P5):
        async def text_chat(s, prompt, session_id=None, image_urls=None):
            if prompt.startswith("判断【新问题】"):
                return Resp(json.dumps({"same": qf, "similar": []}))
            if prompt.startswith("下面是知识库里已有"):
                return Resp(json.dumps({"title": "x", "category": "1.3", "status": "已解决", "source": "经验判断", "tags": ["Claude 5"],
                                        "brief": "合并后", "phenomenon": "p", "elements": {}, "cause": "c", "solution": "s", "reply_script": "r"}))
            return await P5.text_chat(s, prompt, session_id, image_urls)
    ctx.p = P6()
    await p.collect(Ev(comps=[Plain("同一问题的补充")])); await G(p.digest(Ev("整理"), "")); await G(p.commit(Ev()))
    fm = M.kb.find_problem(M.ROOT, qf); rj3 = json.loads((fm["_dir"] / "报告.json").read_text())
    assert rj3["principle"].startswith("上游把这个账号封了") and fm["owner_side"] == "供应侧" and fm["routine"] == "非常态", (rj3, fm)
    assert fm["baseline_reply"] and fm["audit_score"] == 5                                                 # 合并不动人工审核
    # 锁没有被任何指令占住
    assert not p.lock.locked()
    print("git log:\n" + M.kb.git(M.ROOT, "log", "--oneline"))
    print("\nALL FLOW OK")

asyncio.run(main()); shutil.rmtree(work, ignore_errors=True)
