"""知识库核心逻辑：不依赖 AstrBot，可独立测试。"""
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

DEFAULT_TREE = [
    ("1", "分组、渠道与账号", [
        ("1.1", "分组选择与路由"), ("1.2", "渠道接入与配置"), ("1.3", "账号失效、封禁与风控"),
        ("1.4", "号池与会话连续性"), ("1.5", "渠道稳定性与延迟"), ("1.6", "上游切换与降级")]),
    ("2", "计费、余额与缓存", [
        ("2.1", "倍率与定价"), ("2.2", "余额、充值与退款"), ("2.3", "用量对账与扣费争议"),
        ("2.4", "Token计量与系统提示词注入"), ("2.5", "缓存命中与节点亲和")]),
    ("3", "模型与能力", [
        ("3.1", "模型上下线与映射"), ("3.2", "真假模型与降智"), ("3.3", "功能支持与能力边界"),
        ("3.4", "生图与视频模型"), ("3.5", "模型专项约束")]),
    ("4", "报错与故障", [
        ("4.1", "鉴权与权限（401、403）"), ("4.2", "路径与模型不存在（404）"),
        ("4.3", "推理与采样参数错误（400）"), ("4.4", "工具调用与多模态请求体错误（400）"),
        ("4.5", "内容审核与风控拦截"), ("4.6", "限流（429）"), ("4.7", "服务端错误与断流（5xx）"),
        ("4.8", "返回异常（空回复、截断、规格不达标）")]),
    ("5", "对接与使用", [
        ("5.1", "客户端与工具配置"), ("5.2", "接口格式与模型接口匹配"), ("5.3", "Key与令牌管理"),
        ("5.4", "网络与代理"), ("5.5", "控制台与日志判读")]),
    ("6", "货盘与采购（供给侧）", [
        ("6.1", "货品识别与渠道分层"), ("6.2", "报价与成本核算"), ("6.3", "并发与配额"),
        ("6.4", "验货与保真探针"), ("6.5", "账号池运营与故障复盘")]),
    ("7", "客户与商务", [
        ("7.1", "客诉与售后"), ("7.2", "竞品与价格对比"), ("7.3", "合规与风险")]),
    ("0", "待分类", []),
]

STATUSES = ["已解决", "未解决", "待跟进", "需人工核验"]
SOURCES = ["官方文档", "后台核验", "经验判断", "知识库提炼", "待验证"]
OWNER_SIDES = ["请求侧", "供应侧", "平台侧", "待判断"]     # 报错归属：谁那边的问题
ROUTINES = ["常态", "非常态", "待判断"]                     # 常态：知识库直接答；非常态：转技术支持
AUDIT_PASS = 4                                              # 审核分 >= 4 算“可用”，<= 3 算“需校正”
SUPPORT_HINT = "💬 非常态问题，建议转技术支持，不要自行答复。"

# 给大模型看的新字段说明和分类边界规则。整理（main.py）和存量补全（seed/backfill.py）共用这一份，避免两处措辞跑偏。
NEW_FIELDS_SCHEMA = """ "principle": "原理说明，写给不懂技术的业务人员：2~4句白话，先讲“为什么会这样”，再讲“对客户意味着什么”；原文没有依据就写“原文未提及”，不要编造技术细节",
 "owner_side": "请求侧|供应侧|平台侧|待判断 之一。请求侧=客户的请求本身有问题（参数、Key、余额、客户端配置）；供应侧=上游渠道/账号/供货方的问题（上游报错、账号失效封禁、上游限流）；平台侧=中转站自己的问题（路由、计费口径、网关故障）；信息不足就填待判断，不要猜",
 "routine": "常态|非常态|待判断 之一。常态=常规报错、服务可用性、资源库存这类，可以直接给原因和话术；非常态=小众深度技术问题、需要现场验证或跑脚本的定制化需求，应转技术支持",
 "related_cat": null 或 另一个细类编号（见分类边界规则，category 里只放主分类）"""
BOUNDARY_RULE = ("分类边界：以【根因】决定主分类。根因是账号被封、失效、风控的归 1.3；根因是请求格式、鉴权、限流等请求/接口层面的归 4.x。\n"
                 "一个问题同时牵涉另一边时，category 仍只填主分类，把另一边的细类编号填进 related_cat；没有牵涉就填 null。")


# ---------- 分类树 ----------
def _default_tree_obj():
    return [{"code": c, "name": n, "subs": [{"code": sc, "name": sn} for sc, sn in subs]}
            for c, n, subs in DEFAULT_TREE]


def load_tree(root: Path) -> list:
    p = root / "分类树.json"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_default_tree_obj(), ensure_ascii=False, indent=2), encoding="utf-8")
    return json.loads(p.read_text(encoding="utf-8"))


def tree_text(tree: list) -> str:
    out = []
    for m in tree:
        out.append(f"{m['code']} {m['name']}")
        for s in m["subs"]:
            out.append(f"  {s['code']} {s['name']}")
    return "\n".join(out)


def find_cat(tree: list, code: str):
    """code 可为大类或细类编号。返回 (major, sub或None)。"""
    code = (code or "").strip()
    for m in tree:
        if m["code"] == code:
            return m, None
        for s in m["subs"]:
            if s["code"] == code:
                return m, s
    return None, None


def cat_dir(root: Path, tree: list, code: str) -> Path:
    m, s = find_cat(tree, code)
    if m is None:
        m, s = find_cat(tree, "0")
    base = root / "问题库" / f"{m['code']}-{m['name']}"
    return base / f"{s['code']}-{s['name']}" if s else base


