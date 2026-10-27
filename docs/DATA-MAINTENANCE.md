# 备份、升级与恢复

本文供维护 AstrBot 和 Hindsight 服务的人使用。只想查看或导出个人记忆，请看 [使用指南](USAGE.md)。

## 需要保存什么

完整备份包含三部分：

| 数据 | 默认位置 | 用途 |
| --- | --- | --- |
| 插件业务数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge/` | 尚未完成的记录、去重信息、短期语境和配套服务的 `setup.json` |
| 插件控制数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge_control/` | 部署身份、账号与 Bank 归属、代次、模式、维护状态、清空记录和操作凭据 |
| Hindsight 数据与配置 | PostgreSQL 数据库及后端的 `.env` | 已整理的记忆、来源材料、任务历史及服务配置 |

填写过 `control_state_dir` 时，控制数据在指定目录中。默认两个插件目录随 AstrBot 的 `data` 持久保存；无需为了首次安装另加控制目录挂载。

通过向导安装的服务文件位于执行安装命令者的 `~/.local/share/astrbot-memory-服务编号/`。该目录包含 `.env` 和 `compose.yaml`，需要一同备份；在这个目录中维护时，数据库服务名为 `memory-db`。`setup.json` 保存配套服务身份和 Key，应与插件配置一起保存。数据库存放在 `memory_postgres` 数据卷中，模型配置与凭据存放在 `memory_settings` 数据卷的 `models.json` 中，两个卷都需要备份。

**恢复旧业务备份时，保留最新的控制数据。** 例如周一备份、周二清空记忆、周三恢复周一备份，周二的清空记录仍需生效，防止旧记忆重新出现。

## 备份插件数据

插件附带 `scripts/backup.py`，通过 SQLite 备份接口生成业务快照和清单，同时可单独保存最新控制库副本。

以下示例适用于 AstrBot 安装目录为 `/AstrBot`、控制目录使用默认值的 Linux 容器。在 AstrBot 使用的 Python 环境中执行：

```bash
python /AstrBot/data/plugins/astrbot_plugin_hindsight_bridge/scripts/backup.py \
  --data-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge \
  --control-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge_control \
  --output /AstrBot/data/backups/hindsight-business-2026-10-25 \
  --latest-control /AstrBot/data/backups/hindsight-control-latest/control.sqlite3 \
  --backend-version 1.1.0
```

Docker Compose 部署可以在 AstrBot 的 Compose 目录中，给上述命令加上 `docker compose exec -T astrbot` 前缀；`astrbot` 替换成实际服务名。

每次更换 `--output` 的目录名，目标必须是尚不存在的新目录。使用自定义控制目录时，也要替换 `--control-dir`。业务快照和控制副本的输出位置都须位于两个插件数据目录之外。

备份结果包含 `runtime.sqlite3`、`manifest.json` 和单独的控制库副本。将备份复制到服务主机之外保存，同时备份插件配置；恢复业务快照时使用最新控制库，控制副本也要持续更新。

## 备份 Hindsight 数据库

使用仓库部署模板时，在后端 `deploy` 目录执行：

```bash
docker compose exec -T db pg_dump -U hindsight -d hindsight -Fc -f /tmp/hindsight-backup.dump
docker compose cp db:/tmp/hindsight-backup.dump ./hindsight-backup.dump
```

这会把数据库备份复制到主机的 `deploy/hindsight-backup.dump`，同时保存同目录的 `.env`。每次备份后将文件移入按日期区分的备份目录。

使用其他方式安装 Hindsight 时，使用对应 PostgreSQL 部署的备份工具。只备份插件业务库或导出个人 JSON，无法替代完整数据库备份。

插件业务库、控制库与 PostgreSQL 应在短暂停写窗口或一致性快照下取得兼容备份。恢复验证使用独立环境，不对生产 Bank 执行开发测试。

## 官方内容维护

浏览、搜索使用官方 Control Plane，不需要暂停。更正、删除、清空、重处理与导入等写入操作按以下顺序执行：

