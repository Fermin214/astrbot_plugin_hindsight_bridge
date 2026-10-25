# Hindsight Memory 数据维护

## 存储边界

| 数据 | 位置 | 恢复规则 |
| --- | --- | --- |
| 业务库 | `plugin_data/astrbot_plugin_hindsight_bridge/runtime.sqlite3` | 可恢复历史快照 |
| 控制库 | `control_state_dir/control.sqlite3` | 保留最新版本，独立于业务快照恢复 |
| Hindsight 数据 | 后端 PostgreSQL 与持久卷 | 使用上游备份恢复流程 |

控制库保存逻辑用户和来源账号映射、Bank 分配、代次、模式、采集时间下限、删除日志、操作接收凭据与取消意图，不保存聊天正文。它是身份和遗忘流程的依据。

业务 schema 4 在窗口、队列、去重与更正之外，增加短期 `replies` 关联表：保存标准 `conversation_manager` 持久化 history 中的最终助手纯文本、同一来源账号/代次/会话内已核对用户事件关联、首次观察时间、采集边界及独立任务身份。短期正文按 30 分钟窗口清理，未完成任务保留自己的冻结副本；清理短期关联后仍须保持助手文档及操作的稳定去重。

`history_baselines` 保存前置历史基线，`task_id` 关联用户窗口任务，`digest` 字段保存历史长度与前缀摘要。前置历史读取成功后才建立助手锚点；后续读取的持久历史必须增长、原前缀摘要一致，且新增部分包含对应用户轮次和最终助手正文，才能关联。读取失败不建立助手锚点，用户原文采集独立进行；压缩、编辑或截断改变边界时拒绝误配。

`replies.user_task_id` 与 `history_baselines.task_id` 均通过外键关联 `window(task_id)`，使用 `ON DELETE CASCADE`。移除账号权限删除对应 window，级联清除助手关联和历史基线；恢复权限后从新核对事件与成功读取的前置历史建立语境。

容器中的业务与控制目录都需要持久存储。推荐把 `/AstrBot/data` 与 `/AstrBot/hindsight-control` 分别挂载到两个宿主目录，并配置 `control_state_dir=/AstrBot/hindsight-control`。业务恢复的覆盖范围须排除控制目录。如果使用同一持久卷的不同目录，恢复工具也须明确排除控制目录。`agent_id`、`service_id`、部署 ID 与账号映射属于连续身份，迁移时沿用。

## 创建快照

`backup.py` 使用 SQLite backup API，同时处理 WAL，生成独立的业务快照及 `manifest.json`。清单包含数据格式、Hindsight 版本、部署 ID、控制库 ID、最后见到的控制修订号与快照时间。

在 AstrBot 的 Python 环境中运行，目录按部署调整：

```bash
python /AstrBot/data/plugins/astrbot_plugin_hindsight_bridge/backup.py \
  --data-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge \
  --control-dir /AstrBot/hindsight-control \
  --output /backups/hindsight-business/2026-10-24 \
  --latest-control /backups/hindsight-control-latest/control.sqlite3 \
  --backend-version 1.1.0
```

业务快照目录必须是新目录，并位于业务和控制目录之外。`--latest-control` 可选，目标须位于业务快照之外；它通过临时文件和原子替换维护独立的控制副本。日常业务恢复使用仍在运行目录中保留的最新控制库。

Hindsight PostgreSQL 的备份单独进行，依照后端版本对应的官方流程。插件快照只包含连接层数据。

## 恢复业务数据

1. 停用插件或停止 AstrBot，等待旧 Worker 和 HTTP 连接退出。
2. 保留最新控制目录，检查快照清单的部署 ID、控制库 ID 与目标部署一致。恢复旧配置时保留有效的 `control_state_dir`、`agent_id` 与 `service_id`，包括恢复 P1 备份后补回独立控制路径。
3. 按上游流程恢复 Hindsight 后端数据。
4. 将业务库恢复为选定快照。插件停用后可用 SQLite backup API 将快照复制到 `runtime.sqlite3`；不要把旧 WAL 文件混入恢复结果。
5. 启动插件。恢复检查完成前，采集、自动召回、手动召回和队列提交保持关闭。
6. 插件读取最新控制记录，恢复账号映射、有效 Bank、代次、模式与采集下限，作废旧代次用户及助手队列，清除恢复出的原文窗口、`replies` 和 `history_baselines`，使旧助手候选失效。
7. 插件检查全部历史删除记录，清理恢复出的旧 Bank、任务与文档；随后核对有效范围的已接受任务。操作记录缺失时转 `reconcile_required`。
8. 检查 `/hindsight status`。恢复流程沿用控制库中的模式；暂停范围由本人发送 `/hindsight on` 开启。