# ---------- 脱敏 ----------
_RULES = [
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"), lambda m: m.group(0)[:5] + "****" + m.group(0)[-4:]),
    (re.compile(r"\b(?:AIza|ghp_|xox[bp]-)[A-Za-z0-9_\-]{12,}"), lambda m: m.group(0)[:4] + "****" + m.group(0)[-4:]),
    (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{12,}"), lambda m: m.group(1) + "****"),
    (re.compile(r"(?i)([?&](?:key|token|api_key|access_token)=)[^&\s)\]]+"), lambda m: m.group(1) + "****"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), lambda m: m.group(0)[:3] + "****" + m.group(0)[-4:]),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), lambda m: "***@***"),
]


def redact(text: str) -> str:
    for rx, fn in _RULES:
        text = rx.sub(fn, text)
    return text


# ---------- JSON 容错 ----------
def _close_json(s: str) -> str:
    """按括号配对补全被截断的 JSON（先闭合未结束的字符串，再补 ] 和 }）。"""
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        elif ch == '"': in_str = True
        elif ch in "{[": stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack: stack.pop()
    if in_str: s += '"'
    return s.rstrip().rstrip(",") + "".join(reversed(stack))


def parse_json(text: str):
    """容错解析大模型返回的 JSON：去代码块围栏、截取最外层对象、修复末尾被截断的多余字段。"""
    if not text:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.S)
    cands = [t]
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        cands.append(t[i:j + 1])
    if i >= 0:
        # 末尾出现没写完的字段（如  ,"x": "  ），去掉它并补上右花括号
        head = re.sub(r',\s*"[^"\n]*"\s*:\s*(?:"[^"]*|[^,}\]]*)\s*$', "", t[i:].rstrip())
        cands.append(_close_json(head))
    for c in cands:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


# ---------- 编号 ----------
def next_qid(root: Path) -> str:
    """编号只增不复用：取计数器与磁盘上已有编号的较大值。"""
    p = root / ".counter.json"
    day = time.strftime("%Y%m%d")
    d = json.loads(p.read_text()) if p.exists() else {}
    seen = 0
    for x in root.rglob(f"Q{day}-*"):
        m = re.match(rf"Q{day}-(\d+)", x.name)
        if m:
            seen = max(seen, int(m.group(1)))
    n = max(d.get(day, 0), seen) + 1
    d[day] = n
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))
    return f"Q{day}-{n:03d}"


def slug(title: str, n: int = 30) -> str:
    s = re.sub(r'[\\/:*?"<>|\s#]+', "_", title).strip("_")
    return s[:n] or "未命名"


# ---------- 渲染 ----------
def render_report(r: dict, meta: dict) -> str:
    e = r.get("elements") or {}
    def g(k, d="（原文未提及）"):
        v = r.get(k)
        return str(v).strip() if v not in (None, "", []) else d
    owner = norm_enum(meta.get("owner_side") or r.get("owner_side"), OWNER_SIDES)
    routine = norm_enum(meta.get("routine") or r.get("routine"), ROUTINES)
    lines = [
        f"# {meta['title']}", "",
        f"- 编号：{meta['qid']}　状态：{meta['status']}　口径来源：{meta['source']}",
        f"- 归属侧：{owner}　类型：{routine}　审核：{audit_label(meta)}",
        f"- 标签：{'、'.join(meta.get('tags') or []) or '无'}",
    ]
    if meta.get("related_cats"):
        lines.append(f"- 相关分类：{'、'.join(str(c) for c in meta['related_cats'])}")
    if meta.get("ai_filled"):
        lines.append("- ⚠️ 原理说明、归属侧、类型由 AI 补全，待人工审核（/审 编号 分数）")
    if routine == "非常态":
        lines.append(f"- {SUPPORT_HINT}")
    lines += [
        "",
        "## 现象", g("phenomenon"), "",
        "## 报错 5 要素",
        f"- 状态码：{e.get('status_code') or '—'}",
        f"- message：{e.get('message') or '—'}",
        f"- status 字段：{e.get('status_field') or '—'}",
        f"- rid：{e.get('rid') or '—'}",
        f"- 模型 / 接口路径 / 分组：{e.get('model_path_group') or '—'}", "",
        "## 原因", g("cause"), "",
        "## 原理说明（给非技术人员看）", g("principle", "（待补全）"), "",
        "## 解决方案", g("solution"), "",
        "## 答复口径（可直接发给客户）", g("reply_script"), "",
        f"## 重试是否有用\n{g('retry_helps', '未说明')}", "",
        f"## 是否需要收 rid 人工核验\n{g('need_verify', '否')}",
    ]
    if r.get("verify_materials"):
        lines += ["需要收集：" + str(r["verify_materials"])]
    if meta.get("baseline_reply"):
        lines += ["", f"## 基准回答（人工校正，{meta.get('audited_at', '')}）", str(meta["baseline_reply"]),
                  "", "> 上面“答复口径”是 AI 原稿，保留作对照；对客户请优先用基准回答。"]
    lines += ["", "## 影响范围", g("impact"), "", "## 依据（对应原文位置）", g("evidence"), "",
              "## 相关问题"]
    rel = meta.get("related") or []
    lines.append("、".join(rel) if rel else "无")
    if meta.get("suspect_dup"):
        lines += ["", f"> ⚠️ 疑似重复：{'、'.join(meta['suspect_dup'])}，待确认"]
    if meta.get("new_cat"):
        nc = meta["new_cat"]
        lines += ["", f"> ⚠️ 建议新增细类：{nc.get('name', '')}（{nc.get('reason', '')}）"]
    return redact("\n".join(lines)) + "\n"


