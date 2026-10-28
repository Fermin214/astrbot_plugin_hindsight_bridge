# 备份、升级与恢复

本文供维护 AstrBot 和 Hindsight 服务的人使用。只想查看或导出个人记忆，请看 [使用指南](USAGE.md)。

## 需要保存什么

完整备份包含三部分：

| 数据 | 默认位置 | 用途 |
| --- | --- | --- |
| 插件业务数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge/` | 尚未完成的记录、去重信息、短期语境和配套服务的 `setup.json` |
| 插件控制数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge_control/` | 部署身份、账号与 Bank 归属、代次、模式、维护状态、清空记录和操作凭据 |
| Hindsight 数据与配置 | PostgreSQL 数据库及后端的 `.env` | 已整理的记忆、来源材料、任务历史及服务配置 |

官方页面中的 Bank 是每个用户的记忆库，插件页用“记忆库编号”标明归属。首次保存聊天时自动建库，无需在官方页面另建。

填写过 `control_state_dir` 时，控制数据在指定目录中。默认两个插件目录随 AstrBot 的 `data` 持久保存；无需为了首次安装另加控制目录挂载。

通过向导安装的服务文件位于执行安装命令者的 `~/.local/share/astrbot-memory-<id>/`，`<id>` 为服务编号；自定义过安装目录时，以安装输出的路径为准。该目录包含 `.env` 和 `compose.yaml`，需要一同备份；在这个目录中维护时，数据库服务名为 `memory-db`。`setup.json` 保存配套服务身份和 Key，应与插件配置一起保存。数据库存放在 `memory_postgres` 数据卷中，模型配置与凭据存放在 `memory_settings` 数据卷的 `models.json` 中，两个卷都需要备份。

**恢复旧业务备份时，保留最新的控制数据。** 例如周一备份、周二清空记忆、周三恢复周一备份，周二的清空记录仍需生效，防止旧记忆重新出现。

## 备份插件数据

插件附带 `scripts/backup.py`，通过 SQLite 备份接口生成业务快照和清单，同时可单独保存最新控制库副本。

以下示例适用于 AstrBot 安装目录为 `/AstrBot`、控制目录使用默认值的 Linux 容器。在 AstrBot 使用的 Python 环境中执行：

```bash
python /AstrBot/data/plugins/astrbot_plugin_hindsight_bridge/scripts/backup.py \
  --data-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge \
  --control-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge_control \
  --output /AstrBot/data/backups/hindsight-business-YYYYMMDD \
  --latest-control /AstrBot/data/backups/hindsight-control-latest/control.sqlite3 \
  --backend-version 1.1.0
```

Docker Compose 部署可以在 AstrBot 的 Compose 目录中，给上述命令加上 `docker compose exec -T astrbot` 前缀；`astrbot` 替换成实际服务名。

将 `YYYYMMDD` 替换为备份日期，每次更换 `--output` 的目录名，目标必须是尚不存在的新目录。使用自定义控制目录时，也要替换 `--control-dir`。业务快照和控制副本的输出位置都须位于两个插件数据目录之外。

备份结果包含 `runtime.sqlite3`、`manifest.json` 和单独的控制库副本。将备份复制到服务主机之外保存，同时备份插件配置；恢复业务快照时使用最新控制库，控制副本也要持续更新。

## 备份 Hindsight 数据库

使用仓库部署模板时，在后端 `deploy` 目录执行：

```bash
docker compose exec -T db pg_dump -U hindsight -d hindsight -Fc -f /tmp/hindsight-backup.dump
docker compose cp db:/tmp/hindsight-backup.dump ./hindsight-backup.dump
```

这会把数据库备份复制到主机的 `deploy/hindsight-backup.dump`，同时保存同目录的 `.env`。每次备份后将文件移入按日期区分的备份目录。

使用其他方式安装 Hindsight 时，使用对应 PostgreSQL 部署的备份工具。只备份插件业务库或导出个人 JSON，无法替代完整数据库备份。

插件业务库、控制库与 PostgreSQL 应在暂停写入期间备份，或使用一致性快照，确保记录能相互对应。恢复备份时先在独立环境确认数据可用。

## 官方内容维护

