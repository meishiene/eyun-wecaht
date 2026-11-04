"""存量补全脚本：出方案不改库；写入只补缺的字段、标记待审、不碰 AI 原稿；失败/过期的条目跳过。"""
import json, shutil, sys, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "kb_digest")); sys.path.insert(0, str(REPO / "seed"))
import kb_core as k, backfill as B


def mk(root, tree, title, cat, **extra):
    qid = k.next_qid(root); d = k.cat_dir(root, tree, cat) / f"{qid}-{k.slug(title)}"; d.mkdir(parents=True)
    meta = {"qid": qid, "title": title, "category": cat, "status": "已解决", "source": "待验证", "tags": [], "brief": title + " 简要",
            "related": [], "suspect_dup": [], "new_cat": None, **extra}
    k.write_meta(d, meta)
    r = {"title": title, "phenomenon": "现象 " + title, "elements": {}, "cause": "原因", "solution": "方案", "reply_script": "AI原稿 " + title}
    (d / "报告.json").write_text(json.dumps(r, ensure_ascii=False)); (d / "报告.md").write_text(k.render_report(r, meta), encoding="utf-8")
    k.write_raw(d / "原文", [("text", "客户：" + title + " sk-abcdefghijklmnop1234")], "bot1", "s1")
    return qid


def fake(prompts):
    def chat_json(prompt):
        prompts.append(prompt)
        if "炸了" in prompt: raise RuntimeError("llm down")
        if "垃圾" in prompt: return None
        return {"principle": "白话原理 手机13812345678", "owner_side": "供应侧", "routine": "非常态", "related_cat": "1.3"}
    return chat_json


def test_plan_and_apply():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        a = mk(root, tree, "号被封", "4.1"); b = mk(root, tree, "限流", "4.6"); c = mk(root, tree, "炸了的", "4.2"); d_ = mk(root, tree, "垃圾的", "4.3")
        done = mk(root, tree, "已有字段", "4.7", owner_side="请求侧", routine="常态")                       # 已有字段的不补
        before = {m["qid"]: (m["_dir"] / "报告.json").read_text(encoding="utf-8") for m in k.all_items(root)}
        prompts = []
        plan = B.build_plan(root, chat_json=fake(prompts))
        assert plan["total_candidates"] == 4 and [e["qid"] for e in plan["entries"]] == [a, b, c, d_]            # 已有字段的不在方案里
        assert all("以【根因】决定主分类" in p and "owner_side" in p and "4.1 鉴权与权限" in p for p in prompts)       # 规则和分类树进了提示词
        pa = next(p for p in prompts if "【问题】" + a in p)                                                       # 线程池里顺序不固定，按内容找
        assert "AI原稿 号被封" in pa and "客户：号被封" in pa and "当前主分类是 4.1" in pa
        assert {e["qid"]: bool(e.get("error")) for e in plan["entries"]} == {a: False, b: False, c: True, d_: True}
        ea = plan["entries"][0]
        assert ea["owner_side"] == "供应侧" and ea["routine"] == "非常态" and ea["related_cats"] == ["1.3"] and "13812345678" not in ea["principle"]
        for m in k.all_items(root):                                                                              # 出方案不改库
            assert (m["_dir"] / "报告.json").read_text(encoding="utf-8") == before[m["qid"]]
            assert ("owner_side" in k.read_meta(m["_dir"])) == (m["qid"] == done)
        res = B.apply_plan(root, plan)
        assert res["done"] == [a, b] and sorted(q for q, _ in res["skipped"]) == sorted([c, d_])
        m = k.find_problem(root, a); r = json.loads((m["_dir"] / "报告.json").read_text(encoding="utf-8"))
        assert m["owner_side"] == "供应侧" and m["routine"] == "非常态" and m["related_cats"] == ["1.3"] and m["ai_filled"] is True and m["ai_filled_at"]
        assert r["reply_script"] == "AI原稿 号被封" and r["cause"] == "原因" and r["principle"].startswith("白话原理") and "related_cat" not in r   # 原稿不动
        rep = (m["_dir"] / "报告.md").read_text(encoding="utf-8")
        assert "AI 补全" in rep and "白话原理" in rep and "相关分类：1.3" in rep and "13812345678" not in rep + json.dumps(r, ensure_ascii=False)
        assert k.needs_review(m) and "owner_side" not in k.find_problem(root, c)                                  # 失败的没动
        assert k.find_problem(root, done)["owner_side"] == "请求侧" and not k.find_problem(root, done).get("ai_filled")
        # 再跑一次：幂等，已补的跳过
        res = B.apply_plan(root, plan); assert res["done"] == [] and len(res["skipped"]) == 4 and "已经有" in dict(res["skipped"])[a]
        # 人工审核后 ai_filled 消失，字段保留
        k.set_audit(root, a, 5); m = k.find_problem(root, a); assert m["ai_filled"] is False and m["owner_side"] == "供应侧"
    finally:
        shutil.rmtree(root)


def test_limit_qids_and_changed_since_plan():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        ids = [mk(root, tree, f"问题{i}", "4.6") for i in range(10)]
        assert [m["qid"] for m in B.pick([{"qid": q} for q in ids], 5)] == ids[0::2]                              # 均匀抽样
        assert B.pick([{"qid": q} for q in ids], 0) == [{"qid": q} for q in ids] and len(B.pick([{"qid": q} for q in ids], 99)) == 10
        plan = B.build_plan(root, limit=3, chat_json=fake([])); assert len(plan["entries"]) == 3 and plan["total_candidates"] == 10
        plan = B.build_plan(root, qids={ids[4], ids[5]}, chat_json=fake([])); assert [e["qid"] for e in plan["entries"]] == ids[4:6]
        # 出方案后被别人补过 / 被合并删除 → 跳过
        d = k.find_problem(root, ids[4])["_dir"]; mm = k.read_meta(d); mm["owner_side"] = "请求侧"; k.write_meta(d, mm)
        shutil.rmtree(k.find_problem(root, ids[5])["_dir"])
        res = B.apply_plan(root, plan); assert res["done"] == [] and [q for q, _ in res["skipped"]] == ids[4:6]
        assert B.apply_plan(root, plan, qids={"Q-none"}) == {"done": [], "skipped": []}                          # --qids 过滤
    finally:
        shutil.rmtree(root)


def test_cli_apply_commits():
    import subprocess
    root = Path(tempfile.mkdtemp()); out = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root); a = mk(root, tree, "号被封", "4.1"); k.git_commit(root, "初始")
        (out / "p.json").write_text(json.dumps(B.build_plan(root, chat_json=fake([]))), encoding="utf-8")
        r = subprocess.run([sys.executable, "-I", str(REPO / "seed" / "backfill.py"), "apply", "--kb", str(root), "--plan", str(out / "p.json")],
                           capture_output=True, text=True)
        assert r.returncode == 0 and "已补全 1 条" in r.stdout, r.stdout + r.stderr
        assert "补全新字段 1 条" in k.git(root, "log", "--oneline", "-1") and k.find_problem(root, a)["ai_filled"]
        r = subprocess.run([sys.executable, "-I", str(REPO / "seed" / "backfill.py"), "apply", "--kb", str(root), "--plan", str(out / "p.json")],
                           capture_output=True, text=True)
        assert "已补全 0 条" in r.stdout and "跳过 1 条" in r.stdout and k.git(root, "log", "--oneline").count("\n") == 1   # 再跑一次不产生新提交
    finally:
        shutil.rmtree(root); shutil.rmtree(out)

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
