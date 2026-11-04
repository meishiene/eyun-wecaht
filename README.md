# eyun-wecaht

用 Eyun（e云管家）个人微信 API 接收群消息和私聊消息，落到 SQLite，并统计员工对客户消息的**首次响应时长**。只用 Python 标准库，没有第三方依赖。

> **状态：已放弃接入生产（2026-11-03）。** 代码和测试完整，保留作参考。原因见文末。

## 已完成的功能

### 1. 回调接收服务 `eyun/eyun_recv.py`
收到 Eyun 的推送后，只做一件事：验证路径密钥 → 去重落盘 → 立刻返回 200（Eyun 回调 6 秒超时，统计和整理都在别处异步做）。

- **消息类型：** 群消息（文本 80001、图片 80002、引用 80014）和私聊消息（文本 60001、图片 60002、引用 60014、文件 60009）。60008（对方仍在上传）不收，等 60009。
- **去重：** 用 `wcId:newMsgId` 作为主键，重复推送只存一条。`newMsgId` 超出 JS 精确整数范围，所有整数按字符串读取，避免精度丢失。
- **私聊会话 ID：** 取「对方」。别人发来的用 `fromUser`，机器人自己发出去的用 `toUser`。
- **永远回 2xx：** 解析失败的数据进 `raw_events` 留底（只保留最近 500 条），不让 Eyun 反复重试。
- **访问控制：** 回调路径含随机密钥 `/webhooks/eyun/<EYUN_PATH_SECRET>`（常数时间比较），可选再校验 `X-Eyun-Signature`；请求体上限 5 MB；日志不输出路径，不泄露密钥。
- **旧库升级：** `MIGRATIONS` 自动给旧库补列，并建索引。
- **健康检查：** `GET /healthz`。

### 2. 响应时长统计 `eyun/response_stats.py`
只读数据库，输出近 N 天的提问数、已响应 / 未响应、平均 / 中位 / P90 / 最长响应时长，以及每个员工的回复次数和平均时长。

- **口径：** `staff.json` 里的 wxid 和机器人自己发的消息算「员工」，其余算「客户」。客户连发多条只算一次提问，从第一条未被回复的消息起计时；员工在该群发任意消息即视为已响应。
- **工作时间：** `--work 09:00-18:00` 只累计工作时段，`--weekdays-only` 周末不计。
- **超时清单：** `--pending 15` 只列出超过 15 分钟没人回的提问。
- **只统计群消息，** 私聊不计入。

### 3. 部署文件
- `eyun/eyun-recv.service`：加固的 systemd 服务（`DynamicUser`、`ProtectSystem=strict`、`MemoryMax=256M`、`NoNewPrivileges` 等），密钥从 `EnvironmentFile` 读取。
- `eyun/staff.example.json`：员工名单示例。真实的 `staff.json` 已在 `.gitignore` 里。

### 4. 测试 `tests/test_eyun.py`
11 个测试，全部通过：

```
python3 -I tests/test_eyun.py
```

覆盖：接收与去重、大整数 ID、错误路径和坏数据、各消息类型、签名校验、提问与响应配对、工作时间计算、命令行输出、私聊类型、旧库迁移、统计忽略私聊。

## 用法

```
# 启动接收服务
EYUN_PATH_SECRET=$(python3 -c "import secrets;print(secrets.token_urlsafe(32))") \
EYUN_DB=./eyun.db python3 -I eyun/eyun_recv.py
# 回调地址：https://<你的域名>/webhooks/eyun/<EYUN_PATH_SECRET>

# 统计
cp eyun/staff.example.json eyun/staff.json   # 改成真实员工 wxid
python3 -I eyun/response_stats.py --db eyun.db --staff eyun/staff.json --days 7 --work 09:00-18:00 --weekdays-only
python3 -I eyun/response_stats.py --db eyun.db --staff eyun/staff.json --pending 15
```

| 环境变量 | 说明 |
|---|---|
| `EYUN_PATH_SECRET` | 必填。回调路径里的随机段 |
| `EYUN_SIGN_SECRET` | 可选。设置后校验 `X-Eyun-Signature` |
| `EYUN_DB` | SQLite 路径，默认 `./eyun.db` |
| `EYUN_BIND` / `EYUN_PORT` | 监听地址与端口，默认 `127.0.0.1:6190`，由隧道转发进来 |

## 未验证 / 已知局限

- **签名算法没有对照真实请求验证过。** Eyun 文档没给出算法，`verify_sign` 按常见的 HMAC-SHA256（hex，可带 `sha256=` 前缀）实现。启用前请用控制台「保存并测试」的真实请求核对一次。
- **多个客户同时提问时**，无法判断员工在回谁，统一按时间顺序近似，这是群聊统计固有的局限。
- **没有长期运行的数据。** 只在测试期间收过一批群消息和私聊数据。

## 为什么放弃

评估后结论是不用 Eyun 来收群消息：

1. **非官方协议，有封号风险。**
2. **稳定性不够。** 小号的实例当天就因「重连失败」掉线，掉线期间的消息收不到，统计会漏数据。

后续继续使用 AstrBot 官方「个人微信」适配器（目前只覆盖私聊）。
