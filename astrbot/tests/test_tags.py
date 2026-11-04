"""标签词表：归一、搜索、待确认、别名、统计、重打标签脚本、网页标签页。"""
import json, sys, tempfile, threading, urllib.request
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "kb_digest")); sys.path.insert(0, str(REPO / "seed")); sys.path.insert(0, str(REPO / "web"))
import kb_core as k, retag as R, kb_web as W


def vocab():
    return k.Vocab(json.loads((REPO / "plugins" / "kb_digest" / "default_tag_vocab.json").read_text(encoding="utf-8")))


def mk(root, tree, title, tags, cat="4.6", pending=False):
    qid = k.next_qid(root)
    base = k.pending_dir(root) if pending else k.cat_dir(root, tree, cat)
    d = base / f"{qid}-{k.slug(title)}"; d.mkdir(parents=True)
    meta = {"qid": qid, "title": title, "category": cat, "status": "已解决", "source": "待验证", "tags": list(tags),
            "brief": title, "related": [], "suspect_dup": [], "new_cat": None}
    k.write_meta(d, meta)
    r = {"title": title, "phenomenon": title, "elements": {}, "tags": list(tags)}
    (d / "报告.json").write_text(json.dumps(r, ensure_ascii=False)); (d / "报告.md").write_text(k.render_report(r, meta), encoding="utf-8")
    return qid


def kbroot():
    root = Path(tempfile.mkdtemp()) / "kb"; tree = k.load_tree(root)
    return root, tree


def test_default_vocab_is_sound():
    v = vocab(); keys = {}
    assert v.groups == k.TAG_GROUPS
    for t in v.tags.values():
        assert t["group"] in v.groups, t
        for x in [t["name"], *t.get("aliases", [])]:
            assert k._tkey(x) not in keys, ("规范名/别名重复", x)       # 不同标签之间不能共用写法
            keys[k._tkey(x)] = t["name"]
        if t["group"] == "模型":
            assert v.group_of(t.get("vendor")) == "厂商", ("模型没有挂到厂商", t["name"])
        else:
            assert "vendor" not in t
    assert len(v.tags) >= 70


def test_normalize():
    v = vocab()
    tags, new = v.normalize(["gemini-3.1-pro", "VT", "vt", " 429 ", "Claude", "号池", "账号池", "全新标签", "全新标签", "", "x" * 40, "带#号"])
    assert tags == ["Anthropic", "Google", "Gemini 3.1 Pro", "Vertex", "号池", "429"]   # 按类排序；模型补了 Google
    assert tags.count("Vertex") == 1 and tags.count("号池") == 1                    # 别名/大小写合并，不重复
    assert new == ["全新标签"]                                                        # 过长、带 # 的不当成新标签
    assert [t for t in tags if v.group_of(t) == "厂商"] == tags[:len([t for t in tags if v.group_of(t) == "厂商"])]   # 按类排序，厂商在最前
    t2, _ = v.normalize(["Claude 5"]); assert t2 == ["Anthropic", "Claude 5"]       # 模型自动补厂商
    many = [n for n, t in v.tags.items() if t["group"] in ("其他", "错误码")][:20]
    t3, _ = v.normalize(many + ["号池"]); assert len(t3) == 12 and "号池" in t3                # 上限 12，先砍排在后面的类（其他）
    assert v.normalize(None) == ([], []) and v.normalize([None, 5])[0] == []


def test_search_by_tag():
    root, tree = kbroot(); v = vocab()
    a = mk(root, tree, "Gemini 限流", ["Google", "Gemini 3.1 Pro", "Vertex", "429"])
    b = mk(root, tree, "Claude 掉线", ["Anthropic", "Claude 5", "号池", "Azure"])
    c = mk(root, tree, "旧数据", ["某个老标签", "gemini-old"])                        # 没归一过的旧标签
    f = lambda q: {m["qid"] for _, m in k.search(root, q, vocab=v)[1]}
    assert f("#3pro") == {a} and f("#gemini") == {a} and f("#GEMINI 3.1 PRO") == {a}   # 别名/大小写
    assert f("#vt") == {a} and f("#号池") == {b}
    only_models = mk(root, tree, "只有模型", ["Claude 5"])                             # 没写厂商的旧数据，搜厂商也要能命中
    assert f("#anthropic") == {b, only_models} and f("#claude") == {b, only_models}
    assert f("#渠道:vt") == {a} and f("#渠道形态:Azure") == {b}                        # 限定类，类名可简写
    assert f("#厂商:Vertex") == set() and f("#模型:Vertex") == set()                    # 类不对就不命中
    assert f("#429 #vt") == {a} and f("#429 #号池") == set()                            # 多个标签都要命中
    assert f("限流 #vt") == {a}
    assert f("#老标") == {c}                                                           # 词表不认识的退回子串匹配
    assert f("#不存在") == set() and f("#渠道:不存在") == set()
    old = {m["qid"] for _, m in k.search(root, "#号池")[1]}; assert old == {b}          # 不传词表时行为和以前一样


