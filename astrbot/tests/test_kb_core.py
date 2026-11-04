import sys, tempfile, shutil, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "kb_digest"))
import kb_core as k

def test_redact():
    t = "key sk-abcdefghijklmnop1234 手机13812345678 邮箱a.b@x.com https://x/y?token=SECRET123&a=1 Bearer abcdefghijklmnopqrstu rid=req-123 模型gpt-5"
    r = k.redact(t)
    assert "abcdefghijklmnop1234" not in r and "sk-ab" in r
    assert "13812345678" not in r and "138****5678" in r
    assert "a.b@x.com" not in r
    assert "SECRET123" not in r and "token=****" in r
    assert "abcdefghijklmnopqrstu" not in r
    assert "rid=req-123" in r and "gpt-5" in r   # 排查要素保留

def test_parse_json():
    assert k.parse_json('```json\n{"a":1}\n```') == {"a": 1}
    assert k.parse_json('好的：{"a":{"b":2}} 完') == {"a": {"b": 2}}
    assert k.parse_json("不是json") is None and k.parse_json("") is None
    # 中转链路偶发：末尾多出一个没写完的字段
    broken = '{\n "title": "x",\n "tags": ["a","b"],\n "n": {"k": 1},\n  "z_end_turn_review": "'
    assert k.parse_json(broken) == {"title": "x", "tags": ["a", "b"], "n": {"k": 1}}
    assert k.parse_json('{"a": 1, "b": "未写完') == {"a": 1}
    assert k.parse_json('{"a": 1, "b": 2,') == {"a": 1, "b": 2}

def test_tree_and_dirs():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        assert sum(len(m["subs"]) for m in tree) == 37
        assert k.find_cat(tree, "4.6")[1]["name"] == "限流（429）"
        assert k.cat_dir(root, tree, "4.6").name == "4.6-限流（429）"
        assert k.cat_dir(root, tree, "9.9").parts[-1] == "0-待分类"   # 未知编号落待分类
    finally:
        shutil.rmtree(root)

def test_qid_never_reused():
    root = Path(tempfile.mkdtemp())
    try:
        a = k.next_qid(root); (root / "问题库").mkdir(); (root / "问题库" / (a + "-x")).mkdir()
        (root / ".counter.json").unlink()            # 计数器丢失
        b = k.next_qid(root)
        assert a != b and int(b[-3:]) == int(a[-3:]) + 1
    finally:
        shutil.rmtree(root)

def test_raw_is_verbatim_and_append_only():
    root = Path(tempfile.mkdtemp())
    try:
        img = root / "a.png"; img.write_bytes(b"\x89PNG")
        raw = root / "原文"
        txt = "  原样  文字 sk-abcdefghijklmnop1234\n第二行 "
        k.write_raw(raw, [("text", txt), ("img", img), ("text", "二")], "bot1", "s1")
        k.write_raw(raw, [("text", "追加")], "bot2", "s2")
        files = sorted(raw.glob("原文-*.md"))
        assert len(files) == 2
        c = files[0].read_text(encoding="utf-8")
        assert txt in c and "sk-abcdefghijklmnop1234" in c      # 原文不脱敏、不改写
        assert len(list((raw / "images").iterdir())) == 1
    finally:
        shutil.rmtree(root)

def test_pending_commit_move_git():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        qid = k.next_qid(root)
        d = k.pending_dir(root) / f"{qid}-测试"; d.mkdir(parents=True)
        k.write_meta(d, {"qid": qid, "title": "测试", "category": "4.6", "status": "未解决", "source": "待验证", "tags": []})
        assert [m["qid"] for m in k.list_pending(root)] == [qid]
        dst = k.commit_pending_item(root, tree, k.list_pending(root)[0])
        assert dst.parent.name == "4.6-限流（429）" and not k.list_pending(root)
        assert k.find_problem(root, qid)["committed_at"]
        new = k.move_problem(root, tree, qid, "4.7")
        assert new.parent.name == "4.7-服务端错误与断流（5xx）" and k.find_problem(root, qid)["category"] == "4.7"
        try: k.move_problem(root, tree, qid, "8.8"); assert False
        except ValueError: pass
        h = k.git_commit(root, "测试"); assert h and h != "无变更"
        assert k.git_commit(root, "无变更") == "无变更"
    finally:
        shutil.rmtree(root)

