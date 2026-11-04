#!/usr/bin/env python3
"""知识库只读网页。仅用标准库，只读取知识库目录，不写任何文件。

环境变量：KB_ROOT 知识库目录；KB_PLUGIN_DIR kb_core.py 所在目录；KB_BIND 监听地址；KB_PORT 端口；
KB_WEB_PASSWORD 访问密码（不设则不要求登录）。
"""
import base64
import hmac
import html
import json
import mimetypes
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, os.environ.get("KB_PLUGIN_DIR") or str(_HERE.parent / "plugins" / "kb_digest"))
import kb_core as kb  # noqa: E402

esc = html.escape
QID_RE = re.compile(r"^Q\d{8}-\d{3}$")
IMG_OK = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
STATUS_CLS = {"已解决": "ok", "未解决": "bad", "待跟进": "warn", "需人工核验": "info"}

CSS = """
:root{--fg:#1f2328;--mut:#656d76;--bd:#d0d7de;--bg:#f6f8fa;--ac:#0969da}
*{box-sizing:border-box}body{margin:0;font:15px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:var(--fg);background:#fff}
a{color:var(--ac);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--bd);padding:10px 20px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header b{font-size:16px}header form{margin-left:auto;display:flex;gap:6px}
input[type=search]{padding:6px 10px;border:1px solid var(--bd);border-radius:6px;min-width:240px;font:inherit}
button{padding:6px 14px;border:1px solid var(--bd);border-radius:6px;background:var(--bg);font:inherit;cursor:pointer}
main{max-width:1100px;margin:0 auto;padding:20px}
h1{font-size:22px;margin:.2em 0 .6em}h2{font-size:18px;margin:1.6em 0 .5em;padding-bottom:4px;border-bottom:1px solid var(--bd)}h3{font-size:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.card{border:1px solid var(--bd);border-radius:8px;padding:12px 14px}.card h3{margin:0 0 6px}.card li{list-style:none;display:flex;justify-content:space-between;gap:8px}
.card ul{margin:0;padding:0}.n{color:var(--mut)}
table{border-collapse:collapse;width:100%;margin:10px 0}th,td{border:1px solid var(--bd);padding:6px 10px;text-align:left;vertical-align:top}th{background:var(--bg)}
.tw{overflow-x:auto}
.badge{display:inline-block;padding:0 8px;border-radius:10px;font-size:12px;line-height:20px;border:1px solid}
.ok{color:#1a7f37;border-color:#1a7f37;background:#dafbe1}.bad{color:#cf222e;border-color:#cf222e;background:#ffebe9}
.warn{color:#9a6700;border-color:#9a6700;background:#fff8c5}.info{color:#0550ae;border-color:#0550ae;background:#ddf4ff}.pend{color:#6639ba;border-color:#6639ba;background:#fbefff}
.tag{display:inline-block;margin:0 4px 4px 0;padding:0 8px;border-radius:10px;background:var(--bg);border:1px solid var(--bd);font-size:12px;color:var(--fg)}
.meta{color:var(--mut);font-size:13px}.brief{background:var(--bg);border-left:4px solid var(--ac);padding:10px 14px;border-radius:0 6px 6px 0}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:6px;padding:10px 12px;overflow-x:auto;white-space:pre-wrap;word-break:break-word}
code{background:var(--bg);padding:1px 5px;border-radius:4px;font-size:90%}pre code{background:none;padding:0}
blockquote{margin:8px 0;padding:2px 14px;border-left:4px solid var(--bd);color:var(--mut)}
img.raw{max-width:100%;border:1px solid var(--bd);border-radius:6px;margin:6px 0;display:block}
.cap{font-size:13px;color:var(--mut);margin:14px 0 4px}.tree{margin:.4em 0;padding-left:1.2em}
.box{padding:8px 14px;border-radius:6px;margin:0 0 12px;border:1px solid}.box.good{background:#dafbe1;border-color:#1a7f37;color:#1a7f37}.box.bad{background:#ffebe9;border-color:#cf222e;color:#cf222e;font-weight:600}
footer{color:var(--mut);font-size:12px;text-align:center;padding:24px}
"""


