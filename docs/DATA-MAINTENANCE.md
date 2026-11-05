# 备份、升级与恢复

本文供管理 AstrBot 和 Hindsight 服务的人使用。只想查看或导出个人记忆，请看 [使用指南](USAGE.md)。

## 需要保存什么

完整备份包含三部分：

| 数据 | 默认位置 | 用途 |
| --- | --- | --- |
| 插件业务数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge/` | 尚未完成的记录、去重信息、短期语境和配套服务的 `setup.json` |
| 插件控制数据 | AstrBot 的 `data/plugin_data/astrbot_plugin_hindsight_bridge_control/` | 部署身份、账号与 Bank 归属、代次、模式、保存和查询暂停状态、清空记录和操作凭据 |
| Hindsight 数据与配置 | PostgreSQL 数据库及后端的 `.env` | 已整理的记忆、来源材料、任务历史及服务配置 |

官方页面中的 Bank 是每个用户的记忆库，插件页用“记忆库 ID”标明归属。首次保存聊天时自动建库，无需在官方页面另建。

填写过 `control_state_dir` 时，控制数据在指定目录中。默认两个插件目录随 AstrBot 的 `data` 持久保存；无需为了首次安装另加控制目录挂载。

通过向导安装的服务文件位于执行安装命令者的 `~/.local/share/astrbot-memory-<id>/`，`<id>` 为服务编号；自定义过安装目录时，以安装输出的路径为准。该目录包含 `.env` 和 `compose.yaml`，需要一同备份；在这个目录中操作数据库时，数据库服务名为 `memory-db`。`setup.json` 保存配套服务身份和 Key，应与插件配置一起保存。数据库存放在 `memory_postgres` 数据卷中，模型配置与凭据存放在 `memory_settings` 数据卷的 `models.json` 中，两个卷都需要备份。

**恢复旧业务备份时，保留最新的控制数据。** 例如周一备份、周二清空记忆、周三恢复周一备份，周二的清空记录仍需生效，防止旧记忆重新出现。知识页选择也在最新控制库中，恢复旧插件配置不会恢复已关闭或失效的选择；知识页正文与历史在 Hindsight 中，需随数据库备份。

## 备份插件数据

插件附带 `scripts/backup.py`，通过 SQLite 备份接口生成业务快照和清单，同时可单独保存最新控制库副本。

以下示例适用于 AstrBot 安装目录为 `/AstrBot`、控制目录使用默认值的 Linux 容器。在 AstrBot 使用的 Python 环境中执行：

```bash
python /AstrBot/data/plugins/astrbot_plugin_hindsight_bridge/scripts/backup.py \
  --data-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge \
  --control-dir /AstrBot/data/plugin_data/astrbot_plugin_hindsight_bridge_control \
  --output /AstrBot/data/backups/hindsight-business-YYYYMMDD \
  --latest-control /AstrBot/data/backups/hindsight-control-latest/control.sqlite3 \
  --backend-version 1.2.0