def test_git_recovers_from_half_initialized_repo():
    """复现线上问题：.git 已存在但没有身份配置、没有 .gitignore，且目录属主不同(safe.directory)。"""
    import os, subprocess
    root = Path(tempfile.mkdtemp())
    env = {k: os.environ.get(k) for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HOME")}
    os.environ.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    try:
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        (root / "a.md").write_text("x", encoding="utf-8")
        (root / ".counter.json").write_text("{}")
        h = k.git_commit(root, "半初始化后提交")
        assert h and h != "无变更"
        files = k.git(root, "ls-files").split("\n")
        assert "a.md" in files and ".counter.json" not in files and ".gitignore" in files
        # 已被误跟踪的计数器文件：下次提交会被取消跟踪，之后改动不再产生提交
        k.git(root, "add", "-f", ".counter.json"); k.git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "误跟踪")
        assert ".counter.json" in k.git(root, "ls-files")
        (root / ".counter.json").write_text('{"x":1}')
        k.git_commit(root, "清理")
        assert ".counter.json" not in k.git(root, "ls-files")
        (root / ".counter.json").write_text('{"x":2}')
        assert k.git_commit(root, "不应产生提交") == "无变更"
    finally:
        for key, v in env.items():
            if v is None: os.environ.pop(key, None)
            else: os.environ[key] = v
        shutil.rmtree(root)

def _mk(root, tree, title, cat, brief, tags=(), status="未解决", pending=False, report="", new_cat=None):
    qid = k.next_qid(root)
    base = k.pending_dir(root) if pending else k.cat_dir(root, tree, cat)
    d = base / f"{qid}-{k.slug(title)}"; d.mkdir(parents=True)
    meta = {"qid": qid, "title": title, "category": cat, "status": status, "source": "待验证", "tags": list(tags),
            "brief": brief, "related": [], "suspect_dup": [], "new_cat": new_cat}
    k.write_meta(d, meta)
    r = {"title": title, "phenomenon": report or brief, "elements": {}}
    (d / "报告.json").write_text(json.dumps(r, ensure_ascii=False)); (d / "报告.md").write_text(k.render_report(r, meta), encoding="utf-8")
    return qid

def test_search_and_filters():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        a = _mk(root, tree, "网关前限流无日志", "4.6", "429 RPM 限流不产生日志", ["429", "号池"])
        b = _mk(root, tree, "401 鉴权失败", "4.1", "Token 填错", ["401"])
        c = _mk(root, tree, "待入库的429问题", "4.6", "新来的", ["429", "GPT"], pending=True)
        n, r = k.search(root, "429")
        assert n == 2 and {m["qid"] for _, m in r} == {a, c}
        n, r = k.search(root, "429 日志")                       # AND：同时命中
        assert [m["qid"] for _, m in r] == [a]
        n, r = k.search(root, "429 #号池"); assert [m["qid"] for _, m in r] == [a]
        n, r = k.search(root, "#gpt"); assert [m["qid"] for _, m in r] == [c]     # 只有标签，不区分大小写
        n, r = k.search(root, "TOKEN"); assert [m["qid"] for _, m in r] == [b]    # 不区分大小写
        n, r = k.search(root, "网关限流日志"); assert r and r[0][1]["qid"] == a    # 退化：二字组合
        assert k.search(root, "分组")[0] == 0 and k.search(root, "rid")[0] == 0     # 模板标题不应命中
        n, r = k.search(root, "怎么网关限流"); assert r and r[0][1]["qid"] == a      # 口语词(怎么)不应拖垮二字匹配
        assert k.search(root, "完全无关的词xyz")[0] == 0 and k.search(root, "")[0] == 0
        assert k.search(root, a)[1][0][1]["qid"] == a                              # 按编号搜
        assert k.search(root, "429", limit=1)[0] == 2 and len(k.search(root, "429", limit=1)[1]) == 1
    finally:
        shutil.rmtree(root)

def test_resolve_qid():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        a = _mk(root, tree, "甲", "4.6", "x"); b = _mk(root, tree, "乙", "4.6", "y")
        assert k.resolve_qid(root, a)[0]["qid"] == a
        assert k.resolve_qid(root, a.lower())[0]["qid"] == a
        assert k.resolve_qid(root, a[1:])[0]["qid"] == a                    # 不带 Q
        assert k.resolve_qid(root, "2")[0]["qid"] == b                      # 只写序号
        assert "找不到" in k.resolve_qid(root, "Q0")[1] and "请给出" in k.resolve_qid(root, "")[1]
        # 序号歧义：同序号跨日期
        d = k.cat_dir(root, tree, "4.6") / "Q20250101-001-旧"; d.mkdir(parents=True)
        k.write_meta(d, {"qid": "Q20250101-001", "title": "旧", "category": "4.6"})
        r, err = k.resolve_qid(root, "1"); assert r is None and "多个" in err
    finally:
        shutil.rmtree(root)

