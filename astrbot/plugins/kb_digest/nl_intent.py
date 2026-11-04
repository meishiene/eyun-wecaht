"""白话指令：唤醒词识别、大模型意图解析结果的校验、转成现有斜杠指令。不依赖 AstrBot，可独立测试。

约定：
  - 只有以「@11」开头的消息才当指令，其余仍当素材收集（素材里常有“帮我查一下”这类话，不能误触发）。
  - 大模型只负责把一句话翻成固定的意图和参数；参数一律在这里再校验，校验不过就当没听懂，绝不照单全收。
  - 会改动知识库的意图（WRITE_INTENTS）先复述、等用户回「是」才执行。
"""
import json
import re

try:
    from . import kb_core as kb
except ImportError:  # 本地测试
    import kb_core as kb

WAKE_NAME = "11"
# @11 / ＠11，后面可直接接中文，也可跟空格或逗号；@110 之类不算
_WAKE = re.compile(r"^\s*[@＠]\s*" + re.escape(WAKE_NAME) + r"(?!\d)\s*[,，:：]?\s*")
YES = {"是", "是的", "确认", "好", "好的", "可以", "执行", "y", "yes", "ok"}
NO = {"否", "不", "不用", "不用了", "取消", "算了", "no", "n"}
CONFIRM_TTL = 120          # 秒

WRITE_INTENTS = {"clear", "commit", "set_status", "recat", "merge", "approve_cat", "approve_tag", "add_alias"}
QID_RE = re.compile(r"^(?:Q\d{8}-\d{3}|\d{1,4})$", re.I)

INTENT_PROMPT = """你是微信机器人「{name}」的指令解析器。用户在私聊里对机器人说了一句话，请判断他想做什么，只输出一个 JSON 对象：
{{"intent": "意图名", "args": {{...}}, "say": "仅当 intent 为 unknown 时，用一句话告诉用户你没听懂什么、可以怎么说"}}

意图和参数（args 里只写需要的，没有就省略）：
- digest      把刚收集的内容整理成一个待入库问题。args.title 可选（用户明确说了标题才填）
- commit      把待入库的问题归档入库
- count       看已收集了多少、待入库多少
- clear       丢弃还没整理的内容
- pending     看待入库列表
- show        看某个问题的详情。args.qid（编号或序号，如 41、Q20261103-001）
- tree        看分类树和各类数量
- tags        看标签分类。args.group 可选，必须是下面的某个类名
- search      查问题。args.keywords：3~6 个与问题相关的中文短词（含同义词、相近说法，如“计费”可带上“扣费、价格、倍率”），
              args.categories：可选，下面分类树里的编号（大类如 "2" 或细类如 "2.3"），用户说的话明显落在某类时才填，
              args.tags：可选，必须是下面标签词表里的规范名
- set_status  改状态。args.qid，args.status（{statuses} 之一）
- recat       改归类。args.qid，args.code（分类树里的细类编号）
- merge       把问题 A 合并进已入库的问题 B。args.src，args.dst
- approve_cat 同意建议新增的细类。args.name 可选，args.major 可选
- approve_tag 同意待确认的新标签。args.name，args.group（{groups} 之一），args.vendor 可选
- add_alias   给标签补别名。args.name（规范名），args.alias
- help        问怎么用、能做什么
- unknown     以上都不是，或信息不够

规则：1）拿不准就选 unknown，不要猜；2）用户在问“有什么/有哪些/找一下/查一下……的问题”，是 search；
3）不要编造编号和分类；4）只输出 JSON，不要任何别的文字。

分类树：
{tree}

标签词表（每类一行）：
{tags}

用户说的话：{text}
"""


def strip_wake(text: str):
    """返回 (是否带唤醒词, 去掉唤醒词后的内容)。"""
    m = _WAKE.match(text or "")
    return (True, (text or "")[m.end():].strip()) if m else (False, (text or "").strip())


def confirm_answer(text: str):
    """'是' → True，'取消' → False，其他 → None。"""
    t = (text or "").strip().strip("。.!！~ ").lower()
    return True if t in YES else (False if t in NO else None)


def build_prompt(text: str, tree: list, vocab) -> str:
    return INTENT_PROMPT.format(name="@" + WAKE_NAME, statuses="、".join(kb.STATUSES), groups="、".join(vocab.groups),
                                tree=kb.tree_text(tree), tags=vocab.prompt_text(), text=text.strip())


def _s(v, n=40):
    v = re.sub(r"\s+", " ", str(v or "")).strip()
    return v[:n]


