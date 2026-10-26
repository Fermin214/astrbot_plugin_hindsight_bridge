# 备份、升级与恢复

本文供维护 AstrBot 和 Hindsight 服务的人使用。只想查看或导出个人记忆，请看 [使用指南](USAGE.md)。

## 需要保存什么

完整备份包含三部分：

| 数据 | 默认位置 | 用途 |
| --- | --- | --- |
| 插件业务数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge/` | 尚未完成的记录、去重信息、更正和配套服务的 `setup.json` |
| 插件控制数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge_control/` | 账号归属、使用模式和清空记录 |
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

## 升级插件

1. 创建插件业务快照，单独保存最新控制库，并备份 Hindsight 数据库和配置。
2. 在 AstrBot 的插件管理中更新 Hindsight Memory，重载插件。
3. 原有 API 配置、服务标识、Agent 标识和账号配置继续沿用，数据目录继续挂载。

业务数据格式 2、3 会自动升级到格式 4，既有身份、记忆空间和任务保持连续。具体格式见 [实现说明](ARCHITECTURE.md#数据格式与恢复)。

升级后遇到恢复提示，按下文处理；运行状态可通过 `/hindsight status` 查看。

## 恢复业务数据

1. 停用插件或停止 AstrBot。
2. 保存运行目录中的最新控制数据，核对快照 `manifest.json` 中的部署 ID 和控制库 ID 与目标部署一致。
3. 需要恢复 Hindsight 时，先按下一节恢复数据库；只修复插件业务库时沿用后端数据库。
4. 用选定快照恢复 `runtime.sqlite3`。使用 SQLite 备份接口，或在进程完全停止后复制快照并移走目标目录的旧 `runtime.sqlite3-wal`、`runtime.sqlite3-shm`，避免混入旧日志。
5. 沿用最新控制目录，以及有效的 `control_state_dir`、`agent_id`、`service_id` 和账号配置，启动插件。
6. 插件自动检查数据归属、应用清空记录、清理失效任务并核对后端操作。检查完成后继续使用；原先处于暂停的账号可用 `/hindsight on` 开启。

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
| “远端清理中” | 恢复后端连接和正确 API Key，插件会继续删除 |

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