def test_tree_summary_and_status():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        a = _mk(root, tree, "甲", "4.6", "x", status="未解决"); _mk(root, tree, "乙", "4.6", "y")
        _mk(root, tree, "丙", "0", "z", pending=True)
        t = k.tree_summary(root, tree)
        assert "共 2 个问题，待入库 1 个" in t and "4 报错与故障（2）" in t and "4.6 限流（429）  2" in t
        assert k.set_status(root, a, "已解决") == "未解决"
        m = k.find_problem(root, a)
        assert m["status"] == "已解决" and "状态：已解决" in (m["_dir"] / "报告.md").read_text(encoding="utf-8")
        try: k.set_status(root, a, "乱写"); assert False
        except ValueError: pass
        try: k.set_status(root, "Q0", "已解决"); assert False
        except KeyError: pass
    finally:
        shutil.rmtree(root)

def test_new_category_flow():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        nc = {"name": "市场规模与份额", "major": "7", "reason": "无对应细类"}
        a = _mk(root, tree, "蒸馏占比", "0", "x", new_cat=nc)
        b = _mk(root, tree, "另一个", "0", "y", new_cat=nc, pending=True)
        sug = k.new_cat_suggestions(root); assert len(sug) == 1 and sorted(sug[0]["qids"]) == sorted([a, b])
        code, moved = k.approve_new_cat(root, tree, "市场规模与份额")
        assert code == "7.4" and sorted(moved) == sorted([a, b])
        assert k.find_cat(tree, "7.4")[1]["name"] == "市场规模与份额"
        assert json.loads((root / "分类树.json").read_text(encoding="utf-8"))[6]["subs"][-1]["code"] == "7.4"   # 已持久化
        ma = k.find_problem(root, a)
        assert ma["category"] == "7.4" and ma["_dir"].parent.name == "7.4-市场规模与份额" and ma["new_cat"] is None
        assert k.find_problem(root, b)["_dir"].parent == k.pending_dir(root) and k.find_problem(root, b)["category"] == "7.4"
        assert not k.new_cat_suggestions(root)
        # 手动指定大类的新细类；名称重复/非法/不存在大类都要拒绝
        assert k.approve_new_cat(root, tree, "全新类别", "3")[0] == "3.6"
        for bad in [("全新类别", "3"), ("含 空格", "3"), ("", "3"), ("x", "99"), ("x", "0"), ("没有建议的", None)]:
            try: k.approve_new_cat(root, tree, *bad); assert False, bad
            except ValueError: pass
        assert k.load_tree(root)[2]["subs"][-1]["name"] == "全新类别"          # 重启后仍在
    finally:
        shutil.rmtree(root)

def test_render_report_redacts():
    meta = {"qid": "Q1", "title": "t", "status": "已解决", "source": "官方文档", "tags": ["GPT"], "related": [], "suspect_dup": ["Q0"]}
    r = {"phenomenon": "客户贴了 sk-abcdefghijklmnop1234", "elements": {"rid": "R-1"}, "retry_helps": "有用"}
    out = k.render_report(r, meta)
    assert "abcdefghijklmnop1234" not in out and "R-1" in out and "疑似重复" in out