def validate(obj, tree: list, vocab):
    """校验大模型给的解析结果。通过返回 {"intent", "args"}（args 已清洗）；不通过返回 {"intent": "unknown", "say": ...}。"""
    def unknown(say=""):
        return {"intent": "unknown", "args": {}, "say": _s(say, 120)}

    if not isinstance(obj, dict):
        return unknown()
    name = str(obj.get("intent") or "").strip()
    a = obj.get("args") if isinstance(obj.get("args"), dict) else {}
    if name == "unknown" or not name:
        return unknown(obj.get("say"))
    if name in ("digest",):
        return {"intent": name, "args": {"title": _s(a.get("title"))}}
    if name in ("commit", "count", "clear", "pending", "tree", "help"):
        return {"intent": name, "args": {}}
    if name == "show":
        q = _s(a.get("qid"), 20)
        return {"intent": name, "args": {"qid": q}} if QID_RE.match(q) else unknown("没看出要看哪个问题，请带上编号，比如 41")
    if name == "tags":
        g = vocab.group_match(_s(a.get("group"))) if a.get("group") else None
        return {"intent": name, "args": {"group": g or ""}}
    if name == "search":
        kws = [k for k in (_s(x, 12) for x in (a.get("keywords") or []) if isinstance(x, (str, int))) if k][:8]
        cats = [c for c in (_s(x, 6) for x in (a.get("categories") or []) if isinstance(x, (str, int, float))) if kb.find_cat(tree, c)[0]][:4]
        tags = [t for t in (vocab.canon(_s(x, 30)) for x in (a.get("tags") or []) if isinstance(x, str)) if t][:4]
        if not (kws or cats or tags):
            return unknown("想查什么？可以说“找一下限流的问题”“有哪些 Gemini 的问题”")
        return {"intent": name, "args": {"keywords": kws, "categories": cats, "tags": tags}}
    if name == "set_status":
        q, st = _s(a.get("qid"), 20), _s(a.get("status"), 10)
        if QID_RE.match(q) and st in kb.STATUSES:
            return {"intent": name, "args": {"qid": q, "status": st}}
        return unknown("改状态要说清哪个问题、改成什么：" + "、".join(kb.STATUSES))
    if name == "recat":
        q, c = _s(a.get("qid"), 20), _s(a.get("code"), 6)
        if QID_RE.match(q) and kb.find_cat(tree, c)[1] is not None:
            return {"intent": name, "args": {"qid": q, "code": c}}
        return unknown("改归类要说清哪个问题、改到哪个细类（细类编号见 /树）")
    if name == "merge":
        s_, d_ = _s(a.get("src"), 20), _s(a.get("dst"), 20)
        if QID_RE.match(s_) and QID_RE.match(d_):
            return {"intent": name, "args": {"src": s_, "dst": d_}}
        return unknown("合并要说清把哪个并进哪个")
    if name == "approve_cat":
        return {"intent": name, "args": {"name": _s(a.get("name"), 24), "major": _s(a.get("major"), 4)}}
    if name == "approve_tag":
        n, g = _s(a.get("name"), 30), vocab.group_match(_s(a.get("group")))
        if n and g:
            v = vocab.canon(_s(a.get("vendor"), 30)) if a.get("vendor") else ""
            return {"intent": name, "args": {"name": n, "group": g, "vendor": v or ""}}
        return unknown("同意新标签要说清标签名和类：" + "、".join(vocab.groups))
    if name == "add_alias":
        n, al = _s(a.get("name"), 30), _s(a.get("alias"), 30)
        if n and al:
            return {"intent": name, "args": {"name": n, "alias": al}}
        return unknown("加别名要说清给哪个标签加什么别名")
    return unknown()


def describe(it: dict, pend: int = 0, buffered: str = "") -> str:
    """用大白话复述要做的事，用于确认。"""
    n, a = it["intent"], it["args"]
    return {
        "clear": f"丢弃还没整理的内容{('（' + buffered + '）') if buffered else ''}",
        "commit": f"把 {pend} 个待入库问题归档入库" if pend else "把待入库问题归档入库（目前没有）",
        "set_status": f"把 {a.get('qid')} 的状态改成「{a.get('status')}」",
        "recat": f"把 {a.get('qid')} 归到细类 {a.get('code')}",
        "merge": f"把 {a.get('src')} 合并进 {a.get('dst')}（{a.get('src')} 会消失，原文并入）",
        "approve_cat": f"同意新增细类{('「' + a['name'] + '」') if a.get('name') else ''}",
        "approve_tag": f"把「{a.get('name')}」加入标签「{a.get('group')}」" + (f"（厂商 {a['vendor']}）" if a.get("vendor") else ""),
        "add_alias": f"给标签「{a.get('name')}」加别名「{a.get('alias')}」",
    }.get(n, n)


def command_text(it: dict) -> str:
    """意图 → 现有斜杠指令的正文（不含 /），交给原来的处理函数执行。search 不走这里。"""
    n, a = it["intent"], it["args"]
    return {
        "digest": lambda: ("整理 " + a.get("title", "")).strip(),
        "commit": lambda: "入库", "count": lambda: "条数", "clear": lambda: "清空", "pending": lambda: "待入库",
        "tree": lambda: "树", "help": lambda: "帮助",
        "show": lambda: f"看 {a['qid']}",
        "tags": lambda: ("标签 " + a.get("group", "")).strip(),
        "set_status": lambda: f"状态 {a['qid']} {a['status']}",
        "recat": lambda: f"改类 {a['qid']} {a['code']}",
        "merge": lambda: f"合并 {a['src']} {a['dst']}",
        "approve_cat": lambda: ("同意新类 " + a.get("name", "") + " " + a.get("major", "")).strip(),
        "approve_tag": lambda: f"同意新标签 {a['name']} {a['group']} {a.get('vendor', '')}".strip(),
        "add_alias": lambda: f"加别名 {a['name']} {a['alias']}",
    }[n]()