# ---------- 新增字段：原理说明 / 归属侧 / 常态 / 关联分类 / 审核 ----------
def norm_enum(v, allowed: list, default: str = "待判断") -> str:
    v = str(v or "").strip()
    return v if v in allowed else default


def audit_label(meta: dict) -> str:
    s = meta.get("audit_score")
    if not s:
        return "未审核"
    return f"{s} 分（{'可用' if int(s) >= AUDIT_PASS else '需校正'}）"


def extract_fields(data: dict, tree: list, cat: str) -> dict:
    """从大模型返回的 JSON 里取出新字段并校验。只返回 data 里出现过的字段；
    枚举不在范围内的一律退回“待判断”，关联分类必须真实存在且不等于主分类。"""
    out = {}
    if "principle" in data:
        out["principle"] = redact(str(data.get("principle") or "").strip())[:600]
    if "owner_side" in data:
        out["owner_side"] = norm_enum(data.get("owner_side"), OWNER_SIDES)
    if "routine" in data:
        out["routine"] = norm_enum(data.get("routine"), ROUTINES)
    rc = data.get("related_cat")
    codes = rc if isinstance(rc, list) else [rc]
    out["related_cats"] = [c for c in dict.fromkeys(str(x).strip() for x in codes if x)
                           if c != str(cat) and find_cat(tree, c)[1] is not None][:3]
    return out


def apply_fields(meta: dict, data: dict, tree: list, cat: str = None) -> dict:
    """把 extract_fields 的结果同时写进报告 data 和元数据 meta（关联分类与已有的取并集）。"""
    f = extract_fields(data, tree, cat or meta.get("category", "0"))
    for k in ("principle", "owner_side", "routine"):
        if k in f and (f[k] or k != "principle"):
            data[k] = f[k]
    for k in ("owner_side", "routine"):
        if k in f:
            meta[k] = f[k]
    meta["related_cats"] = list(dict.fromkeys([*(meta.get("related_cats") or []), *f["related_cats"]]))
    data.pop("related_cat", None)
    return f


def refresh_report(d: Path, meta: dict = None):
    """元数据变了以后，重写 报告.md（报告.json 不变）。"""
    meta = meta if meta is not None else read_meta(d)
    rp = d / "报告.json"
    if rp.exists():
        (d / "报告.md").write_text(render_report(json.loads(rp.read_text(encoding="utf-8")), meta), encoding="utf-8")


def set_audit(root: Path, qid: str, score, note: str = "") -> str:
    """人工审核：打 1~5 分。写了校正说明就存成“基准回答”，AI 原稿（报告.json 里的 reply_script）不动。
    审核过的问题不再算“AI 补全待审”。返回旧的审核描述。"""
    try:
        s = int(str(score).strip())
    except ValueError:
        raise ValueError("分数要是 1~5 的整数。") from None
    if not 1 <= s <= 5:
        raise ValueError("分数要是 1~5 的整数。")
    m = find_problem(root, qid)
    if not m:
        raise KeyError(qid)
    d = m["_dir"]
    meta = read_meta(d)
    old = audit_label(meta)
    note = redact((note or "").strip())[:1000]
    meta["audit_score"], meta["audited_at"], meta["ai_filled"] = s, time.strftime("%Y-%m-%d %H:%M:%S"), False
    if note:
        meta["audit_note"] = meta["baseline_reply"] = note
    write_meta(d, meta)
    refresh_report(d, meta)
    return old


def needs_review(m: dict) -> bool:
    """AI 补全还没人看过，或审核打了低分需要校正。"""
    s = m.get("audit_score")
    return bool(m.get("ai_filled")) or bool(s and int(s) < AUDIT_PASS)


def item_cats(m: dict) -> list:
    return [str(m.get("category", "0")), *[str(c) for c in (m.get("related_cats") or [])]]


def in_cats(m: dict, cats) -> bool:
    """问题的主分类或关联分类，落在 cats（大类或细类编号）里任何一个。"""
    return any(c == x or c.startswith(x + ".") for c in item_cats(m) for x in cats)


# ---------- 存取 ----------
def _meta_path(d: Path) -> Path:
    return d / "元数据.json"


def read_meta(d: Path) -> dict:
    return json.loads(_meta_path(d).read_text(encoding="utf-8"))


def write_meta(d: Path, meta: dict):
    _meta_path(d).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_dir(root: Path) -> Path:
    return root / "待入库"


def list_pending(root: Path) -> list:
    p = pending_dir(root)
    return sorted((read_meta(d) | {"_dir": d} for d in p.iterdir() if d.is_dir() and _meta_path(d).exists()),
                  key=lambda m: m["qid"]) if p.exists() else []


def list_problems(root: Path, sub_code: str | None = None) -> list:
    base = root / "问题库"
    out = []
    if not base.exists():
        return out
    for mp in base.rglob("元数据.json"):
        m = json.loads(mp.read_text(encoding="utf-8"))
        if sub_code is None or m.get("category") == sub_code:
            out.append(m | {"_dir": mp.parent})
    return sorted(out, key=lambda m: m["qid"])