def test_new_fields_validation():
    tree = k.load_tree(Path(tempfile.mkdtemp()))
    f = k.extract_fields({"principle": "原理 手机13812345678", "owner_side": "供应侧", "routine": "非常态", "related_cat": "1.3"}, tree, "4.1")
    assert f == {"principle": "原理 手机138****5678", "owner_side": "供应侧", "routine": "非常态", "related_cats": ["1.3"]}      # 脱敏 + 合法值原样
    f = k.extract_fields({"owner_side": "随便", "routine": None, "related_cat": "9.9"}, tree, "4.1")
    assert f == {"owner_side": "待判断", "routine": "待判断", "related_cats": []}                                    # 非法值退回待判断；不存在的分类丢掉
    assert k.extract_fields({"related_cat": "4.1"}, tree, "4.1")["related_cats"] == []                              # 等于主分类不算关联
    assert k.extract_fields({"related_cat": ["1.3", "1.3", "2.1", "4.1", "x"]}, tree, "4.1")["related_cats"] == ["1.3", "2.1"]
    assert "principle" not in k.extract_fields({}, tree, "4.1") and k.extract_fields({}, tree, "4.1")["related_cats"] == []
    # apply：关联分类取并集；空的原理说明不覆盖已有的；related_cat 不留在报告里
    meta, data = {"category": "4.1", "related_cats": ["2.1"]}, {"principle": "旧", "related_cat": "1.3"}
    k.apply_fields(meta, data, tree)
    assert meta["related_cats"] == ["2.1", "1.3"] and data["principle"] == "旧" and "related_cat" not in data
    k.apply_fields(meta, {"principle": "", "owner_side": "平台侧"}, tree); assert meta["owner_side"] == "平台侧"
    d2 = {"principle": "旧"}; k.apply_fields({"category": "4.1"}, {**d2, "principle": ""}, tree)

def test_render_report_new_sections():
    meta = {"qid": "Q1", "title": "t", "status": "已解决", "source": "官方文档", "tags": [], "related": []}
    out = k.render_report({"phenomenon": "p"}, meta)                               # 老数据：没有新字段
    assert "归属侧：待判断　类型：待判断　审核：未审核" in out and "## 原理说明（给非技术人员看）\n（待补全）" in out
    assert "基准回答" not in out and "AI 补全" not in out and "转技术支持" not in out
    out = k.render_report({"principle": "白话", "reply_script": "AI原稿"},
                          {**meta, "owner_side": "请求侧", "routine": "非常态", "related_cats": ["1.3"], "ai_filled": True,
                           "audit_score": 2, "baseline_reply": "基准 sk-abcdefghijklmnop1234", "audited_at": "2026-11-04"})
    assert "归属侧：请求侧　类型：非常态　审核：2 分（需校正）" in out and "相关分类：1.3" in out and "转技术支持" in out
    assert "AI 补全" in out and "白话" in out and "## 基准回答（人工校正，2026-11-04）" in out and "AI原稿" in out
    assert "abcdefghijklmnop1234" not in out                                       # 基准回答也走脱敏

def test_audit_rules():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        q = _mk(root, tree, "t", "4.1", "b"); p = _mk(root, tree, "待入库", "4.1", "b", pending=True)
        for bad in ["0", "6", "-1", "abc", "", "3.5"]:
            try: k.set_audit(root, q, bad); assert False, bad
            except ValueError: pass
        try: k.set_audit(root, "Q0", 3); assert False
        except KeyError: pass
        assert k.audit_label(k.find_problem(root, q)) == "未审核" and not k.needs_review(k.find_problem(root, q))
        assert k.set_audit(root, q, "4") == "未审核"
        m = k.find_problem(root, q); assert m["audit_score"] == 4 and "baseline_reply" not in m and k.audit_label(m) == "4 分（可用）"
        assert not k.needs_review(m)                                              # 刚好 4 分算可用
        k.set_audit(root, q, 3, "校正 手机13812345678")
        m = k.find_problem(root, q); assert k.needs_review(m) and m["baseline_reply"] == "校正 手机138****5678" == m["audit_note"]
        k.set_audit(root, q, 5); m = k.find_problem(root, q)                      # 不写说明：基准回答保留
        assert m["baseline_reply"] == "校正 手机138****5678" and not k.needs_review(m)
        assert len(m["audit_note"]) < 1001 and "基准回答" in (m["_dir"] / "报告.md").read_text(encoding="utf-8")
        k.set_audit(root, p, 2)                                                   # 待入库的也能审
        assert k.find_problem(root, p)["audit_score"] == 2
        k.set_audit(root, q, 1, "长" * 3000); assert len(k.find_problem(root, q)["baseline_reply"]) == 1000
        mm = k.find_problem(root, q); x = k.read_meta(mm["_dir"]); x["ai_filled"] = True; k.write_meta(mm["_dir"], x)
        assert k.needs_review(k.find_problem(root, q))
        k.set_audit(root, q, 4); assert k.find_problem(root, q)["ai_filled"] is False
    finally:
        shutil.rmtree(root)

