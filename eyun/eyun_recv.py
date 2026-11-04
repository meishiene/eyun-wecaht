#!/usr/bin/env python3
"""Eyun 回调接收服务。仅用标准库。

只做一件事：收到推送 -> 验证路径密钥 -> 去重落盘(sqlite) -> 立刻返回 200。
统计、整理都在别处异步做，避免超过 Eyun 的 6 秒超时。

环境变量：
  EYUN_DB           sqlite 路径（默认 ./eyun.db）
  EYUN_PATH_SECRET  回调地址里的随机段，回调地址形如 https://域名/webhooks/eyun/<SECRET>（必填）
  EYUN_SIGN_SECRET  控制台生成的签名密钥（可选，设置后校验 X-Eyun-Signature，见 verify_sign）
  EYUN_BIND/EYUN_PORT  监听地址与端口（默认 127.0.0.1:6190，由隧道指过来）
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_BODY = 5 * 1024 * 1024  # 图片回调带 Base64 缩略图，留足余量
GROUP_TYPES = {"80001": "text", "80002": "image", "80014": "quote"}
# 60008 是「对方还在上传、暂不可下载」，等 60009 完成再入库，所以不收
PRIVATE_TYPES = {"60001": "text", "60002": "image", "60014": "quote", "60009": "file"}
TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.S)

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  uid TEXT PRIMARY KEY,          -- wcId:newMsgId，去重键
  group_id TEXT NOT NULL,        -- 群消息是群 ID；私聊是对方 wxid（会话 ID，列名沿用旧版）
  sender TEXT NOT NULL,
  ts INTEGER NOT NULL,           -- 微信消息时间，秒
  kind TEXT NOT NULL,            -- text / image / quote / file
  text TEXT NOT NULL,            -- 图片为 [图片]，文件为文件名，引用为用户写的那句
  is_self INTEGER NOT NULL,      -- 机器人账号自己发的
  at_list TEXT NOT NULL,         -- JSON 数组
  recv_at INTEGER NOT NULL       -- 本服务收到的时间，用来观察推送延迟
);
CREATE TABLE IF NOT EXISTS raw_events(  -- 未识别的事件（群信息变更、好友事件等），只留最近一批排查用
  id INTEGER PRIMARY KEY AUTOINCREMENT, recv_at INTEGER, event TEXT, body TEXT
);
"""


# 旧库升级用：列名 -> 建列语句。以后加列只在这里追加。
MIGRATIONS = {
    "chat_type": "ALTER TABLE messages ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'group'",  # group / private
    "msg_id": "ALTER TABLE messages ADD COLUMN msg_id TEXT NOT NULL DEFAULT ''",             # 下载图片/文件要用
    "content_xml": "ALTER TABLE messages ADD COLUMN content_xml TEXT NOT NULL DEFAULT ''",   # 图片/文件/引用的原始 XML
}