Hindsight 1.1.0 原生官方管理页面（Control Plane）提供浏览、搜索、更正、删除、导入和导出。服务器面板需要 HTTPS 域名反向代理或 SSH 隧道；浏览器中的 `127.0.0.1` 指管理员自己的电脑。API 地址供 AstrBot 访问服务，“官方管理地址”供浏览器访问，两者分别配置。安装路径、隧道和获取 `HINDSIGHT_CP_ACCESS_KEY` 的命令见 [部署说明](../deploy/README.md#五打开官方-control-plane)；已有服务的网页登录密钥由维护者提供。

浏览、搜索无需维护。在官方页面更正、删除、清空、重处理、导入等修改操作按以下顺序执行：

1. 在 **运行管理 → 用户记忆** 选择用户，核对记忆库编号。想保留未发送记录，应先选择“正常记忆”，等它们保存完成；失败记录先处理故障，再点击 **重试失败记录**。
2. 在 **记忆保存与维护** 点击 **开始维护**。维护会暂停新保存和记忆查询，停止发送等待中的记录和重试；服务已接受的任务仍会继续处理。个人 `/hindsight pause` 不能代替维护。
3. 点击 **刷新维护状态**，等待已接受任务完成。未发送或保存失败的记录在维护中不会自动保存；决定放弃时，勾选 **放弃尚未发送或保存失败的记录（不可恢复）**，再次点击 **开始维护**。这些记录以后不会补存。有未完成记录时，不能直接结束维护。
4. 任务状态或取消结果不明时继续暂停，核对原任务编号（operation ID）、相关子任务和 Bank。单独出现 `cancelled` 不能证明所有处理已经结束。查清结果前，不在官方页面修改内容，也不反复重试或取消。
5. 状态显示“可以修改记忆”后，复制官方管理地址，用浏览器登录并找到对应 Bank，完成内容操作。更正直接修改官方服务中的记忆，插件不另存一份更正来覆盖显示结果。
6. 核对用户与记忆库编号，点击 **结束维护并恢复**，再确认“正常记忆 / 只用已有记忆 / 暂停记忆”模式。结束维护会清理过时的短期语境；`/hindsight on` 只切换记忆模式，不能结束维护。

在官方页面删除整个 Bank 后，插件报告 `bank_missing` 并暂停该用户的记忆保存和查询，不会自动重建。可选择：

- 保留旧记忆：从匹配备份恢复原 Bank、文档与任务，核对用户归属和操作记录，再点击 **结束维护并恢复**。
- 舍弃旧记忆：点击 **清空此用户的记忆**，核对用户后点击 **确认清空记忆**。插件停用旧记忆库、作废旧待保存记录、登记新的记忆库编号，并继续核对和清理旧任务。清空无法恢复，AstrBot 自身的聊天记录不删除；清理完成后需重新开启“正常记忆”，新记忆库在首次保存聊天时自动创建。

任务结果无法确认且决定舍弃旧记忆时，也可使用上述清空流程。清理期间保持暂停；旧记忆库编号不再使用。旧 Bank 清理失败、删除凭据不一致或后端操作历史缺失时，数据恢复检查可能暂停所有用户的记忆功能。先按异常表恢复连接、控制数据或原后端状态，再重载并核对；不直接删除错误记录或控制数据。

## 历史本地更正交接

升级时，空的历史 `corrections` 表可自动移除；存在记录时，插件会报告 `legacy_corrections_require_handover` 并阻止启动，原始数据保留。

维护者先备份业务库、最新控制库与 Hindsight 数据，再逐条核对更正原文、目标事实、关联文档、待保存记录及后端据此生成的记忆。在停止插件保存和查询的情况下，将需要保留的更正交接到官方服务，并处理可能重新写入旧内容的保存任务。插件因交接提示无法启动时，先保持停用，再由维护者处理数据。

交接需保留数据归属、来源、任务编号、原提交内容的校验信息，以及较新的清空记录。仅删除 `corrections` 表会丢失更正，不能代替交接。插件不提供自动转换工具，应根据实际数据完成交接后再重载。

## 升级插件

1. 创建插件业务快照，单独保存最新控制库，并备份 Hindsight 数据库和配置。
2. 在 AstrBot 的插件管理中更新 Hindsight Bridge，重载插件。
3. 原有 API 配置、服务标识、Agent 标识和账号配置继续沿用，数据目录继续挂载。

格式升级保留既有身份、记忆空间、摘要与 operation UUID；非空历史更正会阻止启动并要求先完成数据交接。具体规则见 [实现说明](ARCHITECTURE.md#数据格式与恢复)。

升级后遇到恢复提示，按下文处理；运行状态可通过 `/hindsight status` 查看。

## 恢复业务数据

1. 停用插件或停止 AstrBot。
2. 保存运行目录中的最新控制数据，核对快照 `manifest.json` 中的部署 ID 和控制库 ID 与目标部署一致。
3. 需要恢复 Hindsight 时，先按下一节恢复数据库；只修复插件业务库时沿用后端数据库。
4. 用选定快照恢复 `runtime.sqlite3`。使用 SQLite 备份接口，或在进程完全停止后复制快照并移走目标目录的旧 `runtime.sqlite3-wal`、`runtime.sqlite3-shm`，避免混入旧日志。
5. 沿用最新控制目录，以及有效的 `control_state_dir`、`agent_id`、`service_id` 和账号配置，启动插件。
6. 插件自动检查数据归属、应用清空记录、清理失效任务并核对后端操作。通过恢复检查后，核对用户的记忆库编号和维护状态；维护中的用户需点击 **结束维护并恢复**。Bank 丢失时先恢复原 Bank，或按清空记忆流程舍弃旧数据。个人暂停的账号再用 `/hindsight on` 开启。

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
| `bank_missing` | 已建好的记忆库丢失，保持暂停；恢复原 Bank 后点击“结束维护并恢复”，或清空此用户的记忆，清理后重新开启。不会自动重建 |
| `maintenance_active` | 新保存和记忆查询已暂停，等待发送的记录与重试也暂停；管理员刷新维护状态，完成内容操作后点击“结束维护并恢复” |
| `maintenance_pending` | 等待服务已接受的任务完成；决定放弃未发送或失败记录时，勾选“放弃尚未发送或保存失败的记录（不可恢复）”并再次点击“开始维护”，然后刷新状态 |
| `maintenance_unknown_operation` | 提交或取消结果不明，继续暂停并核对原任务；决定舍弃旧记忆时用“清空此用户的记忆”，清理后重新开启 |
| `legacy_corrections_require_handover` | 历史本地更正非空，启动被阻止；保留原库，备份并完成实际数据交接后再重载 |
| “正在删除旧记忆” | 恢复后端连接和正确 API Key，插件会继续删除 |

`accepted_operation_missing` 等故障不能只靠重试消除。保留原提交内容、operation ID 与最新控制数据，找回匹配的后端备份；无法确认旧任务时保持暂停。决定舍弃旧记忆时按清空记忆流程处理，清空无法恢复，清理后需重新开启，AstrBot 聊天记录不删除。官方管理地址与记忆库编号可在插件页复制，网页登录密钥和 API Key 不放入普通用户诊断。

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
