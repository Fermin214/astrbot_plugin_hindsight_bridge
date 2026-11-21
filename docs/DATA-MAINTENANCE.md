# 备份、升级与恢复

本文介绍插件自身数据的备份与恢复，以及配套 Hindsight 服务的运维操作（连接已有服务时，服务数据与升级由原有部署自行管理）。个人记忆的日常查看与导出见 [使用指南](USAGE.md)。

## 数据目录与构成

完整备份需要包含插件业务数据、控制数据与插件配置；如果使用配套部署，还应备份配套的 PostgreSQL 数据库、安装目录与模型配置文件：

| 数据 | 默认位置 | 内容说明 |
| --- | --- | --- |
| 插件业务数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge/` | 待保存队列、去重记录、短期语境窗口与配套 `setup.json` |
| 插件控制数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge_control/` | 部署身份、用户与 Bank 绑定、代次、记忆模式、暂停状态与清空记录 |
| 配套 Hindsight 数据与配置 | 配套 PostgreSQL 数据卷、安装目录与设置卷 | 已整理的长期记忆、知识页正文、操作历史、`models.json` 与 `.env` |

控制目录路径 `control_state_dir` 建议留空使用默认位置；如需填写自定义路径，只能设为默认控制目录内部的子目录（且不能通过符号链接指向外部）。

通过安装器部署的服务文件在宿主机运行账号的 `~/.local/share/astrbot-memory-<id>/`（若自定义了 `HINDSIGHT_INSTALL_ROOT`，以实际输出路径为准），内含 `.env` 与 `compose.yaml`，数据库服务名为 `memory-db`，分别使用 `memory_postgres`（记忆库数据）与 `memory_settings`（模型配置 `models.json`）两个数据卷。

> **恢复原则**：用旧业务快照恢复时，**必须保留最新的控制数据**。例如周一备份、周二清空了记忆、周三用周一备份恢复，周二的清空记录仍需生效，否则旧消息会重新写入。同样，知识页开关和顺序也以最新控制库为准。

## 备份插件数据

插件自带 `scripts/backup.py`，通过 SQLite 在线备份接口导出业务快照与清单，同时保存一份独立的最新控制库副本。

下面以 Linux 容器部署（AstrBot 目录 `/AstrBot`、默认控制目录）为例，在 AstrBot 的 Python 环境执行：

```bash
python /AstrBot/data/plugins/astrbot_plugin_hindsight_bridge/scripts/backup.py \
  --data-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge \
  --control-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge_control \
  --output /AstrBot/data/backups/hindsight-business-YYYYMMDD \
  --latest-control /AstrBot/data/backups/hindsight-control-latest/control.sqlite3 \
  --backend-version 1.2.0
```

