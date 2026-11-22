# 备份、升级与恢复

本文介绍插件自身数据及专属配套资源的维护。连接已有 Hindsight 时，服务数据与升级由原有部署管理；个人记忆查看与导出见 [使用指南](USAGE.md)。

## 数据目录与构成

插件数据统一位于 AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge/`：

| 文件 / 目录 | 内容 |
| --- | --- |
| `bridge.sqlite3` | 单主库：身份、授权、Bank 代次、设置、持久化队列、命令回执、暂停与清空进度 |
| `bridge.sqlite3-wal` / `bridge.sqlite3-shm` | SQLite 运行侧文件；运行时不得手动删除 |
| `references.sqlite3` | 独立使用记录与跳过记录库，上限 64 MiB；故障不阻塞消息入队 |
| `setup.json` | 配套服务连接与安装状态，含敏感配置 |
| `legacy-v0.5-backup/` | 0.5.0 启动迁移前的完整双目录备份，含历史聊天内容副本 |

完整备份还应另存 AstrBot 的插件配置。使用配套 Hindsight 时，另存 PostgreSQL 数据库、安装目录、`.env` 和 `models.json`；插件 SQLite 快照不能替代远端记忆库备份。

0.6.0 不再运行独立 `control.sqlite3`。旧 `control_state_dir` 仅用于定位迁移来源，迁移完成后无需单独挂载或备份控制目录。

> **恢复原则**：恢复 `bridge.sqlite3` 整库快照会同时恢复快照时刻的授权、设置、队列和清空状态。**不要用清空记忆或撤权前的旧快照覆盖运行库**；不再支持“旧业务库 + 最新控制库”混拼恢复。

## 备份插件数据

在 AstrBot 所用的 Python 环境运行 `scripts/backup.py`。脚本通过 SQLite 在线备份接口读取已提交数据，包含 WAL 中的内容，无需停用插件：

```bash
python /AstrBot/data/plugins/astrbot_plugin_hindsight_bridge/scripts/backup.py \
  --data-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge \
  --output /AstrBot/data/backups/hindsight-YYYYMMDD \
  --backend-version 1.2.0
```

使用 Docker Compose 时，可在命令前加 `docker compose exec -T astrbot`，服务名按实际部署填写。Windows 命令见 [Windows 原生部署](WINDOWS.md#备份与恢复)。

- `--output` 必须是插件数据目录之外、尚不存在的新目录。
- `--backend-version` 填写 `/v1/version` 返回的实际 Hindsight 版本；省略时清单记为未知。
- 输出包含 `bridge.sqlite3`、`manifest.json`，以及存在时的 `references.sqlite3`。清单记录数据库格式、部署身份、时间和文件 SHA-256。
- 主库和使用记录各自在线取快照；恢复时按主库授权及代次核验使用记录，过期记录会被清理。
- `--control-dir` 和 `--latest-control` 已移除。

连同插件配置及 `setup.json` 复制到异机保存。备份包含聊天内容和用户提示，按敏感资料保管；POSIX 输出目录为 `0700`、文件为 `0600`。

## 备份配套 Hindsight 数据库

配套安装目录默认在运行账号的 `~/.local/share/astrbot-memory-<id>/`；如设置了 `HINDSIGHT_INSTALL_ROOT`，以实际路径为准。停用插件并停止其他客户端写入后执行：

```bash
docker compose stop memory-api
docker compose exec -T memory-db pg_dump -U hindsight -d hindsight -Fc -f /tmp/hindsight-backup.dump
docker compose cp memory-db:/tmp/hindsight-backup.dump ./hindsight-backup.dump
```

另存 `.env`、`compose.yaml` 和 `memory_settings` 卷中的 `models.json`。完成后启动 `memory-api` 并启用插件。Hindsight Control Plane 的个人记忆导出不能替代 PostgreSQL 全量备份。

## 在官方面板修改记忆

从 **插件设置 → Hindsight 服务连接** 复制官方 Control Plane 地址，用 **用户与记忆** 顶部的 Bank ID 定位用户。浏览、搜索、单条更正和导出无需暂停插件。

- **误删整个 Bank**：插件报告 `bank_missing`，暂停该用户读写，不会自动创建空库覆盖。要保留旧记忆，先恢复原 Bank，再点 **查看暂停状态 → 核对记忆库**；要重新开始，点 **清空记忆** 后切回读写模式。
- **读写暂停**：在 **查看暂停状态** 核对在途请求，再点 **解除读写暂停**。未发送或已明确失败的记录可放弃；已提交但结果不明的记录不能直接放弃。
- **清空与记录库锁定**：清空立即作废旧代次；若使用记录库被锁住，显示清理未完成，其他用户仍可入队。解锁后重试，核验目标用户的使用记录、交付记录和跳过记录均删净，才标记完成。

## 历史本地更正交接

早于 0.5.0 的部署应先升级到 0.5.0，完成其存储转换。若旧版提示存在未交接的 `corrections` 数据，先停用并备份，核对后将需保留的更正迁入对应 Bank；不要删除来源库来绕过启动校验。

## 升级插件

**0.5.0 → 0.6.0 会合并主库，不能直接降级。**

1. 停用插件或停止 AstrBot，完整另存旧业务目录、旧控制目录与插件配置，包含仍存在的 SQLite 侧文件。
2. 在 AstrBot 插件管理中更新至 0.6.0，保持原 `service_id`、`agent_id`、平台实例、账号及旧数据路径不变。
3. 启动时插件排他锁定旧库，核对部署身份和修订；先生成 `legacy-v0.5-backup/`，再向临时主库导入并原子切换。缺一库、来源错配、损坏或空间不足都会中止，不会按新装启动。
4. 核对用户 Bank、待发队列、设置、清空进度和使用记录。引用回复、带链接及提及的私聊升级后默认正常保存；媒体占位只保留类型，不提取媒体内容。
5. 确认稳定运行后，及时删除 `legacy-v0.5-backup/`，并移除不用的旧控制目录挂载，避免长期留存旧聊天明文副本。

迁移中断时，修复空间、权限或来源问题后重启。备份和导入中断保留原双库；已完成切换时以 `bridge.sqlite3` 为准，不重复导入备份。

### 首次启用前撤回迁移

仅在新版尚未对外启用时使用停写备份撤回：停止 AstrBot，将 `legacy-v0.5-backup/` 先复制到插件目录之外，再以其中 `runtime/` 和 `control/` 完整替换原两目录，并恢复 0.5.0 插件及配置。不要把新版主库留在旧版运行目录中混用。

新版运行后产生的消息、设置和清空操作不在旧备份中；日常灾备使用下一节的 0.6.0 整库恢复契约。

## 升级 Hindsight 服务

本节仅适用于专属配套部署；更新插件不会自动升级 Hindsight 容器。普通 **重新部署** 用于修复同版本服务，不能替代跨版本升级。

1. 点 **查看升级 → 暂停插件读写**，并停止其他客户端及定时任务写入。
2. 等待在途任务和知识页刷新结束，按页内步骤备份插件和 PostgreSQL。
3. 勾选备份确认，生成命令并在原服务器、原安装目录执行；保持 `HINDSIGHT_INSTALL_ROOT` 一致。
4. 回到页面 **核对升级结果**，检查版本、Bank 及模型配置，再解除升级暂停。

服务仍是旧版且决定取消时，点 **暂不升级**；不要再执行已复制的升级命令。

## 恢复业务数据

1. 停止 AstrBot，另存运行目录及配置，选定匹配的插件和远端数据库快照。
2. 若需恢复 PostgreSQL，先按下一节恢复服务及对应模型配置。
3. 用备份脚本校验清单并恢复主库和使用记录：

```bash
python /AstrBot/data/plugins/astrbot_plugin_hindsight_bridge/scripts/backup.py \
  --data-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge \
  --restore /AstrBot/data/backups/hindsight-YYYYMMDD
