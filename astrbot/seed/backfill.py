#!/usr/bin/env python3
"""一次性：给已有问题补「原理说明 / 归属侧 / 常态 / 关联分类」。分两步，先出方案，确认后再写入。

  # 1) 出方案（只读，不改知识库；需要环境变量 SEED_KEY）。--limit N 在全部待补问题里均匀抽 N 条试跑
  python3 -I seed/backfill.py plan  --kb <知识库目录> --out <输出目录> [--limit N] [--qids Q1,Q2]
  # 2) 看过方案后写入（可重复执行；问题自出方案后被改过的会跳过；写完做一次 git 提交）
  python3 -I seed/backfill.py apply --kb <知识库目录> --plan <输出目录>/backfill_plan.json [--qids Q1,Q2] [--no-commit]

规则：
  - 只补还没有这些字段的问题（元数据里没有 owner_side）；已有的、人工审核过的不动。
  - 补出来的内容一律标记 ai_filled=true（“AI 补全，待审”），在 /审 之前网页和报告里都带提醒。
  - 报告.json 里原来的字段（含 AI 原稿的答复口径）一个字都不改，只新增 principle / owner_side / routine。
  - 大模型只能在固定枚举里选；关联分类必须是分类树里真实存在的细类。拿不准就“待判断”。
  - 应用前请先备份（systemctl start kb-backup.service）。
"""
import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.environ.get("KB_PLUGIN_DIR") or str(Path(__file__).resolve().parent.parent / "plugins" / "kb_digest"))
import kb_core as kb  # noqa: E402

PROMPT = """你在给「大模型中转站」客服知识库里一个已有问题补充几个字段。下面是这个问题的报告和部分原文，请只输出一个 JSON 对象（不要任何多余文字、不要代码块），字段如下：
{{
{new_fields}
}}
规则：1）只依据下面的内容，不得编造原文没有的事实，不确定就写“原文未提及”或“待判断”；2）输出里不要出现完整的 API Key、手机号、邮箱；
3）{boundary}
当前主分类是 {cat}，related_cat 不要再填它。

分类树：
{tree}

【问题】{qid}｜{title}
当前分类：{cat}　状态：{status}
简要：{brief}
报告（节选）：
{report}

原文（节选）：
{raw}
"""

REPORT_KEYS = ["phenomenon", "elements", "cause", "solution", "reply_script", "impact", "evidence"]


def _raw_text(d: Path, limit: int = 3000) -> str:
    parts = []
    for f in sorted((d / "原文").glob("原文-*.md")) if (d / "原文").exists() else []:
        t = re.sub(r"\A<!--.*?-->\s*", "", f.read_text(encoding="utf-8"), flags=re.S)
        parts.append(t.replace("\n\n-----消息分隔-----\n\n", "\n"))
    return "\n".join(parts)[:limit]


def _needs(m: dict) -> bool:
    return "owner_side" not in m


def build_prompt(m: dict, tree: list) -> str:
    d = m["_dir"]
    try:
        rep = json.loads((d / "报告.json").read_text(encoding="utf-8"))
    except Exception:
        rep = {}
    rep_txt = json.dumps({k: rep.get(k) for k in REPORT_KEYS if rep.get(k)}, ensure_ascii=False)[:2500]
    return PROMPT.format(new_fields=kb.NEW_FIELDS_SCHEMA, boundary=kb.BOUNDARY_RULE, tree=kb.tree_text(tree),
                         qid=m["qid"], title=m["title"], cat=m.get("category", "0"), status=m.get("status", ""),
                         brief=m.get("brief", ""), report=rep_txt, raw=_raw_text(d))