def connect(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.executescript(SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    for col, ddl in MIGRATIONS.items():
        if col not in have:
            db.execute(ddl)
    db.execute("CREATE INDEX IF NOT EXISTS ix_msg_chat_ts ON messages(chat_type, group_id, ts)")
    db.commit()
    return db


def verify_sign(secret, body, header):
    """签名算法文档未给出，这里按常见的 HMAC-SHA256(hex) 比较，允许带 sha256= 前缀。
    上线前用控制台「保存并测试」的真实请求核对一次，不对就改这里。"""
    if not secret:
        return True
    got = (header or "").removeprefix("sha256=")
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(got, want)


def extract_text(kind, data):
    """文本直接取 content；引用取 title（用户自己写的那句）；文件取文件名；图片用占位符。"""
    if kind == "text":
        return str(data.get("content", ""))
    if kind == "quote":
        return str(data.get("title") or "[引用]")
    if kind == "file":
        m = TITLE_RE.search(str(data.get("content", "")))
        return m.group(1).strip() if m and m.group(1).strip() else "[文件]"
    return "[图片]"


def parse_message(payload):
    """返回入库用的 dict；既不是群消息也不是私聊消息、或字段不全返回 None。"""
    mt = str(payload.get("messageType", ""))
    data = payload.get("data") or {}
    if not data.get("newMsgId"):
        return None
    if mt in GROUP_TYPES and data.get("fromGroup"):
        chat_type, kind, chat_id = "group", GROUP_TYPES[mt], str(data["fromGroup"])
    elif mt in PRIVATE_TYPES and data.get("fromUser"):
        chat_type, kind = "private", PRIVATE_TYPES[mt]
        # 会话 ID 取「对方」：别人发来的是 fromUser，机器人自己发出去的是 toUser
        chat_id = str(data.get("toUser") if data.get("self") else data["fromUser"])
    else:
        return None
    try:
        ts = int(data["timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    wc = str(payload.get("wcId") or data.get("toUser") or "")
    return {
        "uid": f"{wc}:{data['newMsgId']}",
        "chat_type": chat_type,
        "group_id": chat_id,
        "sender": str(data.get("fromUser", "")),
        "ts": ts,
        "kind": kind,
        "text": extract_text(kind, data),
        "is_self": 1 if data.get("self") else 0,
        "at_list": json.dumps(data.get("atlist") or [], ensure_ascii=False),
        "msg_id": str(data.get("msgId", "")),
        "content_xml": "" if kind == "text" else str(data.get("content", ""))[:20000],
    }


def store(db, lock, body_bytes, event, now):
    """返回 'saved' / 'dup' / 'raw'。解析失败也返回 raw 并留底，保证永远能回 200。"""
    try:
        # newMsgId 超出 JS 精确整数范围，全部整数按字符串读，避免精度问题
        payload = json.loads(body_bytes, parse_int=str)
    except ValueError:
        payload = None
    msg = parse_message(payload) if isinstance(payload, dict) else None
    with lock:
        if msg is None:
            db.execute("INSERT INTO raw_events(recv_at,event,body) VALUES(?,?,?)",
                       (now, event, body_bytes[:20000].decode("utf-8", "replace")))
            db.execute("DELETE FROM raw_events WHERE id <= (SELECT MAX(id) FROM raw_events) - 500")
            db.commit()
            return "raw"
        cur = db.execute(
            "INSERT OR IGNORE INTO messages(uid,chat_type,group_id,sender,ts,kind,text,is_self,at_list,recv_at,msg_id,content_xml)"
            " VALUES(:uid,:chat_type,:group_id,:sender,:ts,:kind,:text,:is_self,:at_list,:recv_at,:msg_id,:content_xml)",
            {**msg, "recv_at": now})
        db.commit()
        return "saved" if cur.rowcount else "dup"


def make_server(db_path, path_secret, sign_secret, host, port):
    if not path_secret:
        raise SystemExit("EYUN_PATH_SECRET 必须设置")
    db, lock = connect(db_path), threading.Lock()
    route = f"/webhooks/eyun/{path_secret}"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 日志里不能出现带密钥的路径
            pass

        def _reply(self, code, text="ok"):
            b = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._reply(200 if self.path == "/healthz" else 404, "ok" if self.path == "/healthz" else "not found")

        def do_POST(self):
            if not hmac.compare_digest(self.path.split("?")[0], route):
                return self._reply(404, "not found")
            try:
                n = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                return self._reply(413, "too large")
            body = self.rfile.read(n)
            if not verify_sign(sign_secret, body, self.headers.get("X-Eyun-Signature")):
                return self._reply(401, "bad signature")
            store(db, lock, body, self.headers.get("X-Eyun-Event", ""), int(time.time()))
            self._reply(200)  # 先落盘再回 2xx，其余一律异步

    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    srv.db = db
    return srv


if __name__ == "__main__":
    s = make_server(os.environ.get("EYUN_DB", "eyun.db"), os.environ.get("EYUN_PATH_SECRET", ""),
                    os.environ.get("EYUN_SIGN_SECRET", ""), os.environ.get("EYUN_BIND", "127.0.0.1"),
                    int(os.environ.get("EYUN_PORT", "6190")))
    print(f"listening on {s.server_address[0]}:{s.server_address[1]}", flush=True)
    s.serve_forever()