# ---------------- Markdown（够用的子集：标题/列表/表格/代码/引用/粗体/行内码/链接） ----------------
def _inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
    return re.sub(r"&lt;br\s*/?&gt;", "<br>", s)


def _split_row(line: str):
    cells, cur, tick = [], "", False
    for ch in line.strip().strip("|"):
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            cells.append(cur.strip())
            cur = ""
        else:
            cur += ch
    cells.append(cur.strip())
    return cells


def md_to_html(text: str) -> str:
    L, out, i = text.split("\n"), [], 0
    special = re.compile(r"^(```|#{1,6}\s|>|\s*[-*]\s+|\s*\d+[.)]\s+|\s*\|)")
    while i < len(L):
        l = L[i]
        if l.startswith("```"):
            j, buf = i + 1, []
            while j < len(L) and not L[j].startswith("```"):
                buf.append(L[j]); j += 1
            out.append("<pre><code>" + esc("\n".join(buf)) + "</code></pre>")
            i = j + 1
        elif l.strip().startswith("|") and i + 1 < len(L) and re.fullmatch(r"\s*\|?[\s:\-|]+\|?\s*", L[i + 1]) and "-" in L[i + 1]:
            head, j, rows = _split_row(l), i + 2, []
            while j < len(L) and L[j].strip().startswith("|"):
                rows.append(_split_row(L[j])); j += 1
            t = "<div class=tw><table><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in head) + "</tr>"
            for r in rows:
                t += "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
            out.append(t + "</table></div>")
            i = j
        elif (m := re.match(r"^(#{1,6})\s+(.*)", l)):
            n = min(len(m.group(1)) + 1, 6)
            out.append(f"<h{n}>{_inline(m.group(2))}</h{n}>"); i += 1
        elif l.startswith(">"):
            buf = []
            while i < len(L) and L[i].startswith(">"):
                buf.append(_inline(L[i].lstrip("> ").rstrip())); i += 1
            out.append("<blockquote>" + "<br>".join(buf) + "</blockquote>")
        elif re.match(r"^\s*[-*]\s+", l):
            buf = []
            while i < len(L) and re.match(r"^\s*[-*]\s+", L[i]):
                buf.append("<li>" + _inline(re.sub(r"^\s*[-*]\s+", "", L[i])) + "</li>"); i += 1
            out.append("<ul>" + "".join(buf) + "</ul>")
        elif re.match(r"^\s*\d+[.)]\s+", l):
            buf = []
            while i < len(L) and re.match(r"^\s*\d+[.)]\s+", L[i]):
                buf.append("<li>" + _inline(re.sub(r"^\s*\d+[.)]\s+", "", L[i])) + "</li>"); i += 1
            out.append("<ol>" + "".join(buf) + "</ol>")
        elif l.strip() in ("---", "***", "___"):
            out.append("<hr>"); i += 1
        elif not l.strip():
            i += 1
        else:
            buf = []
            while i < len(L) and L[i].strip() and not special.match(L[i]) and L[i].strip() not in ("---", "***"):
                buf.append(_inline(L[i])); i += 1
            if not buf:                      # 防止死循环：当作普通行输出
                buf.append(_inline(L[i])); i += 1
            out.append("<p>" + "<br>".join(buf) + "</p>")
    return "\n".join(out)


def render_raw(text: str, qid: str) -> str:
    """原文：逐字显示（HTML 转义，不做 Markdown 解析），仅把 [图片：images/xx] 换成 <img>。"""
    parts, last, out = re.split(r"(\[图片：images/[\w.\-]+\])", text), None, []
    for p in parts:
        m = re.fullmatch(r"\[图片：images/([\w.\-]+)\]", p)
        if m:
            u = f"/img/{qid}/{quote(m.group(1))}"
            out.append(f'</pre><a href="{u}" target="_blank"><img class=raw src="{u}" alt="图片" loading=lazy></a><pre>')
        else:
            out.append(esc(p))
    return "<pre>" + "".join(out) + "</pre>"