使用 Docker Compose 时，可在命令前加 `docker compose exec -T astrbot`（`astrbot` 替换为实际容器服务名）。Windows 的备份命令见 [Windows 原生部署](WINDOWS.md#备份与恢复)。

参数说明：
- `--output`：每次需指定尚不存在的新目录（将 `YYYYMMDD` 换为日期），且位置必须在两个插件数据目录之外。
- `--control-dir`：使用自定义子目录时对应修改。
- `--backend-version`：填写当前 Hindsight 版本（可在 `/v1/version` 核对），省略则记录为未知。
- `--latest-control`：固定指向同一个控制库副本路径。每次更新前，脚本会自动将旧副本归档到同级 `*.archive-*/control.sqlite3`，避免并发写入或版本错乱；若检测到控制库身份不符、修订倒退，或丢失了已确认的清空/解阻记录，脚本会报错拒绝覆盖，并将冲突副本留存在归档目录供核对。

备份完成后会生成 `runtime.sqlite3`、`manifest.json` 及控制库副本。请连同插件配置与 `setup.json` 一起复制到异机保存。

> 对话参考「使用记录」单独存放在业务目录的 `references.sqlite3` 中，常规备份脚本不包含此文件。

## 备份配套 Hindsight 数据库

停用插件并停止配套 Hindsight 的写入后，在配套服务安装目录执行：

```bash
docker compose stop memory-api
docker compose exec -T memory-db pg_dump -U hindsight -d hindsight -Fc -f /tmp/hindsight-backup.dump
docker compose cp memory-db:/tmp/hindsight-backup.dump ./hindsight-backup.dump
```

同时另存安装目录下的 `.env`、`compose.yaml` 以及 `memory_settings` 卷里的 `models.json`。备份完毕后启动 `memory-api` 并重新启用插件。

> 完整备份配套记忆库必须按本节导出 PostgreSQL，插件数据备份和个人 `/hindsight export` 都不能替代数据库备份。

## 在官方面板修改记忆

浏览器打开 Hindsight 官方 Control Plane（地址在 **插件设置 → Hindsight 服务连接** 复制），用 **用户与记忆** 顶部的 **Bank ID** 定位对应用户的记忆库。浏览、搜索、单条更正和导出直接在官方面板进行即可，无需暂停插件。访问与密钥获取见 [部署说明](../deploy/README.md#五打开官方-control-plane)。

需要注意的边界情况：
- **误删整个 Bank**：在官方页面直接删除整库后，插件会报告 `bank_missing` 并暂停该用户读写（不会自动新建覆盖）。若需保留旧记忆，先从数据库备份恢复原 Bank，再点击 **查看暂停状态 → 核对记忆库**；若打算重新开始，直接点 **清空记忆** 作废旧任务后切回「读写记忆」即可。
- **处理读写暂停**：升级或断连故障后若出现 **查看暂停状态**，点击后页面会核对在途与已接收任务。核对通过后点 **解除读写暂停** 即可恢复；确认不需要的未发送/失败记录可勾选后 **放弃记录**（已发往后端但结果不明的记录不可放弃）。

## 历史本地更正交接

从极早期版本升级时，如果业务库里残存非空的旧 `corrections` 表，插件会报错 `legacy_corrections_require_handover` 并暂停启动（空表会自动清理）。此时请先停用插件并备份数据，核对表中需要保留的更正内容并手动迁入对应 Bank 后，再重载插件。

## 升级插件

> **0.4.0 升级前必读**：请先备份插件业务数据、控制库和插件配置。**0.4.0 会自动将控制库从格式 0–6 迁移至格式 7，升级后无法直接降级回旧版插件**（旧版插件无法读取格式 7）。

1. 按上文备份插件业务快照、最新控制库与配置。
2. 在 AstrBot 插件管理中更新 Hindsight Bridge 并重载。
3. 沿用原有连接配置与数据目录，查看运行状态；如果页面提示重新确认账号，核对机器人身份后确认即可。

使用 QQ 官方机器人时，保持原平台实例、AppID 和用户 OpenID 不变，在 WebSocket 与 Webhook 之间切换会直接沿用已有记忆。升级后若提示恢复异常，可先通过 `/hindsight status` 查看原因并对照下文处理。

## 升级 Hindsight 服务

本节仅适用于插件配套部署。更新插件本体不会改变 Hindsight 容器；当有新版配套服务可用时（例如从 1.1.0 升至 1.2.0），面板顶部会出现 **查看升级** 横幅。

> 普通的 **重新部署** 只用于在同版本下修复容器或更新配置（例如修复 1.2.0 数据库扩展），不能把 1.1.0 升级为 1.2.0。

1. 点击顶部 **查看升级**，先点 **暂停插件读写**（全局暂停所有用户的保存，普通聊天正常进行）。
2. 停止其他外部客户端或定时任务对 Hindsight 的写入，等待当前在途任务与知识页刷新结束。
3. 按页内 **查看备份步骤** 完成插件与数据库备份（命令执行时也会自动在 `<原安装目录>-backups/upgrade-1.1.0-<随机编号>/` 下另存一份服务备份）。
4. 勾选确认后点击 **生成升级命令**，复制并在原服务器、原安装目录终端执行（如有自定义 `HINDSIGHT_INSTALL_ROOT` 请保持一致）。
5. 执行完毕后回到页面点击 **核对升级结果**，检查版本、Bank 归属及模型配置均正常后，点击 **解除升级暂停** 即可恢复写入。

如果服务端还是 1.1.0 且决定暂不升级，可点击 **暂不升级** 解除升级暂停（注意不要再在终端执行已复制的升级命令）。

## 恢复业务数据

1. 停用插件或停止 AstrBot 进程。
2. 先保存当前运行目录中的最新控制库，确认备份快照 `manifest.json` 里的部署 ID 和控制库 ID 与目标环境一致。
3. 确保所连接的 Hindsight 版本与快照对应；如配套数据库也需回退，先按下一节恢复数据库。
4. 将选定快照覆盖回 `runtime.sqlite3`（若进程已停止且直接复制文件，需同时删掉目标目录残存的 `runtime.sqlite3-wal` 和 `runtime.sqlite3-shm`）。
5. 保持最新控制目录以及原 `control_state_dir`、`agent_id`、`service_id` 和账号配置不变，启动 AstrBot。

启动后插件会自动校验身份归属、清理作废任务并核对后端状态（若此前曾有清空操作未完成，会继续清理旧库）。通过恢复检查后，在面板检查各用户状态并解除需要的读写暂停即可。

## 恢复配套 Hindsight 数据库

先在测试环境验证备份文件可用。停用插件后，将 `hindsight-backup.dump` 放到配套安装目录，保持 `memory-db` 运行并执行：

```bash
docker compose stop memory-api
docker compose cp ./hindsight-backup.dump memory-db:/tmp/hindsight-restore.dump
docker compose exec -T memory-db pg_restore -U hindsight -d hindsight --clean --if-exists --exit-on-error /tmp/hindsight-restore.dump
docker compose up -d memory-api
```

恢复时需保证模型配置（尤其是向量模型与维数）与该备份匹配，随后重新启用插件。

## 异常提示

| 错误码 / 状态 | 含义与处理方法 |
| --- | --- |
| `recovery_required:control_missing` | 找不到与业务库匹配的控制目录，检查挂载路径并恢复最新控制库后重载 |
| `control_mismatch` / `deployment_mismatch` | 业务库与控制库来自不同部署，对照 `deployment_id` 与 `control_id` 恢复匹配副本 |
| `control_rolled_back` | 当前控制库版本低于已知记录，请换回最新的控制库副本（保留清空与解阻记录） |
| `account_mapping_missing` | 控制库缺少账号映射，恢复完整控制库以找回归属关系 |
| `accepted_operation_missing` | Hindsight 中找不到已发送任务的记录，请暂停插件并让服务管理员核对数据库与操作历史 |
| `payload_digest_conflict` | 业务库内容摘要冲突，检查 `runtime.sqlite3` 完整性与备份来源 |
| `bank_missing` | 原记忆库已在服务端被删除；恢复原 Bank 后点 **查看暂停状态 → 核对记忆库**，或直接点 **清空记忆** 重新开始 |
| `maintenance_active` | 记忆库处于读写暂停中；在 **查看暂停状态** 确认服务操作完成后点击 **解除读写暂停** |
| `maintenance_pending` | 仍有任务未结束；等待执行完毕，或勾选放弃未发送/失败记录后再解除暂停 |
| `maintenance_unknown_operation` | 提交或取消任务结果不明；核对后端任务，或用 **清空记忆** 舍弃旧库重来 |
| `legacy_corrections_require_handover` | 存在非空的旧版 `corrections` 表，先备份并完成手工交接后再重载 |
| 正在删除旧记忆 | 清空流程等待后端响应，恢复服务连接与正确 API Key 后会自动继续删完 |

遇到 `accepted_operation_missing` 等核对失败时，不要反复重试，请保留最新控制库并联系管理员核对数据库；如果决定放弃旧记忆，直接走 **清空记忆** 即可。

## 控制目录挂载与旧路径迁移

如果希望用单独的卷或备份策略管理控制数据，请直接把独立目录挂载到默认控制路径，不要通过配置项指向插件目录外：

```yaml
services:
  astrbot:
    volumes:
      - ./astrbot-data:/AstrBot/data
      - ./hindsight-control:/AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge_control
```

升级自 0.2.7 以前版本且曾经把 `control_state_dir` 配在插件目录之外时，启动会提示 `control_directory_outside_plugin`。迁移步骤：

1. 备份配置、业务快照和最新控制库。
2. 完整停止 AstrBot，确保所有 SQLite 文件句柄释放。
3. 将原外部目录下的 `control.sqlite3`（连同仍在的 `-wal`、`-shm` 文件）复制到专属控制目录或新挂载目录，设置目录权限 `0700`、文件权限 `0600` 并匹配 AstrBot 运行用户。
4. 在插件配置里将 `control_state_dir` 留空（若放在子目录，填容器内专属目录下的绝对路径）。
5. 启动 AstrBot 并核对各用户 Bank、模式与清空状态，确认无误后再清理旧外部目录。

搬迁同一套 Hindsight 服务器地址时，保持 `service_id` 不变，更新 `api_url` 并把 `service_address_revision` 加 1；如果是换到全新的一套 Hindsight，则需使用新的 `service_id`。

## Reflect 与控制库格式

从 0.4.0 起控制库采用格式 7，用于记录 Reflect 请求状态及人工解阻日志：
- 升级后自动从旧格式迁入，保留全部账号、Bank、暂停记录与队列状态，**之后不可直接降级回旧版插件**。
- 当断网或服务重启导致 Reflect 请求结果未知时，管理员在确认 Hindsight 已重启或确实没有运行中的 Reflect 任务后，可点击 **用户与记忆 → 解除 Reflect 阻塞**（历史保留库也可在 **查看暂停状态** 中找到该入口），勾选确认即可放行。
- 解阻记录会长期保留在控制库中；恢复旧业务快照时必须继续保留最新控制库，备份脚本也会拒绝用丢失或回退了这些记录的控制库覆盖现有副本。