def find_problem(root: Path, qid: str):
    for m in list_pending(root) + list_problems(root):
        if m["qid"] == qid:
            return m
    return None


def write_raw(dest_raw: Path, items: list, bot: str, sid: str):
    """items: [("text", str) | ("img", Path)]。原文逐条原样保存，只追加。"""
    dest_raw.mkdir(parents=True, exist_ok=True)
    (dest_raw / "images").mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    n = len(list(dest_raw.glob("原文-*.md"))) + 1
    img_names, body = [], []
    for k, v in items:
        if k == "text":
            body.append(v)
        else:
            nm = f"{stamp}-{len(img_names) + 1:02d}{Path(v).suffix or '.jpg'}"
            shutil.copy(v, dest_raw / "images" / nm)
            img_names.append(nm)
            body.append(f"[图片：images/{nm}]")
    head = (f"<!-- 入库批次 {n} | 时间 {time.strftime('%Y-%m-%d %H:%M:%S')} | 来源机器人 {bot} | 会话 {sid} -->\n\n")
    sep = "\n\n-----消息分隔-----\n\n"
    (dest_raw / f"原文-{n:02d}-{stamp}.md").write_text(head + sep.join(body) + "\n", encoding="utf-8")
    return n


def move_problem(root: Path, tree: list, qid: str, new_code: str) -> Path:
    m = find_problem(root, qid)
    if not m:
        raise KeyError(qid)
    _, s = find_cat(tree, new_code)
    if s is None and new_code != "0":
        raise ValueError(f"细类 {new_code} 不存在")
    src = m["_dir"]
    meta = read_meta(src)
    meta["category"] = new_code
    if meta.get("related_cats"):          # 新主分类不再重复出现在关联分类里
        meta["related_cats"] = [c for c in meta["related_cats"] if str(c) != str(new_code)]
    write_meta(src, meta)
    refresh_report(src, meta)
    if src.parent == pending_dir(root):
        return src  # 待入库阶段只改元数据，入库时落位
    dst = cat_dir(root, tree, new_code) / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return dst


