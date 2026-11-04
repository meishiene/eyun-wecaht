import base64, json, shutil, sys, tempfile, threading, time, urllib.error, urllib.request
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "web")); sys.path.insert(0, str(REPO / "plugins" / "kb_digest"))
import kb_core as k, kb_web as W

def serve(root, password=""):
    s = W.make_server(root, "127.0.0.1", 0, password)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"

def get(url, auth=None, method="GET", data=None):
    req = urllib.request.Request(url, method=method, data=data)
    if auth: req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as r: return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)
    except urllib.error.HTTPError as e: return e.code, e.read().decode("utf-8", "replace"), dict(e.headers)

def mk(root, tree, title, cat, brief, tags=(), status="已解决", pending=False, raw=None, img=None):
    qid = k.next_qid(root)
    base = k.pending_dir(root) if pending else k.cat_dir(root, tree, cat)
    d = base / f"{qid}-{k.slug(title)}"; d.mkdir(parents=True)
    meta = {"qid": qid, "title": title, "category": cat, "status": status, "source": "官方文档", "tags": list(tags), "brief": brief,
            "related": [], "suspect_dup": [], "new_cat": None, "created_at": "2026-11-02 10:00:00"}
    k.write_meta(d, meta); (d / "简要.md").write_text(brief, encoding="utf-8")
    r = {"title": title, "phenomenon": brief, "elements": {"rid": "R-1"}, "solution": "改一下"}
    (d / "报告.json").write_text(json.dumps(r, ensure_ascii=False)); (d / "报告.md").write_text(k.render_report(r, meta), encoding="utf-8")
    items = [("text", raw or "原始消息 <b>不应被渲染</b> sk-abcdefghijklmnop1234")] + ([("img", img)] if img else [])
    k.write_raw(d / "原文", items, "bot1", "s1")
    return qid

def test_markdown():
    h = W.md_to_html("# 标题\n\n- a `x<y`\n- **b**\n\n> 引用\n\n| 列1 | 列2 |\n|---|---|\n| 1 | `a|b` |\n\n```\n<script>\n```\n\n文本 [链接](https://x.com/a?b=1&c=2) 和 [坏](javascript:alert(1))")
    assert "<h2>标题</h2>" in h and "<code>x&lt;y</code>" in h and "<strong>b</strong>" in h and "<blockquote>" in h
    assert "<th>列1</th>" in h and "<code>a|b</code>" in h                       # 反引号内的竖线不拆列
    assert "&lt;script&gt;" in h and "<script>" not in h
    assert 'href="https://x.com/a?b=1&amp;c=2"' in h and "javascript:" not in h.replace("[坏](javascript:alert(1))", "")
    assert "<a href=\"javascript" not in h
    assert "<p></p>" not in W.md_to_html("   \n\n行")                              # 不产生空段落
    assert W.md_to_html("|仅一行|")                                                  # 单独的竖线行不卡死