# ---------------- 数据 ----------------
class Store:
    def __init__(self, root: Path, backup_status=""):
        self.root = Path(root)
        self.backup_status = backup_status

    def tree(self):
        try:
            return json.loads((self.root / "分类树.json").read_text(encoding="utf-8"))
        except Exception:
            return kb._default_tree_obj()

    def items(self):
        return kb.all_items(self.root)

    def vocab(self):
        return kb.load_vocab(self.root)        # 只读：知识库里没有词表时用插件自带的默认词表，不落盘

    def get(self, qid: str):
        return next((m for m in self.items() if m["qid"] == qid), None)

    def sub_name(self, tree, code):
        s = kb.find_cat(tree, code)[1]
        return s["name"] if s else ("待分类" if code == "0" else code)


# ---------------- 页面 ----------------
def page(title: str, body: str, q: str = "") -> bytes:
    return (f"<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)} · 中转站知识库</title><style>{CSS}</style>"
            f"<header><b><a href='/'>📚 中转站知识库</a></b><a href='/'>分类</a><a href='/manual'>排查手册</a><a href='/tags'>标签</a>"
            f"<form action='/search'><input type=search name=q value='{esc(q, quote=True)}' placeholder='搜索：关键词 或 #标签'><button>搜索</button></form></header>"
            f"<main>{body}</main><footer>只读页面 · 修改请在微信里用指令</footer></html>").encode("utf-8")


def badge(st):
    return f"<span class='badge {STATUS_CLS.get(st, 'info')}'>{esc(st or '')}</span>"


def tags_html(tags):
    return "".join(f"<a class=tag href='/search?q={quote('#' + t)}'>{esc(t)}</a>" for t in tags or [])


def row(m, tree, st):
    pend = "<span class='badge pend'>待入库</span> " if kb.is_pending(st.root, m) else ""
    pend += "<span class='badge warn'>非常态</span> " if m.get("routine") == "非常态" else ""
    pend += "<span class='badge info'>待审</span> " if kb.needs_review(m) else ""
    return (f"<tr><td><a href='/q/{m['qid']}'>{m['qid']}</a></td><td>{pend}<a href='/q/{m['qid']}'>{esc(m['title'])}</a>"
            f"<div class=meta>{esc((m.get('brief') or '').replace('`', '')[:90])}</div></td><td>{esc(m.get('category', '0'))} {esc(st.sub_name(tree, m.get('category', '0')))}</td>"
            f"<td>{badge(m.get('status'))}</td><td>{tags_html(m.get('tags', [])[:4])}</td></tr>")


def table(ms, tree, st):
    if not ms:
        return "<p class=meta>没有内容。</p>"
    return ("<div class=tw><table><tr><th>编号</th><th>标题 / 简要</th><th>归类</th><th>状态</th><th>标签</th></tr>"
            + "".join(row(m, tree, st) for m in ms) + "</table></div>")


def backup_box(path, now=None) -> str:
    """首页顶部的备份状态条：最近一次成功的备份时间；超过 36 小时没成功备份就告警。"""
    import time as _t
    now = now if now is not None else _t.time()
    if not path:
        return ""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "<div class='box bad'>⚠️ 还没有备份记录：每日备份尚未运行过。</div>"
    except Exception:
        return "<div class='box bad'>⚠️ 备份状态文件无法读取，请检查备份任务。</div>"
    last = d.get("last_ok_epoch")
    if not last:
        return f"<div class='box bad'>⚠️ 从未成功备份。最近一次尝试：{esc(str(d.get('time', '')))}，错误：{esc(str(d.get('error') or '未知'))}</div>"
    age_h = (now - last) / 3600
    when = esc(str(d.get("last_ok_time", "")))
    if age_h > 36 or not d.get("ok"):
        why = f"最近一次尝试（{esc(str(d.get('time', '')))}）失败：{esc(str(d.get('error') or ''))}。" if not d.get("ok") else ""
        return f"<div class='box bad'>⚠️ 备份异常：已 {age_h:.0f} 小时没有成功备份（上次成功 {when}）。{why}</div>"
    extra = f"，共 {d['kept']} 份，{d['size'] / 1024:.0f} KB，含 {d['problems']} 个问题" if d.get("size") else ""
    return f"<div class='box good'>💾 最近备份 {when}（{age_h:.0f} 小时前）{extra}</div>"