def commit_pending_item(root: Path, tree: list, m: dict) -> Path:
    src = m["_dir"]
    dst = cat_dir(root, tree, m["category"]) / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    meta = read_meta(dst)
    meta["committed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_meta(dst, meta)
    return dst


# ---------- git ----------
def git(root: Path, *args) -> str:
    r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()


def git_commit(root: Path, msg: str) -> str:
    """提交全部变更。每一步都独立检查，能从半初始化的仓库恢复；身份每次显式传入，不依赖 git 配置。"""
    if not (root / ".git").exists():
        git(root, "init", "-q")
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text(".buffers/\n.counter.json\n", encoding="utf-8")
    # 应被忽略的文件如果曾被误跟踪（例如 .gitignore 创建前执行过 add），先取消跟踪
    git(root, "rm", "-r", "--cached", "-q", "--ignore-unmatch", ".buffers", ".counter.json")
    git(root, "add", "-A")
    if not git(root, "status", "--porcelain"):
        return "无变更"
    git(root, "-c", "user.name=kb-bot", "-c", "user.email=kb-bot@localhost", "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "--short", "HEAD")


def merge_raw(src_raw: Path, dst_raw: Path):
    """把待入库问题的原文批次追加到目标问题的原文目录（只追加，不覆盖）。"""
    dst_raw.mkdir(parents=True, exist_ok=True)
    (dst_raw / "images").mkdir(exist_ok=True)
    for f in sorted(src_raw.glob("原文-*.md")):
        n = len(list(dst_raw.glob("原文-*.md"))) + 1
        rest = f.name.split("-", 2)[-1]
        shutil.copy(f, dst_raw / f"原文-{n:02d}-{rest}")
    if (src_raw / "images").exists():
        for im in (src_raw / "images").iterdir():
            t = dst_raw / "images" / im.name
            if t.exists():
                t = dst_raw / "images" / f"{time.strftime('%H%M%S')}-{im.name}"
            shutil.copy(im, t)


# ====================== 阶段 3：检索、树、状态、新类 ======================
_QID_RE = re.compile(r"^Q\d{8}-\d{3}$", re.I)


def all_items(root: Path) -> list:
    return list_problems(root) + list_pending(root)


def is_pending(root: Path, m: dict) -> bool:
    return m["_dir"].parent == pending_dir(root)


def resolve_qid(root: Path, text: str):
    """支持完整编号、不带 Q、或只写序号（唯一时）。返回 (meta, None) 或 (None, 错误信息)。"""
    t = (text or "").strip().upper()
    if not t:
        return None, "请给出问题编号，例如 Q20261102-041 或 41。"
    items = all_items(root)
    by = {m["qid"].upper(): m for m in items}
    if t in by:
        return by[t], None
    if not t.startswith("Q") and ("Q" + t) in by:
        return by["Q" + t], None
    num = t[1:] if t.startswith("Q") else t
    if num.isdigit():
        hit = [m for m in items if m["qid"].endswith(f"-{int(num):03d}")]
        if len(hit) == 1:
            return hit[0], None
        if len(hit) > 1:
            return None, f"序号 {num} 对应多个问题，请写完整编号：" + "、".join(m["qid"] for m in hit)
    return None, f"找不到问题 {text}。"


def _text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


_W = {"t": 5, "g": 4, "b": 3, "r": 1}
_STOP = set("的了吗么呢吧啊怎什如何是不有能否这那个我你它他们请问想要可以会还都就也和与及或")


def _strings(o):
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, (list, tuple)):
        for v in o:
            yield from _strings(v)


def _report_text(d: Path) -> str:
    """只取报告的实际内容（不含模板里固定的“报错 5 要素：rid、分组…”标题，否则这些词会命中所有问题）。"""
    try:
        return "\n".join(_strings(json.loads((d / "报告.json").read_text(encoding="utf-8"))))
    except Exception:
        return _text(d / "报告.md")


def _tag_filters(vocab, toks):
    """#名 或 #类:名 → [(类或None, 规范名或None, 原文小写)]。词表认得的走规范名，不认得的退回子串匹配。"""
    out = []
    for t in toks:
        if not (t.startswith("#") and len(t) > 1):
            continue
        raw, grp = t[1:], None
        if vocab:
            for sep in (":", "："):
                if sep in raw:
                    g, n = raw.split(sep, 1)
                    if vocab.group_match(g) and n:
                        grp, raw = vocab.group_match(g), n
                    break
        out.append((grp, vocab.canon(raw) if vocab else None, raw.lower()))
    return out


def _tag_ok(vocab, m, flt) -> bool:
    grp, c, raw = flt
    tags = m.get("tags", [])
    if c:
        if grp and vocab.group_of(c) != grp:
            return False
        return vocab.matches(tags, c)
    if grp:
        return False
    return any(raw in x.lower() for x in tags)


def _split_query(vocab, query: str) -> list:
    """按空格切词，但 #标签 的名字里有空格时（#Claude 5、#渠道形态:Gemini 3.1 Pro）按词表里最长的写法合并成一个词。"""
    toks, out, i = query.split(), [], 0
    while i < len(toks):
        t = toks[i]
        if vocab and t.startswith("#") and len(t) > 1:
            for j in range(min(len(toks), i + 6), i + 1, -1):          # 最多合并 5 个词，取最长的已知写法
                cand = " ".join([t[1:], *toks[i + 1:j]])
                name = cand
                for sep in (":", "："):
                    if sep in cand and vocab.group_match(cand.split(sep, 1)[0]):
                        name = cand.split(sep, 1)[1]
                        break
                if vocab.canon(name):
                    out.append("#" + cand)
                    i = j - 1
                    break
            else:
                out.append(t)
        else:
            out.append(t)
        i += 1
    return out


def search(root: Path, query: str, limit: int = 8, vocab=None):
    """关键词检索。多个词要求同时命中(AND)；#标签 / #类:名 做过滤（传入词表时按规范名和别名归一，厂商包含它的模型；
    标签名里有空格也行，如 #Claude 5）；无结果时退化为二字组合匹配。返回 (命中总数, 前 limit 条[(score, meta)])"""
    toks = _split_query(vocab, query)
    tags = _tag_filters(vocab, toks)
    terms = [t.lower() for t in toks if not t.startswith("#")]
    docs = []
    for m in all_items(root):
        if tags and not all(_tag_ok(vocab, m, f_) for f_ in tags):
            continue
        f = {"t": f"{m['qid']} {m['title']}".lower(), "g": " ".join(m.get("tags", [])).lower(),
             "b": m.get("brief", "").lower(), "r": _report_text(m["_dir"]).lower()}
        docs.append((m, f))
    if not terms:                          # 只有标签过滤
        hits = [(1, m) for m, _ in docs] if tags else []
    else:
        hits = []
        for m, f in docs:
            s, ok = 0, True
            for t in terms:
                got = sum(w for k, w in _W.items() if t in f[k])
                if got == 0:
                    ok = False
                    break
                s += got
            if ok:
                hits.append((s, m))
        if not hits:                       # 退化：二字组合，至少命中一半
            grams = {t[i:i + 2] for t in terms for i in range(len(t) - 1) if not (_STOP & set(t[i:i + 2]))}
            for m, f in docs:
                if not grams:
                    break
                mat = [g for g in grams if any(g in f[k] for k in f)]
                if len(mat) * 2 >= len(grams):
                    hits.append((sum(_W[k] for g in mat for k in f if g in f[k]) / len(grams), m))
    hits.sort(key=lambda x: (-x[0], x[1]["qid"]))
    return len(hits), hits[:limit]


def search_any(root: Path, keywords=(), cats=(), tags=(), vocab=None, limit: int = 8):
    """白话查询用：关键词任一命中(OR)即可，可限定分类(大类或细类编号)和标签(都要命中)。
    没有关键词时，只按分类/标签列出。返回 (命中总数, 前 limit 条[(score, meta)])"""
    terms = [t.lower() for t in keywords or [] if t and t.strip()]
    flts = _tag_filters(vocab, [f"#{t}" for t in tags or []])
    out = []
    for m in all_items(root):
        if cats and not in_cats(m, cats):          # 主分类或关联分类命中都算
            continue
        if flts and not all(_tag_ok(vocab, m, f_) for f_ in flts):
            continue
        s = 1
        if terms:
            f = {"t": f"{m['qid']} {m['title']}".lower(), "g": " ".join(m.get("tags", [])).lower(),
                 "b": m.get("brief", "").lower(), "r": _report_text(m["_dir"]).lower()}
            s = sum(w for t in terms for k, w in _W.items() if t in f[k])
            if s == 0:
                continue
        out.append((s, m))
    out.sort(key=lambda x: (-x[0], x[1]["qid"]))
    return len(out), out[:limit]


def tree_summary(root: Path, tree: list) -> str:
    cnt, pend = {}, 0
    for m in all_items(root):
        if is_pending(root, m):
            pend += 1
        else:
            cnt[m.get("category", "0")] = cnt.get(m.get("category", "0"), 0) + 1
    total = sum(cnt.values())
    out = [f"知识库共 {total} 个问题，待入库 {pend} 个"]
    for mj in tree:
        subs = mj["subs"]
        n = sum(cnt.get(s["code"], 0) for s in subs) if subs else cnt.get(mj["code"], 0)
        out.append(f"{mj['code']} {mj['name']}（{n}）")
        for s in subs:
            out.append(f"  {s['code']} {s['name']}  {cnt.get(s['code'], 0)}")
    return "\n".join(out)


def set_status(root: Path, qid: str, status: str) -> str:
    if status not in STATUSES:
        raise ValueError("状态只能是：" + "、".join(STATUSES))
    m = find_problem(root, qid)
    if not m:
        raise KeyError(qid)
    d = m["_dir"]
    meta = read_meta(d)
    old = meta.get("status", "")
    meta["status"] = status
    write_meta(d, meta)
    rp = d / "报告.json"
    if rp.exists():
        r = json.loads(rp.read_text(encoding="utf-8"))
        r["status"] = status
        rp.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "报告.md").write_text(render_report(r, meta), encoding="utf-8")
    return old