def test_pages_and_security():
    root = Path(tempfile.mkdtemp()); srv = None
    try:
        tree = k.load_tree(root)
        png = root / "a.png"; png.write_bytes(b"\x89PNG\r\n\x1a\n")
        q1 = mk(root, tree, "网关限流<script>alert(1)</script>", "4.6", "429 限流", ["429", "号池"], img=png)
        q2 = mk(root, tree, "待入库一个", "4.1", "新来的", ["401"], status="未解决", pending=True)
        (root / "手册").mkdir(); (root / "手册" / "排查手册.md").write_text("# 手册\n\n| a | b |\n|---|---|\n| 1 | 2 |\n", encoding="utf-8")
        srv, base = serve(root)
        c, b, h = get(base + "/"); assert c == 200 and "共 <b>1</b> 个问题，待入库 <b>1</b>" in b and "4.6 限流（429）" in b
        assert h["Content-Security-Policy"].startswith("default-src 'none'") and h["X-Content-Type-Options"] == "nosniff"
        c, b, _ = get(base + "/c/4.6"); assert c == 200 and q1 in b
        c, b, _ = get(base + "/c/4"); assert c == 200 and q1 in b and q2 in b           # 大类页包含其下所有细类，含待入库
        c, b, _ = get(base + "/c/4.1"); assert q2 in b and q1 not in b                  # 细类页只含本类
        c, b, _ = get(base + "/c/0"); assert c == 200
        assert get(base + "/c/9.9")[0] == 404 and get(base + "/c/abc")[0] == 404
        c, b, _ = get(base + f"/q/{q1}")
        assert c == 200 and "<script>alert(1)</script>" not in b and "&lt;script&gt;alert(1)&lt;/script&gt;" in b     # 标题转义
        assert "原始消息 &lt;b&gt;不应被渲染&lt;/b&gt; sk-abcdefghijklmnop1234" in b                                       # 原文逐字、转义
        assert f"/img/{q1}/" in b and "原文（1 个批次）" in b and "来源机器人 bot1" in b
        assert "sk-abcdefghijklmnop1234" not in b.split("<h2>报告</h2>")[1].split("<h2>原文")[0]                          # 报告部分不含明文 key
        c, b, _ = get(base + f"/q/{q2}"); assert c == 200 and "待入库" in b
        # 简要里的反引号渲染成代码，且不能因此注入标签
        d2 = k.find_problem(root, q2)["_dir"]; (d2 / "简要.md").write_text("用 `Authorization: Bearer x` 并且 <img src=x onerror=alert(1)>", encoding="utf-8")
        c, b, _ = get(base + f"/q/{q2}"); assert "<code>Authorization: Bearer x</code>" in b and "<img src=x" not in b and "&lt;img src=x" in b
        c, b, _ = get(base + "/search?q=429"); assert c == 200 and q1 in b and "共 1 条" in b
        c, b, _ = get(base + "/search?q=%23401"); assert c == 200 and q2 in b and q1 not in b
        c, b, _ = get(base + "/search?q=" + "%3Cscript%3E"); assert "<script>" not in b
        assert get(base + "/search")[0] == 200 and get(base + "/tags")[0] == 200 and "429" in get(base + "/tags")[1]
        c, b, _ = get(base + "/manual"); assert c == 200 and "<th>a</th>" in b
        # 图片
        name = next((root / "待入库").parent.rglob("images/*")).name
        c, body, h = get(base + f"/img/{q1}/{name}"); assert c == 200 and h["Content-Type"] == "image/png"
        for bad in [f"/img/{q1}/..%2F..%2Fmeta.json", f"/img/{q1}/%2e%2e/meta.json", "/img/Q20250101-001/x.png", f"/img/{q1}/nope.png",
                    f"/img/{q1}/..%2f..%2f..%2f..%2fetc%2fpasswd", "/q/Q1", "/q/../etc/passwd", "/../etc/passwd", "/q/%00", "/nothing"]:
            assert get(base + bad)[0] in (400, 404), bad
        # 只读：非 GET 一律 405
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            assert get(base + "/", method=m, data=b"x" if m in ("POST", "PUT", "PATCH") else None)[0] == 405, m
        assert get(base + "/", method="HEAD")[0] == 200 and get(base + "/healthz")[1] == "ok"
        # 页面出错不崩服务
        orig = W.view_home
        W.view_home = lambda st: (_ for _ in ()).throw(RuntimeError("boom"))
        c, b, _ = get(base + "/"); assert c == 500 and "boom" not in b and "Traceback" not in b
        W.view_home = orig; assert get(base + "/")[0] == 200
    finally:
        if srv: srv.shutdown(); srv.server_close()
        shutil.rmtree(root)

