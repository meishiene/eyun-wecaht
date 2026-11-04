# AstrBot 知识库机器人

> 这是本仓库的 **AstrBot 部分**，目前**已上线运行**。整体分工见仓库根目录 [`README.md`](../README.md) 和 [`docs/功能归属与交接.md`](../docs/功能归属与交接.md)。

AstrBot（官方"个人微信"适配器，用 ClawBot 通道）+ 插件 `kb_digest` + 只读网页 `kb_web`。
把群里的问答粘贴给机器人，大模型整理成"大类 → 细类 → 问题（简要 / 报告 / 原文）"的 Markdown 知识库。

## 仓库里没有的东西（交接时要单独拿）
- **知识库数据**（`data/kb`，含全部问题、原文、`.git` 历史）只在服务器上，**不在本仓库**。原文逐字保存、不脱敏，里面有客户聊天内容。
- **种子数据**：`seed/` 里的原始案例文档和由它派生的文件（`source.md`、`cases.json`、`draft_raw.json`、`groups.json`、归类日志、清单、手册原文）是内部客服资料，仓库是公开的，所以**没有上传**。因此 `build.py`、`classify.py` 在仓库里不能直接运行，需要向负责人要这批文件放进 `seed/`。`seed/stage/`（生成出来的暂存知识库）同理。
- **密钥与配置**：模型 Key、AstrBot 配置、代理节点、网页访问密码都不在仓库里。
- 没有种子数据时，`tests/test_kb_web.py` 里的「真实数据全页面渲染」会自动跳过，其余测试不受影响。

## 组成
| 路径 | 作用 |
|---|---|
| `plugins/kb_digest/` | AstrBot 插件：收集、整理、入库、检索、维护（核心逻辑在 `kb_core.py`，不依赖 AstrBot） |
| `web/kb_web.py`、`web/kb-web.service` | 局域网只读网页（仅标准库）+ 加固的 systemd 服务 |
| `backup/kb_backup.py`、`kb-backup.service/.timer` | 每日备份（systemd 定时器）：打包、校验、轮换、写状态 |
| `seed/` | 一次性把《问答案例文档》导成种子知识库的脚本（解析 → 大模型归类 → 去重 → 清单，**原始数据不在仓库里，见上一节**）；`backfill.py` 给存量问题补新字段；`分类边界.md` 是接在排查手册后面的分类边界与答复规范 |
| `tests/` | 在 `astrbot/` 目录下运行 `python3 -I tests/test_kb_core.py`、`test_plugin_flow.py`、`test_kb_web.py`、`test_kb_backup.py`、`test_tags.py`、`test_nl.py`、`test_backfill.py` |
| `docker-compose.yml` | AstrBot 容器 |

## 服务器上的位置（内网服务器，地址见内部记录）
- 知识库（git 管理）：`/opt/astrbot/data/kb`　分类树：`分类树.json`　手册：`手册/`
- 插件：`/opt/astrbot/data/plugins/kb_digest`　网页：`/opt/astrbot/web/kb_web.py`
- 大模型代理：`astrbot-mihomo.service`（只让 `mux.la` 走代理）
- 网页：`kb-web.service`，监听内网地址的 `6186` 端口，访问密码在 `/etc/astrbot-kb-web.env`（仅 root 可读）

## 微信里的指令（命令后必须有空格：`/查 401`，不是 `/查401`）
- 收集：直接粘贴文字、发图片，不回复。`/整理 [标题]` 把这一批整理成**一个**待入库问题；`/入库` 归档并 git 记录。
- 查询：`/查 关键词 [#标签]`、`/看 编号`（可只写序号）、`/树`、`/待入库`、`/条数`。
- 维护：`/改类 编号 细类`、`/状态 编号 状态`、`/同意新类 [名称 [大类]]`、`/合并 被并编号 保留编号`、`/清空`、`/帮助`。
- 审核：`/审 编号 分数(1~5) [校正说明]`（只写编号看审核情况）、`/待审`。4 分及以上算可用，3 分及以下需校正；写了校正说明就存成**基准回答**，AI 原稿保留作对照。
- 标签层级：`/标签 树` 看 厂商 → 模型 → 错误码（由问题的标签推出，网页「标签」页也有）。搜索时带空格的标签名可直接写，如 `/查 #Claude 5 #401`。
- 不提供删除已入库问题的指令（防误操作），需要时到服务器上处理。