def _save_tree(root: Path, tree: list):
    (root / "分类树.json").write_text(json.dumps(tree, ensure_ascii=False, indent=2), encoding="utf-8")


def add_subcat(root: Path, tree: list, major_code: str, name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 24 or re.search(r'[\\/:*?"<>|\s#]', name):
        raise ValueError("细类名不能为空、不能超过 24 字，也不能含空格或 / \\ : * ? \" < > | #")
    mj, _ = find_cat(tree, major_code)
    if mj is None or mj["code"] == "0":
        raise ValueError(f"大类 {major_code} 不存在（待分类不能加细类）")
    if any(s["name"] == name for x in tree for s in x["subs"]):
        raise ValueError(f"已存在名为「{name}」的细类")
    nums = [int(s["code"].split(".")[1]) for s in mj["subs"]]
    code = f"{mj['code']}.{max(nums, default=0) + 1}"
    mj["subs"].append({"code": code, "name": name})
    _save_tree(root, tree)
    return code


def new_cat_suggestions(root: Path) -> list:
    """待分类(0)里带“建议新增细类”的问题，按建议名称归组。"""
    g = {}
    for m in all_items(root):
        nc = m.get("new_cat")
        if m.get("category") == "0" and isinstance(nc, dict) and nc.get("name"):
            x = g.setdefault(nc["name"], {"name": nc["name"], "major": str(nc.get("major", "")), "reason": nc.get("reason", ""), "qids": []})
            x["qids"].append(m["qid"])
    return list(g.values())


def approve_new_cat(root: Path, tree: list, name: str, major: str | None = None):
    s = next((x for x in new_cat_suggestions(root) if x["name"] == name), None)
    mj = (major or (s["major"] if s else "") or "").strip()
    mj = mj.split(".")[0]
    if not mj:
        raise ValueError("没找到这个建议，请指定大类编号：/同意新类 名称 大类编号")
    code = add_subcat(root, tree, mj, name)
    moved = []
    for q in (s["qids"] if s else []):
        move_problem(root, tree, q, code)
        m = find_problem(root, q)
        meta = read_meta(m["_dir"])
        meta["new_cat"] = None
        write_meta(m["_dir"], meta)
        rp = m["_dir"] / "报告.json"
        if rp.exists():
            (m["_dir"] / "报告.md").write_text(render_report(json.loads(rp.read_text(encoding="utf-8")), meta), encoding="utf-8")
        moved.append(q)
    return code, moved


# ---------- 标签词表 ----------
TAG_GROUPS = ["厂商", "模型", "渠道形态", "接入工具", "错误码", "其他"]
VOCAB_FILE = "标签词表.json"
PENDING_TAGS_FILE = "标签待确认.json"
MAX_TAGS = 12
UNGROUPED = "未归类"          # 旧数据里词表对不上的标签，只在显示时用
_DEFAULT_VOCAB = Path(__file__).resolve().with_name("default_tag_vocab.json")


def _tkey(s) -> str:
    """标签比较用的键：忽略大小写和多余空格。"""
    return re.sub(r"\s+", " ", str(s).strip()).lower()


class Vocab:
    """标签词表：规范名 + 别名 + 所属类；模型可挂厂商。"""

    def __init__(self, obj: dict):
        self.obj = obj
        self.groups = list(obj.get("groups") or TAG_GROUPS)
        self.tags = {t["name"]: t for t in obj.get("tags", [])}
        self.index = {}
        for t in self.tags.values():
            for k in [t["name"], *t.get("aliases", [])]:
                self.index.setdefault(_tkey(k), t["name"])

    def canon(self, raw):
        return self.index.get(_tkey(raw))

    def group_of(self, name):
        t = self.tags.get(name)
        return t["group"] if t else None

    def vendor_of(self, name):
        t = self.tags.get(name)
        return t.get("vendor") if t else None

    def models_of(self, vendor: str) -> list:
        return [n for n, t in self.tags.items() if t.get("vendor") == vendor]

    def group_match(self, s):
        """类名匹配：完全相同，或唯一的前缀（'渠道' → '渠道形态'）。"""
        s = str(s).strip()
        if s in self.groups:
            return s
        c = [g for g in self.groups if s and g.startswith(s)]
        return c[0] if len(c) == 1 else None

    def normalize(self, raw_tags, limit: int = MAX_TAGS):
        """原始标签 → (规范名列表, 词表里没有的新标签)。模型自动补厂商；按类顺序排列，超出上限从末尾截。"""
        seen, unknown, ukeys = [], [], set()
        for r in raw_tags or []:
            s = re.sub(r"\s+", " ", str(r).strip())
            if not s:
                continue
            c = self.canon(s)
            if c:
                if c not in seen:
                    seen.append(c)
            elif _tkey(s) not in ukeys and len(s) <= 30 and "#" not in s and len(unknown) < 20:
                ukeys.add(_tkey(s))
                unknown.append(s)
        for c in list(seen):
            v = self.vendor_of(c)
            if v and v in self.tags and v not in seen:
                seen.append(v)
        order = {g: i for i, g in enumerate(self.groups)}
        seen.sort(key=lambda n: order.get(self.group_of(n), len(order)))   # 稳定排序，同类保持原顺序
        return seen[:limit], unknown

    def matches(self, item_tags, name: str) -> bool:
        """问题的标签里有没有 name；name 是厂商时，它名下任何模型也算。"""
        cs = {self.canon(x) or x for x in item_tags or []}
        if name in cs:
            return True
        return self.group_of(name) == "厂商" and any(self.vendor_of(c) == name for c in cs)

    def by_group(self, tags):
        """标签 → [(类, [名])]，按类顺序；词表对不上的归「未归类」。"""
        buckets = {}
        for t in tags or []:
            c = self.canon(t)
            buckets.setdefault(self.group_of(c) if c else UNGROUPED, []).append(c or t)
        return [(g, buckets[g]) for g in [*self.groups, UNGROUPED] if g in buckets]

    def prompt_text(self) -> str:
        """给大模型看的词表：每类列出规范名。"""
        lines = []
        for g in self.groups:
            names = [n for n, t in self.tags.items() if t["group"] == g]
            if names:
                lines.append(f"{g}：{'、'.join(names)}")
        return "\n".join(lines)


def load_vocab(root: Path, create: bool = False) -> Vocab:
    """读知识库里的词表；还没有时用插件自带的默认词表（create=True 时顺便落盘）。网页只读，不要传 create。"""
    p = root / VOCAB_FILE
    if p.exists():
        return Vocab(json.loads(p.read_text(encoding="utf-8")))
    obj = json.loads(_DEFAULT_VOCAB.read_text(encoding="utf-8"))
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    return Vocab(obj)


def save_vocab(root: Path, v: Vocab):
    (root / VOCAB_FILE).write_text(json.dumps(v.obj, ensure_ascii=False, indent=1), encoding="utf-8")


def tags_text(v: Vocab, tags) -> str:
    """'厂商：Anthropic　模型：Claude 5　渠道形态：号池'，没有标签返回 '无'。"""
    parts = [f"{g}：{'、'.join(ns)}" for g, ns in v.by_group(tags)]
    return "　".join(parts) or "无"


def set_item_tags(d: Path, tags: list):
    """改一个问题的标签：元数据、报告.json、报告.md 三处保持一致。"""
    meta = read_meta(d)
    meta["tags"] = list(tags)
    write_meta(d, meta)
    rp = d / "报告.json"
    if rp.exists():
        r = json.loads(rp.read_text(encoding="utf-8"))
        r["tags"] = list(tags)
        rp.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "报告.md").write_text(render_report(r, meta), encoding="utf-8")