def test_related_cats_and_search():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root)
        q = _mk(root, tree, "号被封报401", "4.1", "账号被封")
        d = k.find_problem(root, q)["_dir"]; m = k.read_meta(d); m["related_cats"] = ["1.3", "1.4"]; k.write_meta(d, m)
        for cats in (["1.3"], ["1"], ["4.1"], ["4"]):                              # 主分类和关联分类都查得到
            assert q in [x["qid"] for _, x in k.search_any(root, cats=cats)[1]], cats
        assert k.search_any(root, cats=["2"])[0] == 0
        assert k.in_cats(k.find_problem(root, q), ["1"]) and not k.in_cats(k.find_problem(root, q), ["1.5"])
        k.move_problem(root, tree, q, "1.3")                                       # 改到关联分类：不再重复出现
        assert k.find_problem(root, q)["related_cats"] == ["1.4"]
        assert "相关分类：1.4" in (k.find_problem(root, q)["_dir"] / "报告.md").read_text(encoding="utf-8")
        assert k.tree_summary(root, tree).count("1.3 账号失效、封禁与风控  1") == 1   # 统计只算主分类
    finally:
        shutil.rmtree(root)

def test_tag_hierarchy():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root); v = k.load_vocab(root, create=True)
        _mk(root, tree, "a", "4.1", "x", ["Claude 5", "401"]); _mk(root, tree, "b", "4.1", "x", ["Claude 5", "401", "429"])
        _mk(root, tree, "c", "4.6", "x", ["Gemini 3.1 Pro", "429"]); _mk(root, tree, "d", "4.6", "x", ["Anthropic"])
        _mk(root, tree, "e", "4.6", "x", ["429"]); _mk(root, tree, "f", "4.6", "x", ["号池"]); _mk(root, tree, "g", "4.6", "x", ["Claude 5"], pending=True)
        h = k.tag_hierarchy(root, v)
        assert h["Anthropic"]["Claude 5"] == {"n": 2, "codes": {"401": 2, "429": 1}}                                 # 模型自动挂厂商；待入库不算
        assert h["Google"]["Gemini 3.1 Pro"] == {"n": 1, "codes": {"429": 1}}
        assert h["Anthropic"][k.NO_MODEL] == {"n": 1, "codes": {}}                                                    # 只有厂商
        assert h[k.NO_VENDOR][k.NO_MODEL] == {"n": 1, "codes": {"429": 1}}                                           # 只有错误码
        assert sum(n["n"] for ms in h.values() for n in ms.values()) == 5                                            # “号池”这种既无厂商/模型/错误码的不进层级
        t = k.tag_hierarchy_text(h); assert "【Anthropic】3" in t and "  Claude 5(2)：401(2)、429(1)" in t
        assert "还没有" in k.tag_hierarchy_text({})
    finally:
        shutil.rmtree(root)

def test_search_multiword_tags():
    root = Path(tempfile.mkdtemp())
    try:
        tree = k.load_tree(root); v = k.load_vocab(root, create=True)
        a = _mk(root, tree, "甲 5 号", "4.1", "x", ["Claude 5", "401"]); b = _mk(root, tree, "乙", "4.1", "x", ["Claude 4.6", "401"])
        c = _mk(root, tree, "丙", "4.6", "x", ["Gemini 3.1 Pro", "429"])
        ids = lambda q: sorted(m["qid"] for _, m in k.search(root, q, vocab=v)[1])
        assert k._split_query(v, "#Claude 5 #401 限流") == ["#Claude 5", "#401", "限流"]
        assert k._split_query(v, "#渠道形态:Azure #Gemini 3.1 Pro") == ["#渠道形态:Azure", "#Gemini 3.1 Pro"]
        assert k._split_query(v, "#Claude 限流 5") == ["#Claude", "限流", "5"]            # 后面不是标签名的词不被吞
        assert k._split_query(None, "#Claude 5") == ["#Claude", "5"] and k._split_query(v, "#") == ["#"]
        assert ids("#Claude 5") == [a]                                                   # 不再把 5 当成关键词；Claude 5 不含 Claude 4.6
        assert ids("#Claude 5 #401") == [a] and ids("#Claude 5 #429") == []
        assert ids("#Claude") == sorted([a, b])                                          # 只写 Claude：别名指向厂商，包含名下所有模型
        assert ids("#Gemini 3.1 Pro #429") == [c] and ids("#模型:Gemini 3.1 Pro") == [c]
    finally:
        shutil.rmtree(root)

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
