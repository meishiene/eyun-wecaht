import datetime as dt, hashlib, json, os, shutil, sys, tarfile, tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backup")); sys.path.insert(0, str(REPO / "plugins" / "kb_digest"))
import kb_backup as B, kb_core as k

def mk_src(n=3):
    src = Path(tempfile.mkdtemp()); kbp = src / "data" / "kb"; tree = k.load_tree(kbp)
    for i in range(n):
        d = k.cat_dir(kbp, tree, "4.6") / f"Q20261102-{i+1:03d}-问题{i}"; d.mkdir(parents=True)
        k.write_meta(d, {"qid": f"Q20261102-{i+1:03d}", "title": f"问题{i}", "category": "4.6"})
        (d / "原文").mkdir(); (d / "原文" / "原文-01-x.md").write_text(f"原文{i} sk-abcdefghijklmnop1234 中文", encoding="utf-8")
    pd = k.pending_dir(kbp) / "Q20261103-001-待入库"; pd.mkdir(parents=True); k.write_meta(pd, {"qid": "Q20261103-001", "title": "t", "category": "0"})
    (kbp / ".buffers" / "u").mkdir(parents=True); (kbp / ".buffers" / "u" / "state.json").write_text("{}")
    k.git_commit(kbp, "初始")
    (src / "data" / "plugins" / "kb_digest").mkdir(parents=True); (src / "data" / "plugins" / "kb_digest" / "main.py").write_text("x")
    (src / "data" / "plugins" / "kb_digest" / "__pycache__").mkdir(); (src / "data" / "plugins" / "kb_digest" / "__pycache__" / "a.pyc").write_bytes(b"x")
    (src / "web").mkdir(); (src / "web" / "kb_web.py").write_text("y")
    (src / "data" / "cmd_config.json").write_text('{"secret":"不该被备份"}')          # 配置/密钥不在备份范围
    (src / "mihomo").mkdir(); (src / "mihomo" / "config.yaml").write_text("password: 不该被备份")
    return src

def go(src, base, now, **kw):
    return B.run(src, base / "kb", base / "status", now=now, retry_wait=0, **kw)

def test_backup_contents_and_restore():
    src = mk_src(); base = Path(tempfile.mkdtemp())
    try:
        st = go(src, base, dt.datetime(2026, 11, 3, 3, 30, 0))
        assert st["ok"] and st["file"] == "kb-20261103-033000.tar.gz" and st["problems"] == 4 and st["kept"] == 1 and st["git_head"]
        a = base / "kb" / st["file"]
        assert oct(a.stat().st_mode & 0o777) == "0o600" and oct((base / "kb").stat().st_mode & 0o777) == "0o700"
        assert (base / "kb" / (st["file"] + ".sha256")).read_text().split()[0] == hashlib.sha256(a.read_bytes()).hexdigest() == st["sha256"]
        names = tarfile.open(a).getnames()
        assert not any("cmd_config" in n or "mihomo" in n or "__pycache__" in n or n.endswith(".pyc") for n in names)   # 不含密钥/缓存
        assert "data/kb/.git/HEAD" in names and any(".buffers/u/state.json" in n for n in names)                        # 含 git 历史和缓冲区
        assert "web/kb_web.py" in names and "data/plugins/kb_digest/main.py" in names
        assert not [p for p in (base / "kb").iterdir() if p.name.startswith(".tmp")]                                   # 临时文件已清
        sj = json.loads((base / "status" / "LAST.json").read_text(encoding="utf-8")); assert sj["ok"] and sj["last_ok_epoch"] == sj["epoch"]
        # 真正还原：解压后目录树和原目录逐文件一致，git 历史可用，中文/原文无损
        out = Path(tempfile.mkdtemp()); tarfile.open(a).extractall(out, filter="data")
        for f in (src / "data" / "kb").rglob("*"):
            if f.is_file():
                g = out / "data" / "kb" / f.relative_to(src / "data" / "kb")
                assert g.exists() and g.read_bytes() == f.read_bytes(), f
        assert "初始" in k.git(out / "data" / "kb", "log", "--format=%s") and k.git(out / "data" / "kb", "fsck") is not None
        shutil.rmtree(out)
    finally:
        shutil.rmtree(src); shutil.rmtree(base)

def test_retention_policy():
    base = Path(tempfile.mkdtemp()); d = base / "kb"; d.mkdir()
    try:
        start = dt.date(2026, 8, 1)
        for i in range(100):                                               # 100 天，每天一份
            day = start + dt.timedelta(days=i)
            (d / f"kb-{day:%Y%m%d}-033000.tar.gz").write_text("x"); (d / f"kb-{day:%Y%m%d}-033000.tar.gz.sha256").write_text("h")
        (d / "别的文件.txt").write_text("不该被动"); (d / "kb-bad.tar.gz").write_text("命名不符，不该被动")
        gone = B.prune(d, 14, 8)
        left = sorted(p.name for p in d.iterdir() if B.NAME_RE.match(p.name))
        newest = [f"kb-{(start + dt.timedelta(days=i)):%Y%m%d}-033000.tar.gz" for i in range(99, 85, -1)]
        assert all(n in left for n in newest)                               # 最近 14 份都在
        sundays = [n for n in left if n not in newest]
        assert len(sundays) <= 8 and all(dt.datetime.strptime(n[3:11], "%Y%m%d").weekday() == 6 for n in sundays)   # 其余只留周日
        assert len(left) <= 22 and len(left) >= 14
        assert (d / "别的文件.txt").exists() and (d / "kb-bad.tar.gz").exists()
        assert not [p for p in d.glob("*.sha256") if not (d / p.name[:-7]).exists()]          # 校验文件不遗留
        assert len(gone) == 100 - len(left)
    finally:
        shutil.rmtree(base)