```

Docker Compose 部署可以在 AstrBot 的 Compose 目录中，给上述命令加上 `docker compose exec -T astrbot` 前缀；`astrbot` 替换成实际服务名。

将 `YYYYMMDD` 替换为备份日期，每次更换 `--output` 的目录名，目标必须是尚不存在的新目录。使用自定义控制目录时，也要替换 `--control-dir`。业务快照和控制副本的输出位置都须位于两个插件数据目录之外。

`--backend-version` 填写实际 Hindsight 版本，可通过服务的 `/v1/version` 核对；省略时清单记录为未知（`null`），不会根据插件默认安装版本猜测。

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

## 在官方面板修改记忆

Hindsight 官方管理页面（Control Plane）提供浏览、搜索、更正、删除、导入和导出。访问地址与登录方式见 [部署说明](../deploy/README.md#五打开官方-control-plane)。

从 **插件设置 → 连接与访问** 复制 Hindsight 管理地址，在浏览器打开；从 **用户与记忆** 复制 Bank ID，找到对应记忆库后直接管理内容。普通内容操作不需要暂停插件或服务，也不要求在插件页额外确认。完整清空使用插件 **清空记忆**，同时作废旧来源和待保存记录。

升级或故障造成的读写暂停在 **查看暂停状态** 中处理。页面自动核对在途请求、已接收任务与 Bank；结果不明时保持暂停。选择放弃未发送或已确认失败记录时，勾选确认后点击 **放弃记录**，已发送但结果不明的记录不能放弃。检查通过后点击 **解除读写暂停**。

个人记忆模式不取消 Hindsight 已接收的任务，原生整合与知识页刷新按服务自身设置运行。批量迁移或一致性备份需停止所有写入并等待任务结束，不能仅靠个人 `/hindsight pause`。

在官方页面删除整个 Bank 后，插件报告 `bank_missing` 并暂停该用户的记忆保存和查询，不会自动重建。可选择：

- 保留旧记忆：从匹配备份恢复原 Bank、文档与任务，核对用户归属和操作记录，在 **查看暂停状态** 中点击 **核对记忆库**。已有读写暂停时，等检查通过后再解除。
- 舍弃旧记忆：点击 **清空记忆**，核对用户后点击 **确认清空**。插件停用旧记忆库、作废旧待保存记录、登记新的记忆库 ID，并继续核对和清理旧任务。清空无法恢复，AstrBot 自身的聊天记录不删除；清理完成后需重新开启“读写记忆”，新记忆库在首次保存聊天时自动创建。

任务结果无法确认且决定舍弃旧记忆时，也可使用上述清空流程。清理期间保持暂停。连接、控制数据或后端操作历史异常时，按下方的异常提示恢复，再重载插件核对状态。

## 历史本地更正交接

升级时，空的历史 `corrections` 表会自动移除；存在记录时显示 `legacy_corrections_require_handover`，需先完成旧更正的交接。

管理员先停用插件，备份业务库、最新控制库与 Hindsight 数据，核对更正内容、归属和相关保存任务，将需要保留的更正迁入官方服务，再重载插件。原表应保留到交接完成，避免丢失更正内容。

## 升级插件

1. 创建插件业务快照，单独保存最新控制库，并备份 Hindsight 数据库和配置。
2. 在 AstrBot 的插件管理中更新 Hindsight Bridge，重载插件。
3. 沿用原有连接、账号配置和数据目录，查看运行状态并按页面提示确认账号。

使用 QQ 官方机器人时，在用户与记忆中添加用户 OpenID 并确认授权；同一实例在 WebSocket 与 Webhook 间切换时保留原 AppID 和 OpenID，即可继续使用原有记忆。

格式升级与旧更正的处理规则见 [实现说明](ARCHITECTURE.md#数据格式与恢复)。

升级后遇到恢复提示，按下文处理；运行状态可通过 `/hindsight status` 查看。

## 升级 Hindsight 服务

更新插件后，已有 Hindsight 容器仍使用原版本。升级服务需要另外执行以下步骤；已有外部服务由其管理员升级。

插件页显示实际 Hindsight 版本。1.1.0 仍受支持，升级为可选操作，不会自动替换服务。有适用更新时，顶部横幅提供 **查看升级**；外部服务显示 **查看升级指引**。忽略只针对该连接与目标版本，升级未结束时不能忽略。

普通重新部署不会升级旧版：命令执行前必须确认原服务实际为 1.2.0，确认失败时不替换文件。版本未知时可生成重新部署命令，但它同样不能把未知或 1.1.0 服务升级为默认新版；先恢复连接再使用升级入口。完全卸载后的同身份新安装仍可进行。

面板可为本插件所有用户（包括仍有效的历史范围）暂停保存和查询，不会丢弃待保存或失败记录。先备份插件业务数据、最新控制数据和插件配置，并停止其他客户端及官方面板写入，再点击 **生成升级命令**。有未就绪、丢失的记忆库或未完成的清理时，不会生成命令。

在原服务器、原安装路径执行命令。命令先下载新版镜像，再保存原服务配置与模型设置、停止所属 API、备份数据库并检查备份目录，随后替换服务。备份位于原安装目录旁的 `<原安装目录>-backups/upgrade-1.1.0-<随机编号>/`，权限仅允许执行账号访问；包含密钥和记忆数据，须另存到安全位置。该备份目录在完整卸载服务时保留，需由管理员单独管理。数据库目录检查不等于实际恢复演练，重要数据升级前仍需独立恢复验证。

命令成功后点击 **核对升级结果**，检查实际版本、原记忆库归属及嵌入模型标识；此检查不替代逐条内容核验。确认旧记忆可用，在同一区域点击 **解除升级暂停**，一次解除升级新增的暂停，保留原有暂停以及只读、暂停等记忆模式。旧版安装记录缺少暂停来源时，仍需在「用户与记忆 → 查看暂停状态」逐个解除读写暂停。未核对前不能解除升级暂停。

决定暂不升级且原服务仍正常运行于 1.1.0 时，点击 **暂不升级**。只解除升级新增的暂停，原有暂停及模式保留。面板操作不会撤销已经复制的升级命令，放弃后请勿再执行；服务停止或升级失败时先检查日志并按恢复说明处理。服务已为 1.2.0 时不提供此入口。

升级暂停开始后，面板和 QQ 的清空入口暂时停用，包括此前取得的 QQ 清空确认码；解除升级暂停或选择暂不升级后恢复。生成命令、保存核对结果及批量解除暂停前，还会检查整个连接的在途请求和遗忘清理，旧记忆库的晚到请求也必须处理完成。已经建立却丢失的记忆库须先恢复；只有从未创建、没有已派发请求且远程列表也不存在的空白记忆库可以继续升级。

1. 1.1.0 配套升级从顶部横幅进入并点击 **暂停插件读写**；其他手工升级先停止插件。处理未确认或失败任务，等待原生整合、知识页刷新和其他后台任务结束，并停止其他客户端与官方面板写入。
2. 在停写期间备份插件业务库、最新控制库、Hindsight PostgreSQL、模型设置和服务配置。配套服务的模型与凭据在 `memory_settings` 卷的 `models.json` 中。先用独立环境确认备份可恢复。
3. 更新插件并重载，沿用原 `service_id`、数据目录和配套安装记录。1.1.0 配套服务使用上述升级入口；同版本修复使用 **重新部署**。在原服务主机、原安装路径执行，使用自定义 `HINDSIGHT_INSTALL_ROOT` 时保持原值。命令沿用该安装的身份、密钥、模型设置和数据卷，替换所属服务文件与容器；不要先执行卸载。
4. 连接手工 Compose 服务时，在原 `deploy` 目录将 `.env` 的 `HINDSIGHT_IMAGE` 设为目标固定版本；同步更新本插件部署模板的退出宽限，然后执行 `docker compose pull hindsight` 和 `docker compose up -d hindsight`。保留原数据库及其他配置。
5. 核对 `/v1/version`、API 认证和官方管理面板。检查原 Bank、源文档、知识页与已接收任务仍在，模型选择和嵌入维数保持一致；重载插件，确认无恢复错误后逐用户解除读写暂停。需要继续暂停的账号保留原模式。

升级失败时保持保存和查询暂停并检查服务日志。更换嵌入模型或维数需要独立的数据迁移，不与版本升级一起操作。需要退回旧服务时，先在独立环境确认旧版本与升级前备份匹配，再恢复相应数据库及模型配置；保留最新控制库和清空记录，避免恢复已遗忘的材料。

## 恢复业务数据

1. 停用插件或停止 AstrBot。
2. 保存运行目录中的最新控制数据，核对快照 `manifest.json` 中的部署 ID 和控制库 ID 与目标部署一致。
3. 需要恢复 Hindsight 时，先按下一节恢复数据库；只修复插件业务库时沿用后端数据库。
4. 用选定快照恢复 `runtime.sqlite3`。使用 SQLite 备份接口，或在进程完全停止后复制快照并移走目标目录的旧 `runtime.sqlite3-wal`、`runtime.sqlite3-shm`，避免混入旧日志。
5. 沿用最新控制目录，以及有效的 `control_state_dir`、`agent_id`、`service_id` 和账号配置，启动插件。
6. 插件自动检查数据归属、应用清空记录、清理失效任务并核对后端操作。通过恢复检查后，核对用户的记忆库 ID和保存和查询暂停状态；已暂停保存和查询的用户需点击 **解除读写暂停**。Bank 丢失时先恢复原 Bank，或按清空记忆流程舍弃旧数据。个人暂停的账号再用 `/hindsight on` 开启。

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
| `bank_missing` | 已建好的记忆库丢失，保持暂停；恢复原 Bank 后点击“核对记忆库”，已有读写暂停时再解除；也可清空记忆，清理后重新开启。不会自动重建 |
| `maintenance_active` | 新保存和记忆查询已暂停，等待发送的记录与重试也暂停；管理员检查暂停进度，完成内容操作后点击“解除读写暂停” |
| `maintenance_pending` | 等待服务已接受的任务完成；决定放弃未发送或失败记录时，在“查看暂停状态”勾选确认并点击“放弃记录”，等待自动检查 |
| `maintenance_unknown_operation` | 提交或取消结果不明，继续暂停并核对原任务；决定舍弃旧记忆时用“清空记忆”，清理后重新开启 |
| `legacy_corrections_require_handover` | 历史本地更正非空，启动被阻止；保留原库，备份并完成实际数据交接后再重载 |
| “正在删除旧记忆” | 恢复后端连接和正确 API Key，插件会继续删除 |

`accepted_operation_missing` 等故障不能只靠重试消除。保留原提交内容、operation ID 与最新控制数据，找回匹配的后端备份；无法确认旧任务时保持暂停。决定舍弃旧记忆时按清空记忆流程处理，清空无法恢复，清理后需重新开启，AstrBot 聊天记录不删除。官方管理地址与记忆库 ID可在插件页复制，网页登录密钥和 API Key 不放入普通用户诊断。

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