## 约定
- **新字段**（2026-11-04 起）：每个问题带「原理说明」（给非技术人员的白话）、「归属侧」（请求侧 / 供应侧 / 平台侧 / 待判断）、「类型」（常态 / 非常态 / 待判断）、「相关分类」。非常态问题在 `/看`、`/查`、网页里提示“建议转技术支持”。大模型拿不准一律填“待判断”，不猜。
- **分类边界**：主分类按**根因**定——账号被封/失效/风控归 1.3，请求格式/鉴权/限流等归 4.x；两边都沾的，另一边记在「相关分类」里（不移动文件夹、不改编号，两个分类下都查得到）。规则全文见 `seed/分类边界.md`，部署时放到知识库 `手册/分类边界.md`。
- **AI 补全待审**：存量问题的新字段由 `seed/backfill.py` 补，带 `ai_filled` 标记，`/审` 之后才算人工确认。
- 原文逐字保存、只追加、不脱敏；简要和报告里对 Key、手机号、邮箱、带 token 的链接脱敏。
- 状态：已解决 / 未解决 / 待跟进 / 需人工核验。口径来源：官方文档 / 后台核验 / 经验判断 / 知识库提炼 / 待验证。
- 编号 `Q日期-序号` 只增不复用，改分类只是移动文件夹。

## 给存量问题补新字段
先备份（`systemctl start kb-backup.service`），再分两步，**先出方案、看过再写入**：
```
SEED_KEY=… python3 -I seed/backfill.py plan  --kb <知识库目录> --out <输出目录> [--limit 5]   # 只读，--limit 均匀抽样试跑
python3 -I seed/backfill.py apply --kb <知识库目录> --plan <输出目录>/backfill_plan.json [--qids Q…]   # 写入 + git 提交
```
只补还没有 `owner_side` 的问题；报告里原有字段（含 AI 原稿答复口径）一个字不改；可重复执行。回滚：`git -C <知识库目录> revert <提交>`，或按下面「备份与还原」。

## 备份与还原
- **每天 03:30（±5 分钟）** 由 `kb-backup.timer` 触发，错过会在开机后补跑。备份在 `/opt/astrbot/backup/kb/`（权限 700）。
- 内容：`data/kb`（含 `.git` 历史和尚未整理的缓冲区）、`plugins/kb_digest`、`web`。**不含**任何配置和密钥（AstrBot 配置、代理节点、模型 Key）。
- 保留：最近 14 份 + 其中周日的最近 8 份。**备份失败时绝不清理旧备份。**
- 每次备份先写临时文件，校验通过（能完整读出、有分类树、问题数与现场一致）才落盘；同目录有 `.sha256`。
- 状态在 `/opt/astrbot/backup/status/LAST.json`，网页首页顶部显示；超过 36 小时没成功会变红。
- 手动跑一次：`systemctl start kb-backup.service`，看结果：`journalctl -u kb-backup.service -n 5`。
- **还原（先还原到临时目录检查，再决定是否替换现场）：**
  ```
  cd /opt/astrbot/backup/kb && sha256sum -c kb-XXXX.tar.gz.sha256      # 必须在这个目录下执行
  mkdir /tmp/restore && tar -xzf kb-XXXX.tar.gz -C /tmp/restore         # 得到 data/kb、data/plugins/kb_digest、web
  # 确认无误后替换现场：先停 AstrBot，把现场 data/kb 改名保留，再放入还原出来的 data/kb
  docker stop astrbot && mv /opt/astrbot/data/kb /opt/astrbot/data/kb.broken && cp -a /tmp/restore/data/kb /opt/astrbot/data/kb && docker start astrbot
  ```
- 局限：备份和现场在**同一块硬盘**上，能防误删、改坏、程序 bug，**防不了硬盘损坏或机器丢失**。要真正防灾，需要把 `/opt/astrbot/backup/kb` 同步到另一台机器或网盘。