def test_auth():
    root = Path(tempfile.mkdtemp()); srv = None
    try:
        k.load_tree(root); srv, base = serve(root, "s3cret-pw")
        c, b, h = get(base + "/"); assert c == 401 and "Basic" in h["WWW-Authenticate"] and "知识库共" not in b
        assert get(base + "/", "kb:wrong")[0] == 401 and get(base + "/", "kb:")[0] == 401
        assert get(base + "/", "任何用户名:s3cret-pw")[0] == 200
        assert get(base + "/healthz")[0] == 401                                     # 健康检查也不能绕过认证
        assert get(base + "/img/Q20261102-001/a.png")[0] == 401 and get(base + "/q/Q20261102-001")[0] == 401
        c, _, _ = get(base + "/", method="POST", data=b"x"); assert c in (401, 405)
        h2 = urllib.request.Request(base + "/"); h2.add_header("Authorization", "Basic ###")
        try: urllib.request.urlopen(h2, timeout=5); assert False
        except urllib.error.HTTPError as e: assert e.code == 401                    # 畸形认证头
    finally:
        if srv: srv.shutdown(); srv.server_close()
        shutil.rmtree(root)

def test_real_seed_all_pages():
    seed = REPO / "seed" / "stage" / "kb"
    if not seed.exists(): return print("  (跳过：没有种子数据)")
    root = Path(tempfile.mkdtemp()) / "kb"; shutil.copytree(seed, root); srv = None
    try:
        srv, base = serve(root)
        items = k.all_items(root); assert len(items) == 109
        t0 = time.time(); bad = []
        for m in items:
            c, b, _ = get(base + f"/q/{m['qid']}")
            if c != 200 or "<h2>原文" not in b or "Traceback" in b: bad.append((m["qid"], c))
        assert not bad, bad[:5]
        for code in [s["code"] for mj in k.load_tree(root) for s in mj["subs"]] + ["0"] + [str(i) for i in range(8)]:
            assert get(base + f"/c/{code}")[0] == 200, code
        c, b, _ = get(base + "/search?q=401"); assert c == 200 and "Q20261102-041" in b
        c, b, _ = get(base + "/manual"); assert c == 200 and "<table>" in b and "排查三步法" in b
        c, b, _ = get(base + "/"); assert "共 <b>109</b> 个问题" in b
        print(f"  109 个问题页 + 全部分类页 + 手册 {time.time()-t0:.1f}s")
    finally:
        if srv: srv.shutdown(); srv.server_close()
        shutil.rmtree(root.parent)

def test_backup_box():
    d = Path(tempfile.mkdtemp()); f = d / "LAST.json"; now = 1_800_000_000
    try:
        assert W.backup_box("", now) == ""                                           # 没配置 → 不显示
        assert "尚未运行" in W.backup_box(str(f), now) and "box bad" in W.backup_box(str(f), now)   # 文件不存在 → 告警
        f.write_text("{坏的json"); assert "无法读取" in W.backup_box(str(f), now)
        ok = {"ok": True, "epoch": now - 3600, "last_ok_epoch": now - 3600, "last_ok_time": "2026-11-03 03:30:00", "time": "2026-11-03 03:30:00", "kept": 5, "size": 204800, "problems": 109}
        f.write_text(json.dumps(ok)); b = W.backup_box(str(f), now); assert "box good" in b and "1 小时前" in b and "109 个问题" in b and "共 5 份" in b
        f.write_text(json.dumps({**ok, "last_ok_epoch": now - 40 * 3600})); b = W.backup_box(str(f), now); assert "box bad" in b and "40 小时" in b       # 超过 36 小时
        f.write_text(json.dumps({**ok, "ok": False, "error": "磁盘已满<script>", "time": "2026-11-03 03:30:00"}))
        b = W.backup_box(str(f), now); assert "box bad" in b and "磁盘已满&lt;script&gt;" in b and "<script>" not in b           # 最近失败 + 转义
        f.write_text(json.dumps({"ok": False, "error": "x", "time": "t", "last_ok_epoch": None})); assert "从未成功" in W.backup_box(str(f), now)
        # 首页确实带上了这个条
        root = Path(tempfile.mkdtemp()); k.load_tree(root); f.write_text(json.dumps({**ok, "last_ok_epoch": int(time.time()) - 60, "epoch": int(time.time())}))
        s = W.make_server(root, "127.0.0.1", 0, "", str(f)); threading.Thread(target=s.serve_forever, daemon=True).start()
        try: assert "最近备份" in get(f"http://127.0.0.1:{s.server_address[1]}/")[1]
        finally: s.shutdown(); s.server_close(); shutil.rmtree(root)
    finally:
        shutil.rmtree(d)