def test_failure_never_deletes_old_backups():
    src = mk_src(); base = Path(tempfile.mkdtemp())
    try:
        for i in range(20):
            d = dt.datetime(2026, 10, 1) + dt.timedelta(days=i); go(src, base, d, keep_daily=100, keep_weekly=100)
        before = sorted(p.name for p in (base / "kb").iterdir())
        assert len(before) == 40
        good = json.loads((base / "status" / "LAST.json").read_text(encoding="utf-8"))
        shutil.rmtree(src / "data" / "kb")                                  # 知识库被删/路径错了
        st = go(src, base, dt.datetime(2026, 11, 3, 3, 30), keep_daily=1, keep_weekly=0)   # 即使保留策略很激进
        assert not st["ok"] and "找不到知识库" in st["error"]
        assert sorted(p.name for p in (base / "kb").iterdir()) == before    # 旧备份一个都没少
        sj = json.loads((base / "status" / "LAST.json").read_text(encoding="utf-8"))
        assert sj["ok"] is False and sj["last_ok_epoch"] == good["epoch"]   # 记得上次成功的时间，供网页告警
    finally:
        shutil.rmtree(src, ignore_errors=True); shutil.rmtree(base)

def test_write_during_backup_is_retried_then_consistent():
    src = mk_src(); base = Path(tempfile.mkdtemp()); orig = B._count_problems; calls = {"n": 0}
    def flaky(b):
        calls["n"] += 1
        r = orig(b)
        if calls["n"] == 2: return r + 1                                    # 第一次备份“前后问题数不一致”
        return r
    try:
        B._count_problems = flaky
        st = go(src, base, dt.datetime(2026, 11, 3, 3, 30))
        assert st["ok"] and calls["n"] >= 4                                 # 失败后重试并最终成功
        assert not [p for p in (base / "kb").iterdir() if p.name.startswith(".tmp")]
        tick = {"i": 0}
        def always_differs(b):                                              # 每次调用都比上一次多 1：前后永远对不上
            tick["i"] += 1
            return orig(b) + tick["i"]
        B._count_problems = always_differs
        st = go(src, base, dt.datetime(2026, 11, 4, 3, 30))
        assert not st["ok"] and "问题数" in st["error"]
        assert not [p for p in (base / "kb").iterdir() if p.name.startswith(".tmp")] and len([p for p in (base / "kb").iterdir() if B.NAME_RE.match(p.name)]) == 1
    finally:
        B._count_problems = orig; shutil.rmtree(src); shutil.rmtree(base)

def test_corrupt_archive_is_detected():
    src = mk_src(); base = Path(tempfile.mkdtemp())
    try:
        st = go(src, base, dt.datetime(2026, 11, 3, 3, 30)); a = base / "kb" / st["file"]
        raw = bytearray(a.read_bytes()); raw[len(raw) // 2] ^= 0xFF; bad = base / "bad.tar.gz"; bad.write_bytes(bytes(raw))
        try: B._verify(bad, 4); assert False
        except Exception: pass
        trunc = base / "trunc.tar.gz"; trunc.write_bytes(a.read_bytes()[:len(raw) // 2])
        try: B._verify(trunc, 4); assert False
        except Exception: pass
        assert B._verify(a, 4) == 4
    finally:
        shutil.rmtree(src); shutil.rmtree(base)

def test_verify_catches_incomplete_but_valid_archive():
    """压缩包本身完好（能解压），但少了问题 / 没有分类树：必须判失败。"""
    src = mk_src(); base = Path(tempfile.mkdtemp())
    try:
        full = base / "full.tar.gz"
        with tarfile.open(full, "w:gz") as tf: tf.add(src / "data" / "kb", arcname="data/kb")
        assert B._verify(full, 4) == 4
        few = base / "few.tar.gz"                                           # 丢掉一个问题目录
        with tarfile.open(few, "w:gz") as tf:
            tf.add(src / "data" / "kb", arcname="data/kb", filter=lambda t: None if "问题0" in t.name else t)
        try: B._verify(few, 4); assert False
        except RuntimeError as e: assert "不一致" in str(e)
        notree = base / "notree.tar.gz"
        with tarfile.open(notree, "w:gz") as tf: tf.add(src / "web", arcname="web")
        try: B._verify(notree, 0); assert False
        except RuntimeError as e: assert "分类树" in str(e)
    finally:
        shutil.rmtree(src); shutil.rmtree(base)

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f(); print("ok", n)