1. 在 AstrBot 插件管理页选择对应用户和 Bank，开始维护。维护冻结新用户采集、助手回调采集、召回注入、新提交和重试；已被服务接受的任务继续正常排空。个人 `/hindsight pause` 不能替代维护协调。
2. 查看维护状态，等待已接受任务结束，显式作废未提交旧待办。`maintenance_pending` 表示仍有任务或旧待办要处理；不要直接在官方页面修改后重新放行旧队列。
3. 提交响应不明、操作结果不明或协作取消仍不确定时，保持暂停并核对原 operation ID、父子任务和 Bank。单独出现 `cancelled` 不能证明在途批次已经退出。无法确认时，使用完整遗忘恢复，不盲重投或反复取消。
4. 在独立官方 Control Plane 中定位对应 Bank，完成内容操作；插件不额外写入 `uc1`、`ac1`，也不以本地更正覆盖官方结果。
5. 核对 Bank 与用户映射，在插件管理页显式恢复维护（`maintenance_resume`），清理过时的短期语境，再选择开启或只读。`/hindsight on` 只切换个人模式，不能解除维护。

官方删除既有 Bank 后，插件报告 `bank_missing` 并阻止读写，禁止自动重建。恢复方式为二选一：

- 保留旧记忆：从匹配备份恢复原 Bank、文档与任务，核对映射和操作记录，再显式恢复维护。
- 放弃旧范围：在插件执行完整遗忘，持久化独立控制日志，作废旧队列，登记新 Bank 代次，继续取消与清理旧 Bank。清理完成后从新消息开启记忆，旧 Bank ID 永不复用。

恢复流程保留全局安全暂停。旧 Bank 清理失败、删除凭据不一致或后端操作历史缺失时，先按异常表恢复连接、控制数据或原后端状态，再重载并核对；不直接删除错误队列或控制记录。

## 历史本地更正交接

2026-10-27 的生产与本地业务库只读盘点中，`corrections` 均为 0；这两套数据无需更正转换框架。空表可以在格式升级时移除。

其他部署若存在非空历史更正，启动报告 `legacy_corrections_require_handover`，原始数据保留。先备份业务库、最新控制库与 Hindsight 数据，逐条核对更正原文、目标事实、关联文档、待办和远端派生数据，再按实际数据安排官方交接。交接应保留来源与任务身份，并防止旧冻结请求覆盖官方内容；数据是否需要保留须由维护者依据真实使用情况确定，不能把删表当作交接。

首发不引入通用迁移框架。业务与控制数据归属、冻结请求摘要和较新的删除凭据仍须连续。

## 升级插件

1. 创建插件业务快照，单独保存最新控制库，并备份 Hindsight 数据库和配置。
2. 在 AstrBot 的插件管理中更新 Hindsight Memory，重载插件。
3. 原有 API 配置、服务标识、Agent 标识和账号配置继续沿用，数据目录继续挂载。

