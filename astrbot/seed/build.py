"""读取 draft_raw.json → 校验 → 全局找重复并合并 → 生成暂存知识库 stage/kb 和 清单.md。不碰服务器。"""
import json, shutil, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm
from llm import kb

HERE = Path(__file__).resolve().parent
STAGE = HERE / "stage" / "kb"
SRC = "问答案例文档-主版本.md"

GROUP_PROMPT = """下面是知识库种子案例列表，格式：行号｜当前分类｜标题｜核心问题概括。
找出其中【讲同一个问题】的案例组（同一报错/同一问法/同一结论，只是在不同章节重复出现，例如 FAQ 和错误码表里的 401）。
只输出 JSON：{{"groups": [[行号, 行号, ...], ...]}}。每组至少 2 个行号，一个行号最多属于一组。
只把真正重复的放一组；只是主题相近、结论不同的不要放一起；拿不准就不放。

{items}
"""

MERGE_PROMPT = """下面是同一个问题的两份整理（JSON）。请合并成一份更完整的，只输出一个 JSON 对象，字段与输入完全一致
（title、category、status、source、tags、brief、phenomenon、elements、cause、solution、reply_script、retry_helps、need_verify、verify_materials、impact、evidence、dedupe_key）。
规则：保留双方有效信息，去重，不得编造；category、title 取【甲】的。

【甲】：{a}

【乙】：{b}
"""

# 人工复核后的归类调整（行号 -> 细类）。规则：
#  3.4 = 生图/视频模型自身的参数与行为；3.5 = 其余模型（Kimi/GLM/DeepSeek 等）的专项约束
#  2.1 = 客户在平台买到的价格/倍率/计量规则；6.2 = 采购报价与成本核算
OVERRIDES = {226: "3.4", 227: "3.4", 228: "3.4", 229: "3.4", 230: "3.4",
             205: "6.2", 206: "6.2", 208: "6.2", 211: "6.2", 258: "6.2",
             182: "6.1", 254: "6.4"}
# 第一次运行时大模型给出的重复分组（缓存，保证重建结果稳定）
GROUPS_CACHE = HERE / "groups.json"

def load():
    cases = {c["line"]: c for c in json.load(open(HERE / "cases.json", encoding="utf-8"))}
    raw = {r["line"]: r["data"] for r in json.load(open(HERE / "draft_raw.json", encoding="utf-8"))}
    return cases, raw

def clean(d):
    _, sub = kb.find_cat(TREE, str(d.get("category", "0")))
    if sub is None: d["category"] = "0"
    else: d["category"] = str(d["category"]).strip()
    if d.get("status") not in kb.STATUSES: d["status"] = "待跟进"
    if d.get("source") not in kb.SOURCES: d["source"] = "待验证"
    d["tags"] = [str(t) for t in (d.get("tags") or [])][:12]
    if not isinstance(d.get("new_cat"), dict) or d["category"] != "0": d["new_cat"] = d.get("new_cat") if isinstance(d.get("new_cat"), dict) else None
    return d

def raw_text(c):
    head = f"来源：{SRC} 第{c['line']}行（章节：{c['section']}）。此条来自已整理文档，无聊天原文。"
    hdr = f"表头：{' | '.join(c['header'])}\n" if c["header"] else ""
    return f"{head}\n{hdr}原行：\n{c['raw']}"

TREE = kb.load_tree(Path(__import__("tempfile").mkdtemp()))