例如，周一创建快照、周二清空、周三恢复周一业务库：周二控制记录仍然有效，所以周一的旧 Bank 和队列被清理。周二清空前发生、恢复后才重投的陌生事件也受采集时间下限约束。

恢复后的助手采集只能关联有效边界内新核对的用户事件。旧 history 即使重新读取，也不能建立被遗忘或已切断的关联；新的 `history_observed_at` 不能让边界前用户事件的迟到助手进入新窗口。

## 异常与恢复条件

| 状态 | 处理 |
| --- | --- |
| `recovery_required:control_missing` | 恢复与业务库匹配的最新控制目录，再重载插件 |
| `recovery_required:control_mismatch` / `deployment_mismatch` | 核对挂载路径、部署 ID 与控制库 ID |
| `recovery_required:control_rolled_back` | 使用更新的控制库副本；旧副本不能替代更新的删除和身份记录 |
| `recovery_required:account_mapping_missing` | 恢复完整的控制库，避免重新分配逻辑用户 |
| `accepted_operation_missing` | 操作已被服务接受，但记录不存在；核对后端恢复点及操作保留配置 |
| `payload_digest_conflict` | 检查业务库完整性，保留原操作身份，不用原 UUID 提交修改后的正文 |
| `远端清理中` | 检查连接与后端认证；控制日志会驱动后续重试 |

清空删除记忆副本和异步任务材料。AstrBot 的聊天记录由 AstrBot 管理。只读、暂停、清空和移除账号权限均切断双方窗口及未冻结助手关联，清除相应 `replies` 与 `history_baselines`；恢复采集或重新加入账号后，从新核对用户事件及成功读取的前置历史建立语境。迟到助手不能跨边界回填，旧 history 不回灌已遗忘原文。

## 格式升级与数据连续性

业务格式 2 升级到 3 时增加 `corrections` 表，沿用既有部署、Bank、身份映射、事件摘要和操作 UUID。新更正采用 `user_correction_v1` 材料，独立于既有 `user_window_v1` 文档；不修改已经冻结的请求。

schema 3→4 新增短期 `replies` 和 `history_baselines` 表，继承既有部署与逻辑身份、Bank 分配、代次、去重和 operation UUID，不重建业务库。用户文档沿用 `uw1`，旧冻结请求的正文、格式、摘要及操作身份不变；缺少前置历史基线的旧任务不从后来历史补造助手锚点。新建双方材料采用 `dialogue_window_v2`，历史限同账号、代次和会话最近 30 分钟内最多 4 个已核对用户轮次及对应助手，双方历史正文合计最多 4,000 字符，超限按最旧完整轮次移除。

每条最终助手正文保存后独立创建 `aw1` 文档和 operation，不等待用户再次发消息。冻结材料标明 `role=assistant`、`delivery=unknown`、`time_source=history_observed_at`，首次观察时间在重试时不变；发送回执只是可选附加信息，不声称 QQ 送达。用户标准接收入站核对规则保持不变，助手关联不能通过未核对的 history 用户文本补建。

采集排除 system/tool/reasoning、工具调用中间消息、临时召回块、压缩摘要及未保存草稿。建议不能自动归为用户接受，承诺和助手自称操作完成均按助手陈述保存，实际完成需独立来源佐证。

显式更正保存在业务库中，包括目标记忆 ID、源文档、旧陈述、新陈述和发生时间。任务完成后清除队列正文，更正索引用于后续召回和个人导出。完整遗忘同步清除旧代次更正；恢复早于遗忘的业务快照时，依据独立控制记录再次清除。

结构调整前创建业务快照，单独保存最新控制库。Bank 归属、逻辑用户、删除记录、代次与在途冻结任务共同保持连续性。隔离 Bank 和副本用于故障及恢复实验。

精确账号配置的格式版本为 2。已有开发配置的导入支持单实例或单用户旧列表的明确转换；多个实例配多个用户的旧配置需要填写实际组合。平台列表为空时只导入业务库已经识别的实例—用户关系。

跨平台身份层保存 `principal_id` 和 Bank 归属。新增账号绑定到既有逻辑用户需要先完成账号验证；已有账号转移以及历史 Bank 合并需要单独的数据迁移流程。

公开版本的兼容与迁移说明从 v0.1.0 起随发布维护，安装说明列明支持的数据格式。独立控制库不会被业务历史快照覆盖。

## 恢复与迁移的模型验证

恢复副本、迁移检查和模型效果实验使用独立 Hindsight 后端。分别配置聊天、抽取、整合、向量与重排服务，使用隔离账号和 Bank；恢复出的正式配置不能直接用于模型验证。验证结果及用量取自实际执行记录。
