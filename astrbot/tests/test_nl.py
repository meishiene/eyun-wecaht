"""白话指令：唤醒词、确认词、大模型输出的校验、转指令、语义查询。"""
import json, sys, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "plugins" / "kb_digest"))
import kb_core as k, nl_intent as nl

V = k.Vocab(json.loads((REPO / "plugins" / "kb_digest" / "default_tag_vocab.json").read_text(encoding="utf-8")))
TREE = k._default_tree_obj()


def test_wake():
    ok = {"@11 帮我整理刚发的": "帮我整理刚发的", "＠11,找一下计费": "找一下计费", "  @ 11：入库": "入库", "@11": "", "@11帮我看看": "帮我看看"}
    for t, rest in ok.items():
        assert nl.strip_wake(t) == (True, rest), t
    for t in ["@110 帮我", "@111", "11 帮我整理", "帮我 @11 整理", "@1", "/整理", "客户：@11 在吗", ""]:
        assert nl.strip_wake(t)[0] is False, t                 # 不是行首的 @11 / 别的号码 / 普通话，都不是指令


def test_confirm_words():
    for t in ["是", "是的", "确认", "好的", " 好。", "OK", "yes", "执行"]:
        assert nl.confirm_answer(t) is True, t
    for t in ["否", "取消", "算了", "不用了", "No"]:
        assert nl.confirm_answer(t) is False, t
    for t in ["嗯", "是不是", "好像不行", "确认一下吧", "", "入库"]:
        assert nl.confirm_answer(t) is None, t                  # 只认整句，长句不算回复


def test_validate_ok():
    v = lambda o: nl.validate(o, TREE, V)
    assert v({"intent": "digest", "args": {"title": " 限流 无日志 "}}) == {"intent": "digest", "args": {"title": "限流 无日志"}}
    assert v({"intent": "commit"}) == {"intent": "commit", "args": {}}
    assert v({"intent": "show", "args": {"qid": "41"}})["args"] == {"qid": "41"} and v({"intent": "show", "args": {"qid": "q20261102-041"}})["intent"] == "show"
    r = v({"intent": "search", "args": {"keywords": ["计费", "扣费", "", None, "x" * 30], "categories": ["2", "2.3", "9", "abc"], "tags": ["gemini", "乱写", "VT"]}})
    assert r["args"] == {"keywords": ["计费", "扣费", "x" * 12], "categories": ["2", "2.3"], "tags": ["Google", "Vertex"]}    # 非法分类/标签被剔除，别名归一
    assert v({"intent": "search", "args": {"tags": ["号池"]}})["intent"] == "search"
    assert v({"intent": "tags", "args": {"group": "渠道"}})["args"] == {"group": "渠道形态"} and v({"intent": "tags"})["args"] == {"group": ""}
    assert v({"intent": "set_status", "args": {"qid": "41", "status": "已解决"}})["intent"] == "set_status"
    assert v({"intent": "recat", "args": {"qid": "41", "code": "4.6"}})["args"] == {"qid": "41", "code": "4.6"}
    assert v({"intent": "merge", "args": {"src": "110", "dst": "41"}})["intent"] == "merge"
    assert v({"intent": "approve_tag", "args": {"name": "Claude 4.8", "group": "模型", "vendor": "anthropic"}})["args"]["vendor"] == "Anthropic"
    assert v({"intent": "add_alias", "args": {"name": "Gemini 3.1 Pro", "alias": "g3p"}})["intent"] == "add_alias"


def test_validate_rejects():
    v = lambda o: nl.validate(o, TREE, V)
    bad = [None, "字符串", [], {}, {"intent": ""}, {"intent": "delete_all"}, {"intent": "rm -rf", "args": {}}, {"intent": "unknown", "say": "x"},
           {"intent": "show", "args": {"qid": "drop table"}}, {"intent": "show", "args": {}},
           {"intent": "search", "args": {}}, {"intent": "search", "args": {"categories": ["99"], "keywords": [], "tags": ["不存在"]}},
           {"intent": "set_status", "args": {"qid": "41", "status": "随便"}}, {"intent": "set_status", "args": {"qid": "41"}},
           {"intent": "recat", "args": {"qid": "41", "code": "4"}}, {"intent": "recat", "args": {"qid": "41", "code": "9.9"}},   # 只能改到细类
           {"intent": "merge", "args": {"src": "110"}}, {"intent": "approve_tag", "args": {"name": "x", "group": "乱写"}},
           {"intent": "add_alias", "args": {"name": "x"}}, {"intent": "commit", "args": "不是字典"}]
    for o in bad:
        r = v(o)
        if o == {"intent": "commit", "args": "不是字典"}:
            assert r["intent"] == "commit" and r["args"] == {}                         # args 不是字典也不会带进任何参数
        else:
            assert r["intent"] == "unknown", o
    assert nl.validate({"intent": "unknown", "say": "不太明白" * 100}, TREE, V)["say"].__len__() <= 120