def test_pending_approve_alias():
    root, tree = kbroot(); v = vocab()
    q1 = mk(root, tree, "q1", ["号池"]); q2 = mk(root, tree, "q2", ["Google"], pending=True)
    k.add_pending_tags(root, q1, ["Brand New", "brand new", "另一个"]); k.add_pending_tags(root, q2, ["Brand New"])
    p = k.load_pending_tags(root); assert p["Brand New"]["qids"] == [q1, q2] and "另一个" in p     # 大小写不同算同一个
    # 同意
    for bad, msg in [(("不存在", "其他"), "待确认里没有"), (("Brand New", "乱写"), "类要写"), (("Brand New", "模型", "号池"), "不是词表里的厂商")]:
        try: k.approve_new_tag(root, v, *bad); assert False
        except ValueError as e: assert msg in str(e), e
    name, done = k.approve_new_tag(root, v, "brand new", "模型", "openai")               # 厂商也可以写别名
    assert name == "Brand New" and done == [q1, q2]
    assert v.vendor_of("Brand New") == "OpenAI" and v.canon("BRAND NEW") == "Brand New"
    assert k.find_problem(root, q1)["tags"] == ["OpenAI", "Brand New", "号池"]            # 补了厂商，按类排序
    assert "Brand New" in (k.find_problem(root, q1)["_dir"] / "报告.md").read_text(encoding="utf-8")
    assert "Brand New" in json.loads((k.find_problem(root, q2)["_dir"] / "报告.json").read_text(encoding="utf-8"))["tags"]
    assert "Brand New" not in k.load_pending_tags(root) and "另一个" in k.load_pending_tags(root)
    assert json.loads((root / k.VOCAB_FILE).read_text(encoding="utf-8"))["tags"][-1]["name"] == "Brand New"    # 落盘
    try: k.approve_new_tag(root, v, "号池", "其他"); assert False
    except ValueError: pass
    # 别名：规范名带空格；撞车；顺带把待确认里同名的并过来
    k.add_pending_tags(root, q1, ["gpt-x"])
    c, done = k.add_alias(root, v, "Brand New", "gpt-x"); assert c == "Brand New" and done == [q1]
    assert "gpt-x" not in k.load_pending_tags(root)
    for args, msg in [(("Brand New", "号池"), "已经是"), (("没有这个", "abc"), "词表里没有"), (("Brand New", "a#b"), "不能")]:
        try: k.add_alias(root, v, *args); assert False
        except ValueError as e: assert msg in str(e), e
    assert k.split_alias_args(v, ["Gemini", "3.1", "Pro", "g3p"]) == ("Gemini 3.1 Pro", "g3p")
    assert k.split_alias_args(v, ["Gemini", "3.1", "Pro", "my", "alias"]) == ("Gemini 3.1 Pro", "my alias")
    assert k.split_group_args(v, ["Claude", "4.8", "模型", "Anthropic"]) == ("Claude 4.8", "模型", "Anthropic")
    assert k.split_group_args(v, ["x", "渠道"]) == ("x", "渠道形态", "") and k.split_group_args(v, ["x"]) == ("x", None, "")
    # 合并后待确认标签改挂
    k.add_pending_tags(root, q2, ["留着"]); k.retarget_pending_tags(root, q2, q1)
    assert k.load_pending_tags(root)["留着"]["qids"] == [q1]


def test_stats_and_text():
    root, tree = kbroot(); v = vocab()
    mk(root, tree, "a", ["Google", "Gemini 3.1 Pro", "429"]); mk(root, tree, "b", ["gemini-3.1-pro", "老标签"])
    mk(root, tree, "待入库不算", ["号池"], pending=True)
    st = k.tag_stats(root, v)
    assert st["模型"] == {"Gemini 3.1 Pro": 2} and st["错误码"] == {"429": 1} and st[k.UNGROUPED] == {"老标签": 1}
    assert "渠道形态" not in st                                                          # 待入库的不统计
    assert k.tags_text(v, ["Google", "Gemini 3.1 Pro", "老标签"]) == "厂商：Google　模型：Gemini 3.1 Pro　未归类：老标签"
    assert k.tags_text(v, []) == "无"
    pt = v.prompt_text().split("\n")
    assert [l.split("：")[0] for l in pt] == v.groups and "Claude 5" in pt[1] and pt[4].startswith("错误码：400、")


def test_load_vocab_creates_only_when_asked():
    root, _ = kbroot()
    k.load_vocab(root); assert not (root / k.VOCAB_FILE).exists()                       # 网页只读，不落盘
    k.load_vocab(root, create=True); assert (root / k.VOCAB_FILE).exists()
    p = root / k.VOCAB_FILE; obj = json.loads(p.read_text(encoding="utf-8")); obj["tags"].append({"name": "手改", "group": "其他", "aliases": ["sg"]})
    p.write_text(json.dumps(obj, ensure_ascii=False)); assert k.load_vocab(root).canon("SG") == "手改"   # 手改立即生效