def test_new_fields_pages():
    root = Path(tempfile.mkdtemp()); srv = None
    try:
        tree = k.load_tree(root)
        q = mk(root, tree, "号被封报401", "4.1", "账号被封", ["Claude 5", "401"])
        q2 = mk(root, tree, "普通问题", "4.6", "限流", ["Claude 5", "429"])
        d = k.find_problem(root, q)["_dir"]; m = k.read_meta(d)
        m.update({"owner_side": "供应侧", "routine": "非常态", "related_cats": ["1.3"], "ai_filled": True})
        k.write_meta(d, m)
        r = json.loads((d / "报告.json").read_text(encoding="utf-8")); r["principle"] = "上游封号，所以被拒绝 <b>x</b>"
        (d / "报告.json").write_text(json.dumps(r, ensure_ascii=False)); k.refresh_report(d, m)
        srv, base = serve(root)
        c, b, _ = get(base + f"/q/{q}")
        assert c == 200 and "归属侧：<b>供应侧</b>" in b and "类型：<b>非常态</b>" in b and "审核：<b>未审核</b>" in b
        assert "建议转技术支持" in b and "由 AI 补全" in b and "同时牵涉" in b and "href='/c/1.3'" in b and "1.3 账号失效、封禁与风控" in b
        assert "原理说明（给非技术人员看）" in b and "上游封号，所以被拒绝 &lt;b&gt;x&lt;/b&gt;" in b and "<b>x</b>" not in b   # 转义
        c, b, _ = get(base + f"/q/{q2}"); assert "归属侧：<b>待判断</b>" in b and "建议转技术支持" not in b and "由 AI 补全" not in b   # 老数据不报错
        c, b, _ = get(base + "/c/1.3"); assert c == 200 and q in b and "号被封报401" in b             # 关联分类页也列出来
        c, b, _ = get(base + "/c/1.4"); assert q not in b
        (root / "手册").mkdir(exist_ok=True); (root / "手册" / "排查手册.md").write_text("# 手册\n\n正文\n", encoding="utf-8")
        assert "分类边界" not in get(base + "/manual")[1]                                              # 没有这份文件时手册照常
        (root / "手册" / "分类边界.md").write_text("## 附：分类边界与答复规范\n\n按根因定\n", encoding="utf-8")
        b = get(base + "/manual")[1]; assert "正文" in b and "附：分类边界与答复规范" in b and "按根因定" in b
        c, b, _ = get(base + "/c/4.1"); assert "badge warn'>非常态" in b and "badge info'>待审" in b
        c, b, _ = get(base + "/c/4.6"); assert "非常态" not in b and "待审" not in b
        k.set_audit(root, q, 5)
        c, b, _ = get(base + f"/q/{q}"); assert "审核：<b>5 分（可用）</b>" in b and "由 AI 补全" not in b
        assert "待审" not in get(base + "/c/4.1")[1]
        c, b, _ = get(base + "/tags")
        assert c == 200 and "层级视图" in b and "href='/search?q=%23Anthropic'" in b and "href='/search?q=%23Claude%205%20%23401'" in b
        assert "href='/search?q=%23Claude%205%20%23429'" in b
    finally:
        if srv: srv.shutdown(); srv.server_close()
        shutil.rmtree(root)

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