def main():
    cases, raw = load()
    bad = [l for l in cases if not raw.get(l)]
    items = {l: clean(raw[l]) for l in cases if raw.get(l)}
    # ---- 全局找重复 ----
    listing = "\n".join(f"{l}｜{d['category']}｜{d['title']}｜{d.get('dedupe_key','')}" for l, d in items.items())
    if GROUPS_CACHE.exists():
        gj = json.load(open(GROUPS_CACHE, encoding="utf-8"))
    else:
        gj = llm.chat_json(GROUP_PROMPT.format(items=listing)) or {"groups": []}
        GROUPS_CACHE.write_text(json.dumps(gj, ensure_ascii=False), encoding="utf-8")
    groups, used = [], set()
    for g in gj.get("groups", []):
        g = [int(x) for x in g if int(x) in items and int(x) not in used]
        if len(g) >= 2:
            groups.append(g); used.update(g)
    # ---- 合并 ----
    merged_from = {}     # 主行号 -> [被并行号]
    notes = []
    for g in groups:
        g.sort(key=lambda l: -(len(items[l].get("solution", "")) + len(items[l].get("reply_script", ""))))
        main_l, rest = g[0], g[1:]
        cur = items[main_l]
        for o in rest:
            try:
                m = llm.chat_json(MERGE_PROMPT.format(a=json.dumps(cur, ensure_ascii=False), b=json.dumps(items[o], ensure_ascii=False)))
                if m:
                    m["title"], m["category"] = cur["title"], cur["category"]
                    cur = clean(m)
                else: raise ValueError("合并结果无法解析")
            except Exception as e:
                notes.append(f"第{o}行并入第{main_l}行时报告合并失败（{e}），仅追加原文，报告保留第{main_l}行版本")
        items[main_l] = cur
        merged_from[main_l] = rest
    absorbed = {o for rest in merged_from.values() for o in rest}
    for l, c in OVERRIDES.items():
        if l in items and l not in absorbed:
            items[l]["category"] = c
    # ---- 写暂存知识库 ----
    shutil.rmtree(HERE / "stage", ignore_errors=True)
    STAGE.mkdir(parents=True)
    kb.load_tree(STAGE)
    (STAGE / "手册").mkdir()
    shutil.copy(HERE / "manual.md", STAGE / "手册" / "排查手册.md")
    qid_of, rows = {}, []
    for l in sorted(items):
        if l in absorbed: continue
        d = items[l]
        qid = kb.next_qid(STAGE); qid_of[l] = qid
        pd = kb.cat_dir(STAGE, TREE, d["category"]) / f"{qid}-{kb.slug(d['title'])}"
        pd.mkdir(parents=True)
        lines_all = [l] + merged_from.get(l, [])
        for ln in lines_all:
            kb.write_raw(pd / "原文", [("text", raw_text(cases[ln]))], "种子导入", f"{SRC}#L{ln}")
        meta = {"qid": qid, "title": d["title"], "category": d["category"], "status": d["status"], "source": d["source"],
                "tags": d["tags"], "brief": kb.redact(str(d.get("brief", ""))), "related": [], "suspect_dup": [],
                "merge_into": None, "new_cat": d.get("new_cat") if d["category"] == "0" else None, "bot": "种子导入",
                "sid": f"{SRC}", "origin_lines": lines_all, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "committed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "merged_from": [f"第{x}行" for x in merged_from.get(l, [])]}
        (pd / "简要.md").write_text(meta["brief"] + "\n", encoding="utf-8")
        (pd / "报告.json").write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        (pd / "报告.md").write_text(kb.render_report(d, meta), encoding="utf-8")
        kb.write_meta(pd, meta)
        rows.append((l, qid, d, meta))
    # ---- 清单 ----
    out = [f"# 种子导入归类清单（待你确认）\n", f"- 文档案例行：{len(cases)} 条；成功整理 {len(items)} 条；失败 {len(bad)} 条；",
           f"- 重复合并：{len(merged_from)} 组，合并掉 {len(absorbed)} 条；最终问题数：**{len(rows)}**；",
           f"- 手册：第一章整章原样放入 `手册/排查手册.md`。\n"]
    if bad: out.append("## ⚠️ 失败的行\n" + "、".join(f"第{l}行" for l in bad) + "\n")
    cnt = {}
    for _, _, d, _ in rows: cnt[d["category"]] = cnt.get(d["category"], 0) + 1
    out.append("## 各细类数量\n")
    for m in TREE:
        for s in (m["subs"] or [{"code": m["code"], "name": m["name"]}]):
            out.append(f"- {s['code']} {s['name']}：{cnt.get(s['code'], 0)}")
    out.append("\n## 逐条归类\n")
    for m in TREE:
        for s in (m["subs"] or [{"code": m["code"], "name": m["name"]}]):
            rs = [r for r in rows if r[2]["category"] == s["code"]]
            if not rs: continue
            out.append(f"\n### {s['code']} {s['name']}（{len(rs)}）\n\n| 编号 | 来源行 | 标题 | 状态 | 口径 | 标签 |\n|---|---|---|---|---|---|")
            for l, qid, d, meta in rs:
                src = "、".join(str(x) for x in meta["origin_lines"])
                out.append(f"| {qid} | {src} | {d['title']} | {d['status']} | {d['source']} | {'、'.join(d['tags'])} |")
    if merged_from:
        out.append("\n## 重复合并明细\n")
        for ml, rest in merged_from.items():
            out.append(f"- **{qid_of[ml]} {items[ml]['title']}**（第{ml}行）← 并入：" + "；".join(f"第{o}行《{raw[o]['title']}》[{raw[o]['category']}]" for o in rest))
    nc = [(l, d) for l, _, d, _ in rows if d["category"] == "0"]
    sug = [(l, d["new_cat"]) for l, _, d, _ in rows if d.get("new_cat")]
    out.append("\n## 待分类与新增细类建议\n")
    out += ([f"- 第{l}行《{d['title']}》→ 建议新增：{(d.get('new_cat') or {}).get('name', '（无建议）')}（{(d.get('new_cat') or {}).get('reason', '')}）" for l, d in nc] or ["- 无，全部归入现有细类"])
    if notes: out.append("\n## 备注\n" + "\n".join(f"- {n}" for n in notes))
    (HERE / "清单.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"问题 {len(rows)}；合并 {len(merged_from)} 组/{len(absorbed)} 条；失败 {len(bad)}；待分类 {len(nc)}；备注 {len(notes)}")

if __name__ == "__main__":
    main()
