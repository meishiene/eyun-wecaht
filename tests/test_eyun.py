import hashlib, hmac, json, sqlite3, sys, tempfile, threading, urllib.error, urllib.request
from datetime import datetime
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "eyun"))
import eyun_recv as R, response_stats as S

SECRET = "s3cret-path"
G = "123@chatroom"


def payload(mt="80001", new_id="1497474118261484795", ts=1000, sender="wxid_cust", content="你好", **extra):
    data = {"content": content, "fromGroup": G, "fromUser": sender, "msgId": 1, "newMsgId": int(new_id),
            "self": False, "timestamp": ts, "toUser": "wxid_bot", **extra}
    return json.dumps({"messageType": mt, "wcId": "wxid_bot", "data": data}).encode()


def serve(sign=""):
    d = tempfile.mkdtemp()
    s = R.make_server(f"{d}/t.db", SECRET, sign, "127.0.0.1", 0)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, f"http://127.0.0.1:{s.server_address[1]}"


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=body, method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status
    except urllib.error.HTTPError as e: return e.code


def count(s, sql="SELECT COUNT(*) FROM messages"):
    return s.db.execute(sql).fetchone()[0]


def test_receive_dedup_and_bigint():
    s, u = serve()
    assert post(f"{u}/webhooks/eyun/{SECRET}", payload()) == 200
    assert post(f"{u}/webhooks/eyun/{SECRET}", payload()) == 200          # 重复推送
    assert count(s) == 1
    # 19 位 newMsgId 不能被浮点化丢精度
    assert s.db.execute("SELECT uid FROM messages").fetchone()[0] == "wxid_bot:1497474118261484795"


def test_wrong_path_and_bad_body():
    s, u = serve()
    assert post(f"{u}/webhooks/eyun/wrong", payload()) == 404
    assert count(s) == 0
    assert post(f"{u}/webhooks/eyun/{SECRET}", b"not json") == 200       # 永远回 2xx，坏数据留底
    assert count(s, "SELECT COUNT(*) FROM raw_events") == 1


def test_kinds_and_non_group():
    s, u = serve()
    url = f"{u}/webhooks/eyun/{SECRET}"
    post(url, payload("80002", "2", content="<msg><img/></msg>", img="AAAA"))
    post(url, payload("80014", "3", content="<msg>..</msg>", title="收到，马上看", msgType=49))
    post(url, json.dumps({"messageType": "85001", "data": {"fromGroup": "g@chatroom", "newMsgId": 9, "timestamp": 1}}).encode())
    rows = dict(s.db.execute("SELECT kind,text FROM messages").fetchall())
    assert rows == {"image": "[图片]", "quote": "收到，马上看"}
    assert count(s, "SELECT COUNT(*) FROM raw_events") == 1              # 群信息变更等未识别事件进 raw


def test_signature():
    s, u = serve(sign="k")
    body = payload()
    good = hmac.new(b"k", body, hashlib.sha256).hexdigest()
    assert post(f"{u}/webhooks/eyun/{SECRET}", body) == 401
    assert post(f"{u}/webhooks/eyun/{SECRET}", body, {"X-Eyun-Signature": "sha256=" + good}) == 200
    assert count(s) == 1


def test_pairs():
    staff = {"wxid_a"}
    rows = [(G, "c1", 100, 0), (G, "c2", 130, 0),            # 连发两条，只算一次提问，从 100 起
            (G, "wxid_a", 400, 0),                            # 员工回复 -> 300s
            (G, "c1", 500, 0), (G, "bot", 560, 1),            # 机器人自己发的也算员工 -> 60s
            (G, "c3", 900, 0)]                                # 未回复
    ans, pend = S.pairs(rows, staff)
    assert [(a[1], a[2]) for a in ans] == [(100, 400), (500, 560)]
    assert pend == [(G, 900, "c3")]


def test_work_seconds():
    t = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").timestamp()
    w = S.parse_work("09:00-18:00")
    # 周五 17:30 -> 下周一 09:30：只算周五 30 分钟 + 周一 30 分钟
    assert S.work_seconds(t("2026-10-30 17:30"), t("2026-11-02 09:30"), w, True) == 3600
    # 同一天夜里发、第二天早上回：只算早上到回复
    assert S.work_seconds(t("2026-11-03 22:00"), t("2026-11-04 09:10"), w, True) == 600
    assert S.work_seconds(100, 400, None, False) == 300