def view_home(st):
    tree, items = st.tree(), st.items()
    done = [m for m in items if not kb.is_pending(st.root, m)]
    pend = [m for m in items if kb.is_pending(st.root, m)]
    cnt = {}
    for m in done:
        cnt[m.get("category", "0")] = cnt.get(m.get("category", "0"), 0) + 1
    bys = {}
    for m in done:
        bys[m.get("status", "")] = bys.get(m.get("status", ""), 0) + 1
    body = [f"<h1>知识库</h1>{backup_box(st.backup_status)}<p>共 <b>{len(done)}</b> 个问题" + (f"，待入库 <b>{len(pend)}</b>" if pend else "")
            + "　" + "　".join(f"{badge(k)} {v}" for k, v in bys.items()) + "</p><div class=grid>"]
    for mj in tree:
        subs = mj["subs"]
        n = sum(cnt.get(s["code"], 0) for s in subs) if subs else cnt.get(mj["code"], 0)
        li = "".join(f"<li><a href='/c/{s['code']}'>{esc(s['code'])} {esc(s['name'])}</a><span class=n>{cnt.get(s['code'], 0)}</span></li>" for s in subs)
        if not subs:
            li = f"<li><a href='/c/{mj['code']}'>查看全部</a><span class=n>{n}</span></li>"
        body.append(f"<div class=card><h3><a href='/c/{mj['code']}'>{esc(mj['code'])} {esc(mj['name'])}</a> <span class=n>（{n}）</span></h3><ul>{li}</ul></div>")
    body.append("</div>")
    if pend:
        body.append("<h2>待入库</h2>" + table(sorted(pend, key=lambda m: m["qid"]), tree, st))
    recent = sorted(done, key=lambda m: (m.get("committed_at") or m.get("created_at") or "", m["qid"]), reverse=True)[:10]
    body.append("<h2>最近入库</h2>" + table(recent, tree, st))
    return "知识库", "".join(body)


def view_cat(st, code):
    tree = st.tree()
    mj, sub = kb.find_cat(tree, code)
    if mj is None:
        return None
    if sub is None:
        codes = {s["code"] for s in mj["subs"]} or {mj["code"]}
        ms, name = [m for m in st.items() if m.get("category", "0") in codes], f"{mj['code']} {mj['name']}"
    else:
        ms, name = [m for m in st.items() if m.get("category") == sub["code"]], f"{sub['code']} {sub['name']}"
        rel = [m for m in st.items() if sub["code"] in [str(c) for c in m.get("related_cats") or []]]   # 主分类在别处、这里是关联
        ms += rel
    ms.sort(key=lambda m: m["qid"])
    crumb = f"<p class=meta><a href='/'>全部分类</a> › {esc(mj['code'])} {esc(mj['name'])}" + (f" › {esc(sub['code'])} {esc(sub['name'])}" if sub else "") + "</p>"
    return name, f"{crumb}<h1>{esc(name)} <span class=n>（{len(ms)}）</span></h1>" + table(ms, tree, st)


