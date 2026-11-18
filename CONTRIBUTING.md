# 贡献指南

安装与日常操作见 [安装指南](docs/INSTALL.md) 和 [使用指南](docs/USAGE.md)。消息采集、队列、用户隔离和数据格式见 [实现说明](docs/ARCHITECTURE.md)。

## 阅读路线

| 流程 | 数据流 | 验证入口 |
| --- | --- | --- |
| 私聊记录 | `observe_incoming` 核对来源。`add_memory` 冻结用户材料，由 `Outbox.capture`、`Worker`、`HindsightClient` 投递。`remember_assistant` 和 `persist_history` 从宿主持久历史补录主回复。 | `python tools/run-feedback-e2e.py --scope assistant-capture --output /tmp/bridge-capture` |
| 遗忘与恢复 | 私聊命令或面板提交控制事务。队列重放新代次，清理旧 Bank 和任务。恢复业务快照时读取独立控制库。 | `python tools/run-feedback-e2e.py --scope recovery --output /tmp/bridge-recovery` |
| 配套部署 | `core/panel.py` 提供管理 API。启动命令来自 `core/installer.py`。`core/managed_connections.py` 持久化接入进度，配套容器验证并应用模型配置。 | `python tools/run-managed-e2e.py --scenario connection --output /tmp/bridge-connection` |

| 名称 | 含义 |
| --- | --- |
| `Scope.id`、`scope_id`、`source_id` | 同一个平台来源标识。业务库使用 `scope_id`，控制库映射使用 `source_id`。 |
| `principal_id`、`owner_id` | 逻辑用户和该用户在部署、Agent、记忆空间中的归属。队列按 `owner_id` 排他投递。 |
| Bank、代次 | Hindsight 记忆库及其版本。清空记忆推进代次，作废旧 Bank 与旧待保存材料。 |
| `background`、`reading`、`reflection` | 随请求附带的知识页、按需读取工具和原生 Reflect 执行。配置键及接口沿用这些名称。 |

面板资产在 `views/memory/`。兼容范围、恢复和配套管理分别见 [兼容范围](docs/SUPPORT.md)、[备份与恢复](docs/DATA-MAINTENANCE.md)、[安装与管理设计](docs/SETUP-DESIGN.md)。

## 提交要求

保持 AstrBot 与 Hindsight 的公开接口边界，不修改宿主全局设置或其他插件。格式或身份规则变化需附迁移步骤。

新代码默认不写解释性注释，禁止用注释为捷径找理由，保留宿主使用的命令帮助、机器指令和极少数可说明具体丢数风险的例外。

提交前确认安装包包含全部运行模块、页面资产和部署材料，相关检查通过；涉及安装、升级或数据恢复的修改须验证对应使用流程。

## 后端兼容验证

CI 面向项目回归与社区兼容，按新增提交涉及的文件选择检查；手动运行通用检查时覆盖全部范围。语法、元数据、分发包和文档链接始终检查。Python 行为与回归测试变化运行完整回归；仅页面逻辑变化运行页面测试；文档和独立验收脚本变化不触发业务回归。

| 检查 | 用途 | 触发范围 |
| --- | --- | --- |
| `check` | 回归、页面、语法及分发边界 | 按代码类型选择回归，基础检查始终执行 |
| `windows` | Windows 回归、分发包安装与宿主重载 | 每次插件检查 |
| `astrbot-smoke` | 官方宿主中的安装、重载、消息、临时内容、升级与卸载 | 插件入口、核心模块、配置结构、运行依赖及宿主检查代码 |
| `hindsight-model-contracts` | 两版 Hindsight SDK 的聊天、嵌入及重排协议兼容 | 供应商适配、协议转换、模型探测、运行依赖及后端版本配置 |

模型协议检查使用固定 Hindsight 1.1.0 / 1.2.0 镜像运行 `tools/check-model-protocols.py`，模型响应来自本地协议夹具，不启动完整服务或调用外部模型。

版本收尾、历史故障复现和专项真实环境验收由开发者按改动在独立环境执行，不加入 CI。代码、配置和相关环境未变化时复用已有证据；失败后修复受影响部分并重验，不重复执行无关场景。`python tests/integration/install_startup.py --output /tmp/新的测试目录` 用于旧数据库镜像权限故障及修复后的安装重试，仅在安装与镜像修复需要时执行。

`python tools/run-backend-compat.py --output /tmp/新的测试目录` 使用独立配套服务、PostgreSQL 和两版 AstrBot，验证旧库升级、知识页、模型配置失败恢复、在途任务重启和 Bank 删除。需要 Docker 权限、本地已准备的官方镜像与测试依赖；模型与平台端为合成数据，不证明真实账号投递或模型质量。可用 `--baseline` 指定包含旧版安装材料的提交；`--scope deletion` 只验证新版删除路径，`--scope maintenance` 验证原生整合任务替代、解除保存和查询暂停、双宿主和删除，报告会标明范围。删除检查在专属库停止 Worker 后构造非空图与实体后台任务队列，记录各表前后计数，避免被后台排空掩盖缺陷。工具结束后清理自己创建的资源，证据与数据库备份保留在指定目录，请妥善保管。

## 修改与反馈

`python tools/run-feedback-e2e.py --output /tmp/新的测试目录` 使用独立的真实 AstrBot/Hindsight、原生 Dashboard 与合成平台/模型验证知识页反馈、0.2.6 升级、外部控制目录拒绝、停机迁移和旧业务快照恢复。支持 `--astrbot-version 4.29.1|4.29.4` 与 `--hindsight-version 1.1.0|1.2.0`，不调用外部模型，不使用真实 QQ 账号。需要 Docker、本地官方镜像及测试依赖；结束后清理自身容器和数据卷，证据保留在指定目录。

添加 `--scope single-page` 验证 0.2.7 升级后的单页引用与默认值、独立页面预算、共享 token/字符边界、可配置读取子限时及原生页面长度与索引提示。页面包装与最终临时聊天请求一并取证，覆盖整页使用和整页跳过。

`--scope multi-page` 验证 0.2.8 单页升级、多页添加与排序、共享预算、逐页反馈、重载、按序号查看及完整遗忘；`--scope multi-page-recovery` 专门验证多篇原生页删除后的逐个移除，以及机器人身份查询耗时 300ms 时保存和排序 8 篇页面。只修改选页恢复或身份检查时使用后者，复用既有预算和聊天注入证据。

`--scope read-tools` 验证有序知识页版本升级后的按需工具：原生 Agent 搜索、整行续读、版本变化、事实 Recall、来源和最近历史、同消息额度、按用户隔离、停用、实际保存材料、宿主会话与 Docker 日志、重载及完整遗忘。使用合成工具模型和 OneBot 平台端，不改变已有部署。

Linux 与 Windows 的分发验收使用 `python tools/build-test-package.py --distribution --revision <提交> --output <目录>`。清单包含提交和每个分发文件的摘要。同包重装验证安装与重载。历史版本升级验证数据迁移，并记录历史包来源。

`python tools/run-parameters-e2e.py --mode existing --astrbot-version 4.29.4 --hindsight-version 1.2.0 --output /tmp/新的测试目录` 验证面板参数到实际聊天的完整流程；`--mode managed` 使用独立配套安装。只涉及高级参数、队列或排除规则时，添加 `--scope advanced`，复用其他场景的已有证据。按变更选择接入方式、范围和版本，不以固定矩阵重复执行。

提交问题时提供版本、复现步骤、预期、实际结果和脱敏日志。不要提交账号凭据、真实聊天记录、运行数据库或本地环境文件。

恢复业务快照不得覆盖更新的独立控制记录。