def test_write_set_and_describe():
    assert nl.WRITE_INTENTS == {"clear", "commit", "set_status", "recat", "merge", "approve_cat", "approve_tag", "add_alias"}
    for n in ("digest", "search", "show", "count", "pending", "tree", "tags", "help"):
        assert n not in nl.WRITE_INTENTS                                               # 只读和整理不需要确认
    it = {"intent": "merge", "args": {"src": "110", "dst": "41"}}
    assert "110 合并进 41" in nl.describe(it) and "110 会消失" in nl.describe(it)
    assert "3 个待入库" in nl.describe({"intent": "commit", "args": {}}, pend=3)
    assert "文字 2 段" in nl.describe({"intent": "clear", "args": {}}, buffered="文字 2 段、图片 0 张")


def test_command_text_roundtrip():
    c = lambda n, **a: nl.command_text({"intent": n, "args": a})
    assert c("digest", title="") == "整理" and c("digest", title="限流") == "整理 限流"
    assert c("commit") == "入库" and c("show", qid="41") == "看 41" and c("tags", group="") == "标签" and c("tags", group="厂商") == "标签 厂商"
    assert c("set_status", qid="41", status="已解决") == "状态 41 已解决" and c("recat", qid="41", code="4.6") == "改类 41 4.6"
    assert c("merge", src="110", dst="41") == "合并 110 41" and c("approve_cat", name="", major="") == "同意新类"
    assert c("approve_tag", name="Claude 4.8", group="模型", vendor="Anthropic") == "同意新标签 Claude 4.8 模型 Anthropic"
    assert c("add_alias", name="Gemini 3.1 Pro", alias="g3p") == "加别名 Gemini 3.1 Pro g3p"
    # 每个写意图都能转成指令（不会 KeyError）
    for n in nl.WRITE_INTENTS | {"digest", "count", "pending", "tree", "help"}:
        assert nl.command_text({"intent": n, "args": {"qid": "1", "status": "已解决", "code": "4.6", "src": "1", "dst": "2", "name": "n", "alias": "a", "group": "其他"}})


def test_prompt_contains_context():
    p = nl.build_prompt("找一下计费错误的问题", TREE, V)
    assert "@11" in p and "2.1 倍率与定价" in p and "渠道形态：" in p and "已解决" in p and p.rstrip().endswith("找一下计费错误的问题")


def _mk(root, tree, title, cat, brief, tags=(), report="", pending=False):
    qid = k.next_qid(root)
    base = k.pending_dir(root) if pending else k.cat_dir(root, tree, cat)
    d = base / f"{qid}-{k.slug(title)}"; d.mkdir(parents=True)
    meta = {"qid": qid, "title": title, "category": cat, "status": "已解决", "source": "待验证", "tags": list(tags), "brief": brief,
            "related": [], "suspect_dup": [], "new_cat": None}
    k.write_meta(d, meta)
    r = {"title": title, "phenomenon": report or brief, "elements": {}}
    (d / "报告.json").write_text(json.dumps(r, ensure_ascii=False)); (d / "报告.md").write_text(k.render_report(r, meta), encoding="utf-8")
    return qid


def test_search_any():
    root = Path(tempfile.mkdtemp()) / "kb"; tree = k.load_tree(root)
    a = _mk(root, tree, "扣费比预期多", "2.3", "客户反映扣费比预期多", ["Google", "号池"])
    b = _mk(root, tree, "倍率怎么算", "2.1", "倍率与价格说明", ["Anthropic"])
    c = _mk(root, tree, "429 限流", "4.6", "客户频繁 429", ["429", "号池"])
    d = _mk(root, tree, "待入库的扣费", "2.3", "扣费", pending=True)
    f = lambda **kw: {m["qid"] for _, m in k.search_any(root, vocab=V, limit=50, **kw)[1]}
    assert f(keywords=["计费", "扣费", "倍率"]) == {a, b, d}                       # 任一命中即可（AND 的 /查 做不到）
    assert f(keywords=["扣费", "429"]) == {a, c, d}
    assert f(keywords=["扣费", "429"], cats=["2"]) == {a, d}                     # 大类 = 它下面所有细类
    assert f(cats=["2.3"]) == {a, d} and f(cats=["2.1", "4"]) == {b, c}           # 只给分类就按分类列
    assert f(cats=["2"], keywords=["扣费"], tags=["号池"]) == {a}                 # 标签是“都要命中”
    assert f(tags=["Google"]) == {a} and f(tags=["号池"]) == {a, c}
    assert f(keywords=["完全无关xyz"]) == set() and f(cats=["7"]) == set()
    assert f(cats=["4"]) == {c} and f(cats=["4.6"], keywords=["429"]) == {c}      # 大类编号按“.”前缀匹配，不会把 4 当成 40
    n, hits = k.search_any(root, ["扣费"], limit=1); assert n == 2 and len(hits) == 1                 # 总数和显示条数分开


if __name__ == "__main__":
    fns = [v for n, v in sorted(globals().items()) if n.startswith("test_")]
    for fn in fns:
        fn(); print("ok", fn.__name__)
    print(f"{len(fns)} passed")