def view_problem(st, qid):
    m = st.get(qid)
    if not m:
        return None
    tree, d = st.tree(), m["_dir"]
    rd = lambda n: (d / n).read_text(encoding="utf-8") if (d / n).exists() else ""
    cat = m.get("category", "0")
    mj = kb.find_cat(tree, cat)[0]
    crumb = (f"<p class=meta><a href='/'>全部分类</a> › " + (f"<a href='/c/{mj['code']}'>{esc(mj['code'])} {esc(mj['name'])}</a> › " if mj else "")
             + f"<a href='/c/{esc(cat)}'>{esc(cat)} {esc(st.sub_name(tree, cat))}</a></p>")
    head = (f"<h1>{esc(m['title'])}</h1><p>{badge(m.get('status'))} "
            + ("<span class='badge pend'>待入库</span> " if kb.is_pending(st.root, m) else "")
            + f"<span class=meta>{m['qid']} · 口径：{esc(m.get('source', ''))} · 入库：{esc(m.get('committed_at') or m.get('created_at') or '')}</span></p>"
            f"<p>{tags_html(m.get('tags', []))}</p>"
            f"<p class=meta>归属侧：<b>{esc(kb.norm_enum(m.get('owner_side'), kb.OWNER_SIDES))}</b> · 类型：<b>{esc(kb.norm_enum(m.get('routine'), kb.ROUTINES))}</b> · 审核：<b>{esc(kb.audit_label(m))}</b></p>")
    notes = ""
    if m.get("routine") == "非常态":
        notes += f"<p>{esc(kb.SUPPORT_HINT)}</p>"
    if m.get("ai_filled"):
        notes += "<p>⚠️ 原理说明、归属侧、类型由 AI 补全，待人工审核（在微信里用 /审）。</p>"
    if m.get("related_cats"):
        notes += "<p>🔗 同时牵涉：" + "、".join(f"<a href='/c/{esc(str(c))}'>{esc(str(c))} {esc(st.sub_name(tree, str(c)))}</a>" for c in m["related_cats"]) + "</p>"
    if m.get("suspect_dup"):
        notes += "<p>⚠️ 疑似重复：" + "、".join(f"<a href='/q/{esc(q)}'>{esc(q)}</a>" for q in m["suspect_dup"]) + "</p>"
    if m.get("merge_into"):
        notes += f"<p>🔁 入库时将合并进：<a href='/q/{esc(m['merge_into'])}'>{esc(m['merge_into'])}</a></p>"
    if m.get("merged_from"):
        notes += "<p class=meta>已合并来源：" + esc("、".join(m["merged_from"])) + "</p>"
    if m.get("new_cat"):
        notes += f"<p>💡 建议新增细类：{esc(str(m['new_cat'].get('name', '')))}</p>"
    brief = f"<h2>简要</h2><div class=brief>{_inline(rd('简要.md').strip() or m.get('brief', ''))}</div>"
    report = rd("报告.md")
    report = re.sub(r"\A# .*\n+", "", report)               # 页面已有标题
    report = "<h2>报告</h2>" + md_to_html(report)
    raws = sorted((d / "原文").glob("原文-*.md")) if (d / "原文").exists() else []
    rawh = [f"<h2>原文（{len(raws)} 个批次）</h2><p class=meta>逐字保存，未脱敏，只追加不改写。</p>"]
    for f in raws:
        txt = f.read_text(encoding="utf-8")
        mm = re.match(r"<!--\s*(.*?)\s*-->\s*", txt, re.S)
        cap, body = (mm.group(1), txt[mm.end():]) if mm else (f.name, txt)
        msgs = body.rstrip("\n").split("\n\n-----消息分隔-----\n\n")
        rawh.append(f"<div class=cap>{esc(cap)}　·　共 {len(msgs)} 段</div>" + "".join(render_raw(x, m["qid"]) for x in msgs))
    if (d / "识图结果.md").exists():
        rawh.append("<h2>图片识别结果</h2>" + "<pre>" + esc(rd("识图结果.md")) + "</pre>")
    return m["title"], crumb + head + notes + brief + report + "".join(rawh)