格式升级保留既有身份、记忆空间、摘要与 operation UUID；非空历史更正会阻止启动并要求先完成数据交接。具体规则见 [实现说明](ARCHITECTURE.md#数据格式与恢复)。

升级后遇到恢复提示，按下文处理；运行状态可通过 `/hindsight status` 查看。

## 恢复业务数据

1. 停用插件或停止 AstrBot。
2. 保存运行目录中的最新控制数据，核对快照 `manifest.json` 中的部署 ID 和控制库 ID 与目标部署一致。
3. 需要恢复 Hindsight 时，先按下一节恢复数据库；只修复插件业务库时沿用后端数据库。
4. 用选定快照恢复 `runtime.sqlite3`。使用 SQLite 备份接口，或在进程完全停止后复制快照并移走目标目录的旧 `runtime.sqlite3-wal`、`runtime.sqlite3-shm`，避免混入旧日志。
5. 沿用最新控制目录，以及有效的 `control_state_dir`、`agent_id`、`service_id` 和账号配置，启动插件。
6. 插件自动检查数据归属、应用清空记录、清理失效任务并核对后端操作。通过恢复检查后，核对 Bank 和维护状态；存在维护或 Bank 缺失时，先恢复原 Bank 并显式恢复维护，或完整遗忘登记新 Bank。个人暂停的账号再用 `/hindsight on` 开启。

删除尚未完成时，插件会继续清理后端旧记忆。恢复检查期间自动记录和记忆查询暂时关闭，进度从 `/hindsight status` 查看。

## 恢复 Hindsight 数据库

使用仓库部署模板时，在 `deploy` 目录放入选定的 `hindsight-backup.dump`，停止 AstrBot 插件，并停止后端服务、保留数据库运行：

```bash
docker compose stop hindsight
docker compose cp ./hindsight-backup.dump db:/tmp/hindsight-restore.dump
docker compose exec -T db pg_restore -U hindsight -d hindsight --clean --if-exists --exit-on-error /tmp/hindsight-restore.dump
docker compose up -d hindsight
```

这会将数据库恢复到所选备份。数据库账号或名称改过时，替换命令中的对应值；恢复后按上一节恢复或沿用匹配的插件数据。

## 异常提示

| 提示 | 处理方式 |
| --- | --- |
| `recovery_required:control_missing` | 找回与业务数据匹配的最新控制目录，确认挂载正确后重载 |
| `control_mismatch` 或 `deployment_mismatch` | 核对是否挂载了另一套部署的数据；比较部署 ID 与控制库 ID |
| `control_rolled_back` | 使用更新的控制库副本，保留较新的清空与身份记录 |
| `account_mapping_missing` | 恢复完整控制库，找回原有账号归属 |
| `accepted_operation_missing` | 核对后端数据库恢复点和操作历史，保留 `HINDSIGHT_API_OPERATION_RETENTION_DAYS=0` |
| `payload_digest_conflict` | 检查插件业务库完整性和备份来源 |
| `bank_missing` | 既有 Bank 消失，保持暂停；恢复原 Bank 后显式恢复维护，或完整遗忘登记新 Bank，不能自动重建 |
| `maintenance_active` | 维护冻结了采集、助手回调、召回注入、提交和重试；管理员查看维护状态，完成内容操作后显式恢复 |
| `maintenance_pending` | 等待已接受任务正常排空，显式作废未提交旧待办，再进行官方内容修改或恢复 |
| `maintenance_unknown_operation` | 提交响应或协作取消结果不明；保持暂停并核对原操作，无法确认时用完整遗忘的控制日志、新 Bank 代次和持续清理恢复 |
| `legacy_corrections_require_handover` | 历史本地更正非空，启动被阻止；保留原库，备份并完成实际数据交接后再重载 |
| “远端清理中” | 恢复后端连接和正确 API Key，插件会继续删除 |

`accepted_operation_missing` 等信息不足的故障不能只靠重试消除。保留冻结请求、operation ID 与最新控制日志，找回兼容的后端恢复点；无法确认旧任务时保持暂停，按完整遗忘流程更换 Bank 代次。管理地址与 Bank 可在插件管理页复制，CP/API Key 仅由管理员和服务端保管，不放入普通用户诊断。

## 自定义控制目录与搬迁

需要让业务与控制数据由不同备份任务管理时，可为 AstrBot 增加独立挂载：

```yaml
services:
  astrbot:
    volumes:
      - ./astrbot-data:/AstrBot/data
      - ./hindsight-control:/AstrBot/hindsight-control
```

插件填写 `control_state_dir=/AstrBot/hindsight-control`。两个主机目录都须允许 AstrBot 读写。已有控制数据时，停止插件后将原控制目录完整迁到新位置，再修改配置和挂载。

搬迁同一服务时沿用 `service_id`；API 地址发生变化，更新 `api_url` 并递增 `service_address_revision`。更换成另一套服务时使用新的服务标识。账号转移或合并历史记忆空间需要单独的数据迁移，不能靠修改 Agent 名称完成。
