#!/usr/bin/env python3
"""知识库每日备份（仅标准库）。

打包：data/kb（含 .git 与未整理的缓冲区）、data/plugins/kb_digest、web。不含 AstrBot 配置和任何密钥。
流程：写临时文件 → 校验（能完整读出、含分类树、问题数与现场一致）→ 改名 → 清理旧备份 → 写状态文件。
任何一步失败：不删除旧备份、保留上次成功的记录、退出码非 0。
"""
import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

NAME_RE = re.compile(r"^kb-(\d{8})-(\d{6})\.tar\.gz$")
INCLUDE = [("data/kb", True), ("data/plugins/kb_digest", False), ("web", False)]


def _count_problems(base: Path) -> int:
    return sum(1 for _ in (base / "data" / "kb" / "问题库").rglob("元数据.json")) + \
        sum(1 for _ in (base / "data" / "kb" / "待入库").rglob("元数据.json"))


def _excluded(ti: tarfile.TarInfo):
    return None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _git_head(kb: Path) -> str:
    try:
        r = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(kb), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _verify(archive: Path, expect_problems: int) -> int:
    """完整读一遍压缩包（会校验 gzip CRC），返回包里的问题数；不合格则抛异常。"""
    names = []
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf:                              # 逐个遍历直到流结束 → 触发完整解压校验
            names.append(m.name)
            if m.isfile():
                f = tf.extractfile(m)
                while f.read(1 << 20):
                    pass
    if "data/kb/分类树.json" not in names:
        raise RuntimeError("备份里没有 分类树.json")
    n = sum(1 for x in names if x.endswith("/元数据.json") and ("/问题库/" in x or "/待入库/" in x))
    if n != expect_problems:
        raise RuntimeError(f"备份里的问题数 {n} 与现场 {expect_problems} 不一致（备份期间可能有写入）")
    return n


def prune(dest: Path, keep_daily: int = 14, keep_weekly: int = 8) -> list:
    """保留最新 keep_daily 份 + 其中周日的最新 keep_weekly 份；只处理符合命名规则的文件。返回被删文件名。"""
    files = sorted((p for p in dest.iterdir() if NAME_RE.match(p.name)), key=lambda p: p.name, reverse=True)
    keep = {p.name for p in files[:keep_daily]}
    sundays = [p for p in files if dt.datetime.strptime(NAME_RE.match(p.name).group(1), "%Y%m%d").weekday() == 6]
    keep |= {p.name for p in sundays[:keep_weekly]}
    gone = []
    for p in files:
        if p.name not in keep:
            p.unlink()
            Path(str(p) + ".sha256").unlink(missing_ok=True)
            gone.append(p.name)
    return gone


def _write_json(p: Path, obj: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".st-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)                          # 状态文件不含敏感信息，网页要读它
    os.replace(tmp, p)


def run(src: Path, dest: Path, status_dir: Path, now: dt.datetime | None = None, keep_daily=14, keep_weekly=8,
        retries=3, retry_wait=5) -> dict:
    now = now or dt.datetime.now()
    src, dest, status_dir = Path(src), Path(dest), Path(status_dir)
    sp = status_dir / "LAST.json"
    prev = {}
    try:
        prev = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        pass
    st = {"ok": False, "time": now.strftime("%Y-%m-%d %H:%M:%S"), "epoch": int(now.timestamp()),
          "last_ok_epoch": prev.get("last_ok_epoch") or (prev.get("epoch") if prev.get("ok") else None),
          "last_ok_time": prev.get("last_ok_time") or (prev.get("time") if prev.get("ok") else None)}
    tmp = None
    try:
        if not (src / "data" / "kb" / "分类树.json").exists():
            raise RuntimeError(f"找不到知识库：{src}/data/kb（没有 分类树.json）")
        dest.mkdir(parents=True, exist_ok=True)
        os.chmod(dest, 0o700)
        last_err = None
        for attempt in range(1, retries + 1):
            tmp = Path(tempfile.mkstemp(dir=dest, prefix=".tmp-", suffix=".tar.gz")[1])
            try:
                before = _count_problems(src)
                with tarfile.open(tmp, "w:gz") as tf:
                    for rel, required in INCLUDE:
                        if (src / rel).exists():
                            tf.add(src / rel, arcname=rel, filter=_excluded)
                        elif required:
                            raise RuntimeError(f"缺少必须备份的目录：{rel}")
                after = _count_problems(src)
                if before != after:
                    raise RuntimeError(f"备份期间问题数从 {before} 变成 {after}")
                n = _verify(tmp, after)
                last_err = None
                break
            except Exception as e:
                last_err = e
                tmp.unlink(missing_ok=True)
                tmp = None
                if attempt < retries:
                    time.sleep(retry_wait)
        if last_err:
            raise last_err
        final = dest / f"kb-{now.strftime('%Y%m%d-%H%M%S')}.tar.gz"
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
        tmp = None
        digest = _sha256(final)
        (dest / (final.name + ".sha256")).write_text(f"{digest}  {final.name}\n", encoding="utf-8")
        os.chmod(dest / (final.name + ".sha256"), 0o600)
        removed = prune(dest, keep_daily, keep_weekly)          # 只有新备份成功落盘后才清理旧的
        kept = sorted(p.name for p in dest.iterdir() if NAME_RE.match(p.name))
        st.update(ok=True, file=final.name, size=final.stat().st_size, sha256=digest, problems=n,
                  git_head=_git_head(src / "data" / "kb"), kept=len(kept), removed=removed,
                  last_ok_epoch=st["epoch"], last_ok_time=st["time"], error=None)
    except Exception as e:
        st.update(error=str(e)[:500])
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    _write_json(sp, st)
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="/opt/astrbot")
    ap.add_argument("--dest", default="/opt/astrbot/backup/kb")
    ap.add_argument("--status", default="/opt/astrbot/backup/status")
    a = ap.parse_args()
    st = run(Path(a.src), Path(a.dest), Path(a.status))
    if st["ok"]:
        print(f"备份成功：{st['file']}  {st['size'] / 1024:.0f} KB  问题 {st['problems']} 个  保留 {st['kept']} 份  git {st['git_head']}")
        return 0
    print(f"备份失败：{st['error']}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