def view_search(st, q):
    tree = st.tree()
    if not q.strip():
        return "搜索", "<h1>搜索</h1><p class=meta>输入关键词（多个词需同时命中），或 <code>#标签</code>（写别名也行，厂商包含它的模型），例如：<code>限流 日志</code>、<code>401 #号池</code>、<code>#Gemini</code>；要限定类可写 <code>#渠道形态:Azure</code>。</p>"
    total, hits = kb.search(st.root, q, limit=60, vocab=st.vocab())
    return f"搜索：{q}", f"<h1>搜索：{esc(q)}</h1><p class=meta>共 {total} 条" + ("，仅显示前 60 条" if total > 60 else "") + "</p>" + table([m for _, m in hits], tree, st)


def view_tags(st):
    v = st.vocab()
    stats = kb.tag_stats(st.root, v)
    pend = kb.load_pending_tags(st.root)

    def chip(n, c):
        return f"<a class=tag href='/search?q={quote('#' + n)}'>{esc(n)} <span class=n>{c}</span></a>"

    def by_count(d):
        return sorted(d.items(), key=lambda x: (-x[1], x[0]))

    out = ["<h1>标签</h1><p class=meta>标签已按类整理，点击标签看相关问题；搜厂商会包含它名下的所有模型。</p>"]
    hier = kb.tag_hierarchy(st.root, v)
    if hier:
        def link(label, q, n):
            lab = f"{esc(label)} <span class=n>{n}</span>"
            return f"<a class=tag href='/search?q={quote(q)}'>{lab}</a>" if q else f"<span class=tag>{lab}</span>"

        out.append("<h2>层级视图 <span class=meta>厂商 → 模型 → 错误码（由问题的标签推出）</span></h2>")
        for vend, models in sorted(hier.items(), key=lambda x: -sum(n["n"] for n in x[1].values())):
            vq = "#" + vend if vend != kb.NO_VENDOR else ""
            vname = f"<a href='/search?q={quote(vq)}'><b>{esc(vend)}</b></a>" if vq else f"<b>{esc(vend)}</b>"
            out.append(f"<p style='margin:.8em 0 .2em'>{vname} <span class=n>{sum(n['n'] for n in models.values())}</span></p><ul class=tree>")
            for mo, n in sorted(models.items(), key=lambda x: -x[1]["n"]):
                mq = ("#" + mo) if mo != kb.NO_MODEL else vq
                codes = "".join(link(c, (mq + " #" + c).strip(), k) for c, k in sorted(n["codes"].items(), key=lambda x: (-x[1], x[0])))
                out.append(f"<li>{link(mo, mq, n['n'])} {codes}</li>")
            out.append("</ul>")
    for g in [*v.groups, kb.UNGROUPED]:
        cnt = stats.get(g)
        if not cnt:
            continue
        out.append(f"<h2>{esc(g)} <span class=meta>{len(cnt)} 个</span></h2>")
        if g == "模型":
            by = {}
            for n, c in cnt.items():
                by.setdefault(v.vendor_of(n) or "（无厂商）", {})[n] = c
            for vend, d in sorted(by.items(), key=lambda x: -sum(x[1].values())):
                out.append(f"<p class=meta style='margin:.6em 0 .2em'>{esc(vend)}</p><p style='margin:.1em 0 .6em 1.2em'>"
                           + "".join(chip(n, c) for n, c in by_count(d)) + "</p>")
        else:
            out.append("<p>" + "".join(chip(n, c) for n, c in by_count(cnt)) + "</p>")
    if pend:
        out.append(f"<h2>待确认 <span class=meta>{len(pend)} 个</span></h2><p class=meta>整理时出现、词表里还没有的标签；在微信里用 /同意新标签 确认。</p><p>"
                   + "".join(f"<span class=tag>{esc(n)} <span class=n>{len(x['qids'])}</span></span>" for n, x in pend.items()) + "</p>")
    if len(out) == 1:
        out.append("<p>暂无标签</p>")
    return "标签", "".join(out)


def view_manual(st):
    p = st.root / "手册" / "排查手册.md"
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8")
    extra = st.root / "手册" / "分类边界.md"            # 机器人整理时遵守的分类边界与答复规范，接在案例文档后面
    if extra.exists():
        text += "\n\n---\n\n" + extra.read_text(encoding="utf-8")
    return "排查手册", md_to_html(text)