def pick(items: list, limit: int) -> list:
    """均匀抽样，让试跑覆盖不同分类。"""
    if not limit or limit >= len(items):
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def build_plan(root: Path, limit: int = 0, qids=None, chat_json=None) -> dict:
    tree = kb.load_tree(root)
    items = [m for m in kb.all_items(root) if _needs(m)]
    if qids:
        items = [m for m in items if m["qid"] in qids]
    items = pick(sorted(items, key=lambda m: m["qid"]), limit)
    if chat_json is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))   # python3 -I 不会自动带上脚本所在目录
        import llm  # 种子脚本里的 mux.la 调用，读 SEED_KEY
        chat_json = llm.chat_json

    def ask(m):
        try:
            out = chat_json(build_prompt(m, tree))
        except Exception as e:  # 单条失败不影响其他
            return {"qid": m["qid"], "title": m["title"], "error": str(e)}
        if not isinstance(out, dict):
            return {"qid": m["qid"], "title": m["title"], "error": "大模型返回无法解析"}
        f = kb.extract_fields(out, tree, m.get("category", "0"))
        return {"qid": m["qid"], "title": m["title"], "category": m.get("category", "0"), "dir": str(m["_dir"].relative_to(root)),
                "principle": f.get("principle", ""), "owner_side": f.get("owner_side", "待判断"),
                "routine": f.get("routine", "待判断"), "related_cats": f["related_cats"]}

    with ThreadPoolExecutor(4) as ex:
        entries = list(ex.map(ask, items))
    return {"made_at": time.strftime("%Y-%m-%d %H:%M:%S"), "total_candidates": len([m for m in kb.all_items(root) if _needs(m)]),
            "entries": entries}


def apply_plan(root: Path, plan: dict, qids=None) -> dict:
    """写入方案。返回 {"done": [...], "skipped": [(qid, 原因)]}。"""
    tree = kb.load_tree(root)
    done, skipped = [], []
    for e in plan.get("entries", []):
        q = e["qid"]
        if qids and q not in qids:
            continue
        if e.get("error"):
            skipped.append((q, "方案里就是失败的：" + e["error"]))
            continue
        m = kb.find_problem(root, q)
        if not m:
            skipped.append((q, "问题已不存在"))
            continue
        if not _needs(m):
            skipped.append((q, "已经有这些字段了（可能被人工改过或已补过）"))
            continue
        d = m["_dir"]
        meta = kb.read_meta(d)
        rp = d / "报告.json"
        data = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {"title": meta["title"]}
        data.update({"principle": e["principle"], "owner_side": e["owner_side"], "routine": e["routine"],
                     "related_cat": e["related_cats"]})
        kb.apply_fields(meta, data, tree, meta.get("category", "0"))
        meta["ai_filled"], meta["ai_filled_at"] = True, time.strftime("%Y-%m-%d %H:%M:%S")
        rp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        kb.write_meta(d, meta)
        (d / "报告.md").write_text(kb.render_report(data, meta), encoding="utf-8")
        done.append(q)
    return {"done": done, "skipped": skipped}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("--kb", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--limit", type=int, default=0)
    a.add_argument("--qids", default="")
    b = sub.add_parser("apply")
    b.add_argument("--kb", required=True)
    b.add_argument("--plan", required=True)
    b.add_argument("--qids", default="")
    b.add_argument("--no-commit", action="store_true")
    args = ap.parse_args()
    root = Path(args.kb)
    qids = {x.strip() for x in args.qids.split(",") if x.strip()} or None
    if args.cmd == "plan":
        plan = build_plan(root, args.limit, qids)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "backfill_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        ok = [e for e in plan["entries"] if not e.get("error")]
        lines = [f"# 补全方案（{plan['made_at']}）", "", f"共 {len(plan['entries'])} 条，成功 {len(ok)}，失败 {len(plan['entries']) - len(ok)}；知识库里待补的共 {plan['total_candidates']} 条。", ""]
        for e in plan["entries"]:
            if e.get("error"):
                lines += [f"## {e['qid']} {e['title']}", f"- ❌ {e['error']}", ""]
                continue
            lines += [f"## {e['qid']} {e['title']}（{e['category']}）",
                      f"- 归属侧：{e['owner_side']}　类型：{e['routine']}　关联分类：{'、'.join(e['related_cats']) or '无'}",
                      f"- 原理说明：{e['principle'] or '（空）'}", ""]
        (out / "backfill_plan.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"方案已写入 {out}/backfill_plan.json 和 .md：成功 {len(ok)} / {len(plan['entries'])}")
        return
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    res = apply_plan(root, plan, qids)
    print(f"已补全 {len(res['done'])} 条；跳过 {len(res['skipped'])} 条")
    for q, why in res["skipped"]:
        print(f"  跳过 {q}：{why}")
    if res["done"] and not args.no_commit:
        print("git 提交：", kb.git_commit(root, f"补全新字段 {len(res['done'])} 条（AI，待审）"))


if __name__ == "__main__":
    main()