# ---------- 待确认的新标签 ----------
def load_pending_tags(root: Path) -> dict:
    p = root / PENDING_TAGS_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def _save_pending_tags(root: Path, d: dict):
    p = root / PENDING_TAGS_FILE
    if d:
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    elif p.exists():
        p.unlink()


def add_pending_tags(root: Path, qid: str, names: list):
    if not names:
        return
    d = load_pending_tags(root)
    for n in names:
        key = next((k for k in d if _tkey(k) == _tkey(n)), n)
        q = d.setdefault(key, {"qids": []})["qids"]
        if qid not in q:
            q.append(qid)
    _save_pending_tags(root, d)


def retarget_pending_tags(root: Path, old_qid: str, new_qid: str):
    """问题被合并掉后，它名下的待确认标签改挂到保留的那个问题上。"""
    d = load_pending_tags(root)
    for v in d.values():
        if old_qid in v["qids"]:
            v["qids"] = [new_qid if q == old_qid else q for q in v["qids"]]
            v["qids"] = list(dict.fromkeys(v["qids"]))
    _save_pending_tags(root, d)


def _attach_tag(root: Path, v: Vocab, qids: list, tag: str) -> list:
    done = []
    for q in qids:
        m = find_problem(root, q)
        if not m:
            continue
        tags, _ = v.normalize([*m.get("tags", []), tag])
        set_item_tags(m["_dir"], tags)
        done.append(q)
    return done


def split_group_args(v: Vocab, toks: list):
    """'Claude 4.8 模型 Anthropic' → ('Claude 4.8', '模型', 'Anthropic')；没写类则 group 为 None。"""
    for i, t in enumerate(toks):
        if i >= 1 and v.group_match(t):
            return " ".join(toks[:i]), v.group_match(t), " ".join(toks[i + 1:])
    return " ".join(toks), None, ""


