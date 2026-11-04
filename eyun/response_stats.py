#!/usr/bin/env python3
"""客户消息 -> 员工首次响应时长统计。仅用标准库，只读数据库。

口径（可在命令行调整）：
  - staff.json 里的 wxid、以及机器人账号自己(is_self=1)发的消息，算「员工」；其余都算「客户」。
  - 客户连发多条只算一次提问，从**第一条**未被回复的客户消息起计时。
  - 员工在该群发出的**任意**消息（文本/图片/引用）视为已响应。同一时刻多个客户同时提问时，
    无法判断员工在回谁，统一按时间顺序近似，这是群聊统计固有的局限。
  - --work 09:00-18:00 时，只累计工作时间内的秒数（夜间、周末不算，周末默认 --weekdays-only）。

用法：
  python3 -I eyun/response_stats.py --db eyun.db --staff eyun/staff.json --days 7
  python3 -I eyun/response_stats.py --db eyun.db --staff eyun/staff.json --pending 15   # 超过 15 分钟没人回的
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta


def load_staff(path):
    if not path:
        return set()
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    return set(d.keys() if isinstance(d, dict) else d)


def parse_work(s):
    if not s:
        return None
    a, b = s.split("-")
    f = lambda x: (int(x.split(":")[0]), int(x.split(":")[1]))
    return f(a), f(b)


def work_seconds(t0, t1, work, weekdays_only):
    """t0..t1（秒时间戳）里落在工作时段的秒数；不限工作时间则直接相减。"""
    if t1 <= t0:
        return 0
    if not work:
        return t1 - t0
    (sh, sm), (eh, em) = work
    total, day = 0, datetime.fromtimestamp(t0).replace(hour=0, minute=0, second=0, microsecond=0)
    while day.timestamp() < t1:
        if not (weekdays_only and day.weekday() >= 5):
            a = day.replace(hour=sh, minute=sm).timestamp()
            b = day.replace(hour=eh, minute=em).timestamp()
            total += max(0, min(b, t1) - max(a, t0))
        day += timedelta(days=1)
    return int(total)


def pairs(rows, staff):
    """rows: (group_id, sender, ts, is_self) 按群、时间排序。
    返回 (answered, pending)：answered=[(group, asked_ts, answered_ts, customer, staff)]，pending=[(group, asked_ts, customer)]。"""
    answered, pending = [], {}
    for g, sender, ts, is_self in rows:
        is_staff = bool(is_self) or sender in staff
        if not is_staff:
            pending.setdefault(g, (ts, sender))  # 已有未回复提问就不覆盖，保持起点
        elif g in pending:
            t0, cust = pending.pop(g)
            answered.append((g, t0, ts, cust, sender))
    return answered, [(g, t0, c) for g, (t0, c) in pending.items()]


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0
    return sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))]


def fmt(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--staff", help="员工 wxid 的 json（列表，或 {wxid: 姓名}）")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--work", help="工作时间，如 09:00-18:00；不填则按自然时间")
    ap.add_argument("--weekdays-only", action="store_true", help="配合 --work，周末不计时")
    ap.add_argument("--pending", type=int, metavar="MIN", help="只列出超过 MIN 分钟还没人回的提问")
    a = ap.parse_args(argv)

    staff, work = load_staff(a.staff), parse_work(a.work)
    now = int(time.time())
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    rows = db.execute("SELECT group_id,sender,ts,is_self FROM messages WHERE chat_type='group' AND ts>=? ORDER BY group_id,ts",
                      (now - a.days * 86400,)).fetchall()
    answered, pending = pairs(rows, staff)

    if a.pending is not None:
        late = [(g, t0, c) for g, t0, c in pending
                if work_seconds(t0, now, work, a.weekdays_only) >= a.pending * 60]
        for g, t0, c in sorted(late, key=lambda x: x[1]):
            print(f"{g}\t客户 {c}\t{datetime.fromtimestamp(t0):%m-%d %H:%M} 起已 {fmt(work_seconds(t0, now, work, a.weekdays_only))} 无人回复")
        print(f"共 {len(late)} 条超时未回复", file=sys.stderr)
        return 0

    if not staff:
        print("提示：未提供 --staff，只有机器人账号自己发的消息才算员工回复", file=sys.stderr)
    durs = sorted(work_seconds(t0, t1, work, a.weekdays_only) for _, t0, t1, _, _ in answered)
    print(f"近 {a.days} 天：提问 {len(answered) + len(pending)} 次，已响应 {len(answered)}，未响应 {len(pending)}")
    if durs:
        print(f"响应时长  平均 {fmt(sum(durs) / len(durs))}  中位 {fmt(pct(durs, .5))}  P90 {fmt(pct(durs, .9))}  最长 {fmt(durs[-1])}")
    by_staff = {}
    for _, t0, t1, _, s in answered:
        by_staff.setdefault(s, []).append(work_seconds(t0, t1, work, a.weekdays_only))
    for s, v in sorted(by_staff.items(), key=lambda x: -len(x[1])):
        print(f"  {s}\t回复 {len(v)} 次\t平均 {fmt(sum(v) / len(v))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