# ---------------- HTTP ----------------
SEC = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
       "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}


def make_server(root, host="127.0.0.1", port=0, password="", backup_status=""):
    st = Store(root, backup_status)

    class H(BaseHTTPRequestHandler):
        timeout = 15
        server_version = "kb-web"
        sys_version = ""

        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % a))

        def _send(self, code, body: bytes, ctype="text/html; charset=utf-8", extra=None, head=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in {**SEC, **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def _auth_ok(self):
            if not password:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    u, _, p = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                    return hmac.compare_digest(p.encode(), password.encode())
                except Exception:
                    return False
            return False

        def _handle(self, head=False):
            if not self._auth_ok():
                return self._send(401, page("需要登录", "<h1>需要登录</h1><p>请输入访问密码（用户名随意）。</p>"), extra={"WWW-Authenticate": 'Basic realm="kb", charset="UTF-8"', "Cache-Control": "no-store"}, head=head)
            u = urlparse(self.path)
            path = unquote(u.path)
            q = parse_qs(u.query).get("q", [""])[0][:200]
            if "\x00" in path:
                return self._send(400, page("错误", "<h1>请求无效</h1>"), head=head)
            res = None
            try:
                if path == "/":
                    res = view_home(st)
                elif (m := re.fullmatch(r"/c/(\d+(?:\.\d+)?)", path)):
                    res = view_cat(st, m.group(1))
                elif (m := re.fullmatch(r"/q/(Q\d{8}-\d{3})", path)):
                    res = view_problem(st, m.group(1))
                elif path == "/search":
                    res = view_search(st, q)
                elif path == "/tags":
                    res = view_tags(st)
                elif path == "/manual":
                    res = view_manual(st)
                elif (m := re.fullmatch(r"/img/(Q\d{8}-\d{3})/([\w.\-]+)", path)):
                    return self._image(m.group(1), m.group(2), head)
                elif path == "/healthz":
                    return self._send(200, b"ok", "text/plain", head=head)
            except Exception as e:                                      # 页面出错不能把服务带崩，也不泄漏路径
                sys.stderr.write(f"ERROR {path}: {e!r}\n")
                return self._send(500, page("出错了", "<h1>页面生成出错</h1><p>请稍后再试，或联系维护者查看日志。</p>"), head=head)
            if res is None:
                return self._send(404, page("未找到", "<h1>未找到</h1><p><a href='/'>回到首页</a></p>", q), head=head)
            return self._send(200, page(res[0], res[1], q if path == "/search" else ""), extra={"Cache-Control": "no-store"}, head=head)

        def _image(self, qid, name, head):
            m = st.get(qid)
            f = (m["_dir"] / "原文" / "images" / name) if m else None
            if not f or f.suffix.lower() not in IMG_OK or not f.is_file() or f.resolve().parent != (m["_dir"] / "原文" / "images").resolve():
                return self._send(404, page("未找到", "<h1>未找到</h1>"), head=head)
            return self._send(200, f.read_bytes(), mimetypes.guess_type(name)[0] or "application/octet-stream", {"Cache-Control": "max-age=3600"}, head)

        def do_GET(self):
            self._handle()

        def do_HEAD(self):
            self._handle(head=True)

        def _no(self):
            self._send(405, page("不允许", "<h1>只读页面，不支持该操作</h1>"), extra={"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _no

    class S(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return S((host, port), H)


def main():
    root = os.environ.get("KB_ROOT", "/opt/astrbot/data/kb")
    host, port = os.environ.get("KB_BIND", "127.0.0.1"), int(os.environ.get("KB_PORT", "6186"))
    pw = os.environ.get("KB_WEB_PASSWORD", "")
    srv = make_server(root, host, port, pw, os.environ.get("KB_BACKUP_STATUS", ""))
    print(f"kb-web 监听 {host}:{port}，知识库 {root}，{'已启用密码' if pw else '未设密码'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
