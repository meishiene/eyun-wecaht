#!/usr/bin/env python3
"""一次性：按标签词表给已有问题重打标签。分两步，先出计划和改动清单，确认后再写入。

  # 1) 出计划（只读，不改知识库）。--llm 会再调大模型补漏标签（需要环境变量 SEED_KEY）
  python3 -I seed/retag.py plan  --kb <知识库目录> --out <输出目录> [--llm]
  # 2) 确认清单后写入（可重复执行；标签自上次出计划后被改过的问题会跳过）
  python3 -I seed/retag.py apply --kb <知识库目录> --plan <输出目录>/retag_plan.json [--no-llm-adds]

规则：
  - 旧标签先按词表（规范名+别名，忽略大小写）归一；模型自动补厂商。
  - 词表对不上的旧标签：在 DROP 里的丢弃（用户已决定不要），其余进「待确认」，不会自动丢。
  - 大模型只能从词表里挑规范名来"补"，不能新造标签。
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.environ.get("KB_PLUGIN_DIR") or str(Path(__file__).resolve().parent.parent / "plugins" / "kb_digest"))
import kb_core as kb  # noqa: E402

# 已和用户确认丢弃的旧标签：太泛，或裸数字做别名会冲突
DROP = {"模型", "渠道", "default", "cli", "sdk", "2.5", "2.0"}

LLM_PROMPT = """你在给「大模型中转站」知识库的问题补标签。下面是一个问题的内容、它现有的标签，和【标签词表】。
只从词表里挑【还缺的】规范名补上（厂商、模型、渠道形态、接入工具、错误码）。
要求：必须是问题内容里明确出现或明确涉及的；拿不准就不补；不要新造词表以外的标签；已经有的不要重复。
只输出 JSON：{{"add": ["规范名", ...]}}，没有要补的就 {{"add": []}}。

【标签词表】
{vocab}