def test_cli(capsys=None):
    d = tempfile.mkdtemp()
    db = R.connect(f"{d}/t.db")
    import time as _t; now = int(_t.time())
    for i, (snd, off, me) in enumerate([("c1", 600, 0), ("wxid_a", 300, 0), ("c2", 100, 0)]):
        db.execute("INSERT INTO messages(uid,group_id,sender,ts,kind,text,is_self,at_list,recv_at) VALUES(?,?,?,?,?,?,?,?,?)", (f"u{i}", G, snd, now - off, "text", "x", me, "[]", now))
    db.commit(); db.close()
    Path(f"{d}/staff.json").write_text('["wxid_a"]')
    assert S.main(["--db", f"{d}/t.db", "--staff", f"{d}/staff.json"]) == 0
    assert S.main(["--db", f"{d}/t.db", "--staff", f"{d}/staff.json", "--pending", "1"]) == 0


def priv(mt="60001", new_id="5", sender="wxid_cust", self_=False, **data):
    d = {"content": "hi", "fromUser": sender, "toUser": "wxid_bot", "msgId": 7, "newMsgId": int(new_id),
         "self": self_, "timestamp": 2000, **data}
    return json.dumps({"messageType": mt, "wcId": "wxid_bot", "data": d}).encode()


def test_private_kinds():
    s, u = serve()
    url = f"{u}/webhooks/eyun/{SECRET}"
    post(url, priv("60001", "1", content="/整理 标题"))
    post(url, priv("60002", "2", content="<msg><img aeskey='a'/></msg>", img="AAAA"))
    post(url, priv("60014", "3", content="<msg><appmsg/></msg>", title="补充一句", msgType=49))
    post(url, priv("60009", "4", content="<msg><appmsg><title><![CDATA[报错日志.txt]]></title><type>6</type></appmsg></msg>", msgType=49))
    post(url, priv("60008", "6", content="<msg><appmsg><title>x.txt</title></appmsg></msg>", msgType=49))   # 上传未完成，不收
    rows = {r[0]: r[1:] for r in s.db.execute("SELECT kind,text,chat_type,group_id,msg_id,content_xml FROM messages")}
    assert rows["text"][:4] == ("/整理 标题", "private", "wxid_cust", "7") and rows["text"][4] == ""    # 文本不存 xml
    assert rows["image"][0] == "[图片]" and "aeskey" in rows["image"][4]
    assert rows["quote"][0] == "补充一句"
    assert rows["file"][0] == "报错日志.txt"
    assert count(s) == 4 and count(s, "SELECT COUNT(*) FROM raw_events") == 1


def test_private_self_uses_peer_as_chat():
    # 机器人自己发出去的消息：会话 ID 是对方（toUser），不是机器人自己
    s, u = serve()
    post(f"{u}/webhooks/eyun/{SECRET}", priv("60001", "9", sender="wxid_bot", self_=True, toUser="wxid_cust"))
    assert s.db.execute("SELECT group_id,sender,is_self FROM messages").fetchone() == ("wxid_cust", "wxid_bot", 1)


def test_migrate_old_db():
    d = tempfile.mkdtemp()
    old = sqlite3.connect(f"{d}/old.db")
    old.executescript("""CREATE TABLE messages(uid TEXT PRIMARY KEY, group_id TEXT NOT NULL, sender TEXT NOT NULL, ts INTEGER NOT NULL,
        kind TEXT NOT NULL, text TEXT NOT NULL, is_self INTEGER NOT NULL, at_list TEXT NOT NULL, recv_at INTEGER NOT NULL);
        CREATE INDEX ix_msg_group_ts ON messages(group_id, ts);
        INSERT INTO messages VALUES('w:1','g@chatroom','c',1,'text','旧消息',0,'[]',1);""")
    old.commit(); old.close()
    db = R.connect(f"{d}/old.db"); R.connect(f"{d}/old.db")          # 连两次，迁移要可重复
    assert db.execute("SELECT chat_type,msg_id,content_xml,text FROM messages").fetchone() == ("group", "", "", "旧消息")


def test_stats_ignore_private():
    d = tempfile.mkdtemp()
    db = R.connect(f"{d}/t.db")
    import time as _t; now = int(_t.time())
    ins = "INSERT INTO messages(uid,chat_type,group_id,sender,ts,kind,text,is_self,at_list,recv_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
    db.execute(ins, ("a", "group", G, "c1", now - 300, "text", "问", 0, "[]", now))
    db.execute(ins, ("b", "private", "wxid_x", "wxid_x", now - 200, "text", "私聊粘贴", 0, "[]", now))   # 私聊客户消息不算提问
    db.execute(ins, ("c", "group", G, "bot", now - 100, "text", "答", 1, "[]", now))
    db.commit(); db.close()
    rows = sqlite3.connect(f"{d}/t.db").execute("SELECT group_id,sender,ts,is_self FROM messages WHERE chat_type='group' ORDER BY group_id,ts").fetchall()
    ans, pend = S.pairs(rows, set())
    assert len(ans) == 1 and not pend
    assert S.main(["--db", f"{d}/t.db"]) == 0


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns:
        f(); print("ok", f.__name__)
    print(f"{len(fns)} passed")