```

4. 保持原 `service_id`、`agent_id`、平台实例和账号配置，恢复 `setup.json` 后启动 AstrBot。
5. 核对各用户状态。快照中未完成的清空会继续；快照指向的已建立 Bank 若已被远端删除，触发 `bank_missing`，不会盲建空库。先恢复原 Bank，或清空旧记忆后重新开始。

脚本拒绝活跃 worker、损坏清单、摘要不符和非法格式。不得在插件运行中直接覆盖 SQLite 文件或删除 WAL/SHM。恢复整库也会恢复旧授权与队列，不能用最新控制副本阻断回滚。

## 恢复配套 Hindsight 数据库

先验证备份可用。停用插件并保持 `memory-db` 运行，在配套安装目录执行：

```bash
docker compose stop memory-api
docker compose cp ./hindsight-backup.dump memory-db:/tmp/hindsight-restore.dump
docker compose exec -T memory-db pg_restore -U hindsight -d hindsight --clean --if-exists --exit-on-error /tmp/hindsight-restore.dump
docker compose up -d memory-api
```

恢复时保证向量模型、维数及其他模型配置与备份匹配，随后启用插件。

## 异常提示

| 错误码 / 状态 | 处理方法 |
| --- | --- |
| `migration_legacy_database_missing` | 恢复匹配的 0.5.0 业务库和控制库，核对目录挂载 |
| `migration_identity_mismatch` | 两库部署身份或账号映射不一致，恢复同一部署的完整副本 |
| `migration_control_rolled_back` | 旧控制库修订倒退，恢复迁移时匹配的最新旧控制库 |
| `migration_legacy_version` / `migration_legacy_corrupt` | 来源格式不符或损坏，先在 0.5.0 修复并验证数据 |
| `migration_insufficient_space` / `migration_backup_incomplete` | 释放空间或恢复完整备份；保留原两库，不删除它们来重试 |
| `main_database_invalid` / `main_identity_mismatch` | 主库格式、完整性或身份异常，从匹配的整库快照恢复 |
| `migration_main_database_missing` | 迁移备份仍在但主库缺失；恢复主库，或按首次启用前撤回步骤还原双库 |
| `database_path_invalid` | 主库、侧文件或锁文件是符号链接，核对文件归属与实际路径 |
| `accepted_operation_missing` | 后端找不到已接收操作，保留主库并核对远端数据库和操作历史 |
| `payload_digest_conflict` | 冻结正文摘要冲突，核查主库完整性和备份来源 |
| `bank_missing` | 恢复原 Bank 并核对，或清空旧记忆后重新开始 |
| `maintenance_active` / `maintenance_pending` | 核对在途请求，等待完成后解除暂停 |
| `maintenance_unknown_operation` | 提交或取消结果不明，核对后端操作，或清空旧库 |
| 正在删除旧记忆 | 恢复服务连接或记录库可写状态，插件自动继续删净 |

同一 Hindsight 搬迁地址时保留 `service_id`，更新 `api_url` 并递增 `service_address_revision`；换到全新服务时使用新的 `service_id`。

## Reflect 与主库格式

`reflection_requests` 与其他状态同存主库。启动时将未结束的 `prepared/dispatched/unknown` 请求归档，保留原始请求 ID、错误及归档记录。Reflect 超时不锁住后续请求或清空；迟到结果不得进入新代次。日常恢复这些记录同样遵循整库快照语义。