【问题】{qid}｜{title}
简要：{brief}
现有标签：{tags}
报告内容（节选）：
{report}
"""


def build_plan(root: Path, use_llm: bool) -> dict:
    v = kb.load_vocab(root)
    items = kb.all_items(root)
    plan, pending = [], {}
    for m in items:
        old = [str(t) for t in m.get("tags", [])]
        new, unknown = v.normalize(old)
        dropped = [u for u in unknown if kb._tkey(u) in DROP]
        for u in unknown:
            if u not in dropped:
                pending.setdefault(u, []).append(m["qid"])
        plan.append({"qid": m["qid"], "title": m["title"], "dir": str(m["_dir"].relative_to(root)),
                     "old": old, "new": new, "dropped": dropped,
                     "pending": [u for u in unknown if u not in dropped], "llm_added": []})
    if use_llm:
        import llm  # 种子脚本里的 mux.la 调用，读 SEED_KEY
        by_q = {m["qid"]: m for m in items}

        def ask(p):
            m = by_q[p["qid"]]
            rep = kb._report_text(m["_dir"])[:1800]
            out = llm.chat_json(LLM_PROMPT.format(vocab=v.prompt_text(), qid=p["qid"], title=p["title"],
                                                  brief=m.get("brief", ""), tags="、".join(p["new"]) or "无", report=rep))
            adds = []
            for a in (out or {}).get("add", []):
                c = v.canon(a)
                if c and c not in p["new"] and c not in adds:
                    adds.append(c)
            return p, adds

        with ThreadPoolExecutor(4) as ex:
            for p, adds in ex.map(ask, plan):
                if adds:
                    p["llm_added"] = adds
                    p["new"], _ = v.normalize([*p["new"], *adds])
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "llm": use_llm, "items": plan,
            "pending_tags": pending}


def report_md(plan: dict, v: kb.Vocab) -> str:
    items = plan["items"]
    before, after = {}, {}
    merges = {}   # 规范名 -> {旧写法: 次数}
    for p in items:
        for t in p["old"]:
            before[t] = before.get(t, 0) + 1
            c = v.canon(t)
            if c and t != c:
                merges.setdefault(c, {})
                merges[c][t] = merges[c].get(t, 0) + 1
        for t in p["new"]:
            after[t] = after.get(t, 0) + 1
    changed = [p for p in items if p["old"] != p["new"]]
    L = [f"# 标签改动清单（{plan['generated']}）", "",
         f"- 问题 {len(items)} 个，其中 {len(changed)} 个的标签会变化",
         f"- 不同标签 {len(before)} → {len(after)}",
         f"- 旧标签丢弃 {sum(len(p['dropped']) for p in items)} 处；进待确认的新标签 {len(plan['pending_tags'])} 个",
         f"- 大模型补漏：{'已启用，共补 %d 处' % sum(len(p['llm_added']) for p in items) if plan['llm'] else '未启用（只做了词表归一）'}", ""]
    L += ["## 各类标签数（改后）", "", "| 类 | 标签数 | 涉及次数 |", "|---|---|---|"]
    for g in v.groups:
        ns = [n for n in after if v.group_of(n) == g]
        L.append(f"| {g} | {len(ns)} | {sum(after[n] for n in ns)} |")
    L += ["", "## 合并（旧写法 → 规范名）", ""]
    for c, d in sorted(merges.items(), key=lambda x: -sum(x[1].values())):
        L.append(f"- **{c}** ← " + "、".join(f"{k}({n})" for k, n in sorted(d.items(), key=lambda x: -x[1])))
    drops = {}
    for p in items:
        for d in p["dropped"]:
            drops.setdefault(d, []).append(p["qid"])
    L += ["", "## 丢弃（你已决定不要）", ""] + ([f"- {k}：{len(q)} 个问题" for k, q in drops.items()] or ["- 无"])
    L += ["", "## 待确认的新标签（词表里没有，改后用 /同意新标签 处理）", ""]
    L += [f"- {k}：{'、'.join(q)}" for k, q in plan["pending_tags"].items()] or ["- 无"]
    if plan["llm"]:
        L += ["", "## 大模型补的标签（逐条核对，不放心可用 --no-llm-adds 整体不写）", ""]
        for p in items:
            if p["llm_added"]:
                L.append(f"- {p['qid']} {p['title']}：+ {'、'.join(p['llm_added'])}")
    L += ["", "## 每个问题的变化", "", "| 编号 | 标题 | 旧 | 新 |", "|---|---|---|---|"]
    for p in changed:
        L.append(f"| {p['qid']} | {p['title'][:20]} | {'、'.join(p['old']) or '无'} | {'、'.join(p['new']) or '无'} |")
    return "\n".join(L) + "\n"


def apply_plan(root: Path, plan: dict, no_llm_adds: bool) -> dict:
    v = kb.load_vocab(root, create=True)       # 知识库里还没有词表就先落盘
    done = skipped = 0
    for p in plan["items"]:
        d = root / p["dir"]
        if not (d / "元数据.json").exists():
            skipped += 1
            continue
        cur = kb.read_meta(d).get("tags", [])
        new = p["new"]
        if no_llm_adds and p["llm_added"]:
            new, _ = v.normalize([t for t in new if t not in p["llm_added"] or t in p["old"]])
        if cur == new:
            continue                               # 已经是目标状态（重复执行）
        if cur != p["old"]:
            skipped += 1                           # 出计划之后被人改过，不覆盖
            print("跳过（标签已变动）:", p["qid"])
            continue
        kb.set_item_tags(d, new)
        done += 1
    for name, qids in plan["pending_tags"].items():
        for q in qids:
            kb.add_pending_tags(root, q, [name])
    h = kb.git_commit(root, f"标签重整: 改 {done} 个问题，词表 {len(v.tags)} 个标签")   # 没有变更时返回「无变更」
    return {"changed": done, "skipped": skipped, "commit": h}


def main(argv=None):
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("plan")
    a.add_argument("--kb", required=True); a.add_argument("--out", required=True); a.add_argument("--llm", action="store_true")
    b = sub.add_parser("apply")
    b.add_argument("--kb", required=True); b.add_argument("--plan", required=True); b.add_argument("--no-llm-adds", action="store_true")
    args = ap.parse_args(argv)
    root = Path(args.kb)
    if args.cmd == "plan":
        out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
        plan = build_plan(root, args.llm)
        (out / "retag_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "标签改动清单.md").write_text(report_md(plan, kb.load_vocab(root)), encoding="utf-8")
        print(f"计划已写入 {out}（未改动知识库）")
    else:
        r = apply_plan(root, json.loads(Path(args.plan).read_text(encoding="utf-8")), args.no_llm_adds)
        print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