def test_retag_plan_and_apply():
    root, tree = kbroot()
    a = mk(root, tree, "Gemini 限流", ["VT", "vt", "gemini-3.1-pro", "429", "模型", "CLI"])
    b = mk(root, tree, "Claude 掉线", ["Claude", "账号池", "新玩意"], pending=True)
    c = mk(root, tree, "已规范", ["Anthropic", "号池"])
    out = Path(tempfile.mkdtemp())
    R.main(["plan", "--kb", str(root), "--out", str(out)])
    plan = json.loads((out / "retag_plan.json").read_text(encoding="utf-8")); P = {p["qid"]: p for p in plan["items"]}
    assert P[a]["new"] == ["Google", "Gemini 3.1 Pro", "Vertex", "429"] and P[a]["dropped"] == ["模型", "CLI"]
    assert P[b]["new"] == ["Anthropic", "号池"] and P[b]["pending"] == ["新玩意"] and plan["pending_tags"] == {"新玩意": [b]}
    assert P[c]["old"] == P[c]["new"]
    md = (out / "标签改动清单.md").read_text(encoding="utf-8")
    assert "**Vertex** ←" in md and "未启用" in md and "新玩意" in md and a in md
    assert k.find_problem(root, a)["tags"][0] == "VT"                                   # 出计划不改知识库
    assert not (root / k.VOCAB_FILE).exists()
    # 出计划之后有人手动改了 c 的标签 → 写入时跳过，不覆盖
    plan_c = dict(P[c]); plan_c["new"] = ["Anthropic"]; plan["items"] = [P[a], P[b], plan_c]
    k.set_item_tags(k.find_problem(root, c)["_dir"], ["号池", "手改了"])
    r = R.apply_plan(root, plan, False); assert r["changed"] == 2 and r["skipped"] == 1, r
    assert k.find_problem(root, a)["tags"] == ["Google", "Gemini 3.1 Pro", "Vertex", "429"]
    assert k.find_problem(root, c)["tags"] == ["号池", "手改了"]
    assert "Vertex" in (k.find_problem(root, a)["_dir"] / "报告.md").read_text(encoding="utf-8")
    assert k.load_pending_tags(root) == {"新玩意": {"qids": [b]}} and (root / k.VOCAB_FILE).exists()
    assert R.apply_plan(root, plan, False)["changed"] == 0                              # 重复执行没有副作用
    assert k.git(root, "log", "--oneline").count("标签重整") == 1
    # LLM 补的标签可以整体不写
    p2 = dict(P[a]); p2["old"] = ["Google", "Gemini 3.1 Pro", "Vertex", "429"]; p2["llm_added"] = ["号池"]
    p2["new"] = ["Google", "Gemini 3.1 Pro", "Vertex", "号池", "429"]
    R.apply_plan(root, {"items": [p2], "pending_tags": {}}, True)
    assert "号池" not in k.find_problem(root, a)["tags"]
    R.apply_plan(root, {"items": [p2], "pending_tags": {}}, False)
    assert "号池" in k.find_problem(root, a)["tags"]


def test_web_tags_page():
    root, tree = kbroot()
    mk(root, tree, "Gemini 限流", ["Google", "Gemini 3.1 Pro", "Vertex", "429"]); mk(root, tree, "Claude", ["Anthropic", "Claude 5", "号池"])
    mk(root, tree, "老数据", ["老标签"]); k.add_pending_tags(root, "Q1", ["待定标签"])
    s = W.make_server(root, "127.0.0.1", 0, ""); threading.Thread(target=s.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{s.server_address[1]}"
    page = urllib.request.urlopen(base + "/tags", timeout=10).read().decode()
    for g in ["厂商", "模型", "渠道形态", "错误码", "未归类", "待确认"]:
        assert f"<h2>{g} " in page, g
    assert "Gemini 3.1 Pro" in page and "待定标签" in page and "<h2>接入工具" not in page   # 空的类不显示
    assert page.index("<h2>厂商") < page.index("<h2>模型") < page.index("<h2>渠道形态")
    assert not (root / k.VOCAB_FILE).exists()                                           # 网页不写知识库
    res = urllib.request.urlopen(base + "/search?q=%23gemini", timeout=10).read().decode()
    assert "Gemini 限流" in res and "Claude" not in res          # 别名搜索只命中 Gemini 那条
    s.shutdown()


if __name__ == "__main__":
    fns = [v for n, v in sorted(globals().items()) if n.startswith("test_")]
    for f in fns:
        f(); print("ok", f.__name__)
    print(f"{len(fns)} passed")