def approve_new_tag(root: Path, v: Vocab, name: str, group: str, vendor: str = ""):
    """把待确认里的新标签正式加入词表，并补到相关问题上。返回 (规范名, 已补的问题编号)。"""
    d = load_pending_tags(root)
    key = next((k for k in d if _tkey(k) == _tkey(name)), None)
    if key is None:
        raise ValueError(f"待确认里没有「{name}」。先发 /同意新标签 看看有哪些。")
    if v.canon(key):
        raise ValueError(f"「{key}」已在词表里（规范名：{v.canon(key)}）。")
    g = v.group_match(group)
    if not g:
        raise ValueError("类要写：" + "、".join(v.groups))
    vend = ""
    if vendor:
        vend = v.canon(vendor)
        if not vend or v.group_of(vend) != "厂商":
            raise ValueError(f"「{vendor}」不是词表里的厂商。")
    ent = {"name": key, "group": g, "aliases": []}
    if g == "模型" and vend:
        ent["vendor"] = vend
    v.obj.setdefault("tags", []).append(ent)
    v.__init__(v.obj)
    save_vocab(root, v)
    done = _attach_tag(root, v, d[key]["qids"], key)
    d.pop(key)
    _save_pending_tags(root, d)
    return key, done


def add_alias(root: Path, v: Vocab, name: str, alias: str):
    """给已有标签补一个别名。返回 (规范名, 顺带补上标签的问题编号)。"""
    c = v.canon(name)
    if not c:
        raise ValueError(f"词表里没有「{name}」。")
    alias = re.sub(r"\s+", " ", alias.strip())
    if not alias or len(alias) > 30 or "#" in alias:
        raise ValueError("别名不能为空、不能超过 30 字、不能含 #。")
    other = v.canon(alias)
    if other:
        raise ValueError(f"「{alias}」已经是「{other}」的名字或别名。")
    v.tags[c].setdefault("aliases", []).append(alias)
    v.__init__(v.obj)
    save_vocab(root, v)
    d, done = load_pending_tags(root), []
    key = next((k for k in d if _tkey(k) == _tkey(alias)), None)
    if key:
        done = _attach_tag(root, v, d.pop(key)["qids"], c)
        _save_pending_tags(root, d)
    return c, done


def split_alias_args(v: Vocab, toks: list):
    """'Claude 4.8 c48' → ('Claude 4.8', 'c48')：从右往左找最长的、词表里存在的规范名/别名。"""
    for j in range(len(toks) - 1, 0, -1):
        if v.canon(" ".join(toks[:j])):
            return " ".join(toks[:j]), " ".join(toks[j:])
    return (toks[0], " ".join(toks[1:])) if len(toks) > 1 else ("", "")


NO_MODEL = "（未指明模型）"
NO_VENDOR = "（未指明厂商）"


def tag_hierarchy(root: Path, v: Vocab) -> dict:
    """厂商 → 模型 → 错误码 的三层视图，由已入库问题的标签推出，不改词表结构。
    返回 {厂商: {模型: {"n": 问题数, "codes": {错误码: 问题数}}}}。
    只有厂商没有模型的问题归到“（未指明模型）”；只有错误码、既无模型也无厂商的归到“（未指明厂商）”。"""
    out = {}

    def node(vend, model):
        return out.setdefault(vend, {}).setdefault(model, {"n": 0, "codes": {}})

    for m in all_items(root):
        if is_pending(root, m):
            continue
        tags = {v.canon(x) or x for x in m.get("tags", [])}
        models = sorted(t for t in tags if v.group_of(t) == "模型")
        vendors = sorted(t for t in tags if v.group_of(t) == "厂商")
        codes = sorted(t for t in tags if v.group_of(t) == "错误码")
        if not (models or vendors or codes):
            continue                                  # 一个厂商/模型/错误码都没有的问题，层级里没有位置
        targets = [(v.vendor_of(mo) or NO_VENDOR, mo) for mo in models]
        if not targets:
            targets = [(vd, NO_MODEL) for vd in vendors] or [(NO_VENDOR, NO_MODEL)]
        for vend, mo in targets:
            n = node(vend, mo)
            n["n"] += 1
            for c in codes:
                n["codes"][c] = n["codes"].get(c, 0) + 1
    return out


def tag_hierarchy_text(h: dict) -> str:
    lines = ["厂商 → 模型 → 错误码（括号里是问题数）"]
    for vend, models in sorted(h.items(), key=lambda x: -sum(n["n"] for n in x[1].values())):
        lines.append(f"【{vend}】{sum(n['n'] for n in models.values())}")
        for mo, n in sorted(models.items(), key=lambda x: -x[1]["n"]):
            cs = "、".join(f"{c}({k})" for c, k in sorted(n["codes"].items(), key=lambda x: (-x[1], x[0])))
            lines.append(f"  {mo}({n['n']})" + (f"：{cs}" if cs else ""))
    return "\n".join(lines) if len(lines) > 1 else "还没有带厂商/模型/错误码标签的问题。"


def tag_stats(root: Path, v: Vocab) -> dict:
    """{类: {规范名: 问题数}}，只统计已入库的问题；词表对不上的归「未归类」。"""
    out = {}
    for m in all_items(root):
        if is_pending(root, m):
            continue
        for t in {v.canon(x) or x for x in m.get("tags", [])}:
            g = v.group_of(t) if t in v.tags else UNGROUPED
            out.setdefault(g, {})
            out[g][t] = out[g].get(t, 0) + 1
    return out
