# 插件实现说明

本文面向贡献者，说明 Bridge 的模块分工、身份模型、队列、召回、知识页、工具与恢复机制。安装与日常使用见 [安装指南](INSTALL.md) 和 [使用指南](USAGE.md)。

## 模块分工

- **AstrBot**：负责接收消息、维护会话历史、调用聊天模型和发送回复。
- **Hindsight**：负责记忆提取（Retain）、召回（Recall）、整合（Consolidation）、知识页生成及内容管理。
- **Bridge**：负责来源校验、用户隔离、材料冻结、持久化队列、临时上下文注入、连接管理和暂停/恢复协调。

Bridge 仅通过 AstrBot 的公开钩子/插件接口与 Hindsight 的公开 HTTP API 通信，不修改双方源码，也不往 AstrBot 的 Python 环境安装 Hindsight 或额外调用摘要模型（自动 reflect 默认关闭）。Linux 支持配套部署或接入已有服务，Windows 原生宿主接入已有服务（详见 [兼容范围](SUPPORT.md)）。

面板中 `parameters/save` 只修改全局记忆参数（与标准 `AstrBotConfig` 共用存储）：提交时核对完整配置修订，校验数值范围、前缀和分词编码并立即生效于后续请求（已入队的冻结材料继续沿用提交时的实例），连接、身份、Bank 与暂停状态均不受影响。

## 用户、来源校验与隔离

权限校验以 `(platform_instance_id, user_id)` 精确匹配 `allowed_accounts`（空列表关闭所有人的记忆存取）。每个已确认身份的逻辑用户分配独立的 `principal_id` 与专属 Bank（记忆空间固定为 `default`），跨账号不互通、不合并。

同一服务搬迁地址时通过递增 `service_address_revision` 沿用原有身份与队列；若切换为另一套 Hindsight，须使用新的 `service_id`（旧队列不会转发给新服务）。

各平台的来源与机器人身份校验模块：
- `core/onebot_source.py`：核对 AstrBot 原生事件、适配器、客户端和 `self_id`。反向 WebSocket 挂载多个连接且未指定目标时，因无法唯一识别登录账号而暂停记忆。
- `core/qq_source.py`：WS 与 Webhook 共用逻辑类型 `qq_official`；WS 核对机器人身份，Webhook 核对登录用户及 API/HTTP 客户端归属。客户端更换后可重绑同账号登录，旧客户端残留事件直接丢弃；同一客户端登录账号变化时暂停采集。
- `core/weixin_source.py`：使用登录 `account_id`、`to_user_id`、`from_user_id`、`message_id`（uint64）、毫秒时间戳及状态（`NEW=0` / `FINISH=2`）核对。同账号重登或刷新 Token 延续原范围，换号则旧绑定作废。
- `core/telegram_source.py`：以数字 Bot ID、数字 User ID、私聊 `chat_id` 及事件携带的客户端核对来源；断线重连后旧客户端事件不会写入新客户端范围。
- `core/lark_source.py`：由 API 域名、App ID 与机器人 OpenID 组成稳定身份，核对 `p2p` 私聊、未经编辑的原消息与事件客户端归属。

公共边界由 `core/source.py` 收口：原始私聊与接收者验证后按段提取正文，保留 QQ / Telegram 可读表情标记，混入媒体转为极简占位；纯媒体、群聊、过期事件和命令不入队。各适配器继续核对原始类型、客户端、登录账号与平台时间。`Incoming.text` 为记忆正文，`host_text` 为经核验的宿主可见文本。

授权绑定、`source_id` 映射、命令防重记录、时间下限和队列均持久化在 `bridge.sqlite3`，通过同一连接与嵌套 SAVEPOINT 事务提交。机器人身份暂未确认时只在内存暂停该来源；相同账号恢复不撤销既有授权，已确认换号才使旧授权失效。

引用、链接及私聊提及默认正常保存，旧 `recall_on_quoted_or_link` 配置兼容忽略。引用只读取原始消息 ID，在同私聊、授权与 Bank 代次的本地窗口反查，最多附 200 字引用背景；宿主 Reply 正文不可信，查无原消息则仅保存新正文。关闭 `capture_assistant` 时不附被引用助手正文。媒体标记仅表示附件存在，Retain 提示禁止臆测其内容。

AstrBot 4.29.x 的公开发送钩子不提供助手原生消息 ID，OneBot 的适配器发送方法也不返回发送回执；此类助手引用不能建立 ID 到本地正文的可靠映射，只保存用户新话，不猜测引用对象或包装宿主发送方法。

## 用户消息与助手正文

入站正文需关联标准事件、原始载荷与宿主 LLM 请求。以 `host_text` 对照持久历史的文本段，允许平台核验过的多模态块；AstrBot 的时间、身份和群名提醒作为独立块处理，不匹配的用户轮次拒绝关联。

助手正文在主 Agent 完成时从 `conversation_manager` 的持久化 `history` 读取，并通过 `history_baselines`（记录前置长度与前缀摘要，以 `task_id` 关联窗口）确认归属：只有前置读取成功、历史正常增长且新增部分包含对应的用户轮次与最终纯文本回复时，才以 `role=assistant`、`delivery=unknown`、`time_source=history_observed_at` 独立入队。流式/分段发送、系统提示、思考过程（`reasoning`）、中间工具调用以及临时注入的记忆文本均不直接作为对话材料，历史若发生编辑、压缩或截断导致基线失效则拒绝补录。

## 材料构造与队列

- **文档格式**：纯用户材料采用 `user_window_v1`（沿用 `uw1` 身份）；带助手语境或助手正文采用 `dialogue_window_v2`（助手采用独立 `aw1` 与 operation UUID），单条正文上限 8,000 字符。
- **短期语境窗口**：同一账号、代次和会话在最近 30 分钟内最多保留 4 个已核对用户轮次及对应回复（上限 4,000 字符，超限整轮淘汰），帮助解决“第二个”等指代消歧；旧版已冻结的 `uw1` 任务不会事后补入新助手正文。
- **队列与持久化**：本地未完成队列默认上限为 10,000 条 / 64 MiB 正文。入队原子写入身份、正文、摘要与 UUID；重试沿用冻结内容和 UUID。任务完成后清除正文、保留去重记录。「插件已保存」统计有效作用域的 `state='done'`（用户与助手各 1 条），不是远端文档或记忆数。`core/bank_stats.py` 按连接、Bank、代次单飞刷新原生总量，缓存 60 秒，迟到响应作废，不阻塞页面轮询。

## 召回、参考记录与官方交互

自动召回严格限定在当前用户的单一 Bank，结果仅临时注入当轮 LLM 请求，不污染持久会话历史。

- 新装默认 `recall_timeout` 为 15 秒（范围 0.2–30 秒），统一截止时间覆盖查询、认证与两支并发读取，并预留组装交付余量；已有显式设置不改。默认上下文上限 4,000 字符。
- 召回超时、无匹配或服务断连时只附加简短状态说明（或保持静默），不阻塞普通聊天；各用户的最近检索结果与连续失败次数在内存中按作用域独立统计，服务恢复检查完成前暂缓向远端提交队列。
- `core/reference.py` 将每轮真实交付保存到独立、上限 64 MiB 的 `references.sqlite3`，绑定原控制身份、连接、用户、授权修订和 Bank 代次。最近检索内存为空时从最新有效记录区分交付、仅知识页、未调工具、无匹配和失败；停用记录或不可读不伪造结果。`skips` 每用户保留最近 20 条时间、原因和媒体类型，不存原话；唯一入站连续跳过 5 条触发提醒，新消息成功入队即重置。设 0 轮会清空并停止使用与跳过记录。
- 私聊命令组 `/hb` 只提供 `help`、`status`、`on`、`readonly`、`pause`、`forget` 六个命令。查看交付内容、选页和重试使用插件管理页；浏览、查找、修改和导出记忆使用 Hindsight Control Plane。

## 知识页（Knowledge Page）

`core/background.py` 在主库 `settings` 中按连接与用户保存格式 2 有序选页（最多 8 个）、授权修订、Bank/代次及启用开关，不缓存正文。旧格式 1 只在启动迁移边界规范化，HTTP 统一使用 `page_ids`。

- **预算与并发**：请求标记、提示、知识页与细节共用最终预算（默认 1200 token / 4000 字符）；知识页独立上限默认 400 token。认证后知识页与 Recall 并发，页面最多 4 路；知识页默认 3 秒，仅受总截止时间约束，不再除以 4。一支超时不丢弃另一支及时返回的内容。
- **整页装载**：按用户排列顺序逐篇尝试放入，单篇超限则跳过并继续尝试下一篇，绝不截断正文。拉取时调用单模型 `detail=content` 接口（该接口返回 `is_stale=null`，因此目录的 `index_stale` 标记仅作为独立元数据展示，不代表正文是否已刷新完成）。
- **防过期守卫**：请求前冻结当前配置、Bank、代次与选项修订，返回后和注入前再次比对，一旦用户切换、撤权、遗忘或修改设置即丢弃在途旧结果。

## 按需读取与 Agent 工具

`core/agent_tools.py` 通过 `Context.add_llm_tools` 注册读取与 Reflect 工具（参数中不包含 Bank、用户、URL 或密钥，读取范围完全由当前请求上下文绑定）：

- `reading:` 设置按连接与用户保存在主库，默认 `automatic`；切换为 `tools` 仅关闭自动 Recall，不影响消息保存与自动知识页。
- 每条入站事件拥有独立的工具账本：跨续轮共享最多 4 次调用，单次输出限 `recall_max_tokens` / `context_max_chars` / 8 KiB，累计输出限 `2 * recall_max_tokens` / 16 KiB（预留 32 token / 128 字节提示空间）。单次工具超时 3 秒（且不超过 `recall_timeout`），主 Agent 结束后关闭账本（最长存活 120 秒）。
- 主模型及所有备用候选须显式支持工具，会话未禁用工具且未发生子代理交接。`RequestBinding` 直接绑定请求、原工具集、事件及主运行上下文，兼容 `full` / `skills_like`；`on_agent_begin` 通过真实文本与随机标识认领，独立/子 Agent 不能占额度或提前关闭主轮次。主轮结束、失效或 120 秒到期统一释放绑定。
- `core/reading.py` 对官方树、页、模型和历史接口的流式读取限流 256 KiB；长页按行续读并校验内容摘要与刷新时间，避免拼接错乱。工具调用与返回可进入 AstrBot 自身日志，Bridge 不将工具消息本身再次入队。

## Reflect 与执行终点

`core/reflection.py` 按连接与用户保存 `reflect:` 开关与等待时长（默认关闭，等待 3–30 秒，默认 12 秒）。每次触发都会固定使用 low budget 与受限输出额度向 Hindsight 发起一次综合推理（每轮最多 1 次，计入上述 4 次工具配额）。

Reflect 请求在内存中追踪，传输最长等待 120 秒，聊天按用户设置继续。断网和超时不阻塞后续请求、解除暂停或清空；迟到结果经身份、代次与请求归属复核丢弃。主库保留 `reflection_requests`，启动归档历史 `prepared/dispatched/unknown` 为 `overridden`，不丢原请求 ID 和错误。

## 暂停与 Bank 协调

- **读写暂停**：按受影响 Bank 持久化在主库，暂停期间停止新采集、召回注入与队列提交，同时追踪在途任务。未知父子状态继续核对；被整合替代的 `cancelled` 操作通过 `completed_at` 与子任务状态确认收尾。
- **Bank 缺失**：已创建的 Bank 若在 Hindsight 侧被整库删除，Bridge 会报 `bank_missing` 并停止读写（禁止走首次建库流程自动重建），需从备份恢复后点「核对记忆库」，或直接「清空记忆」。
- **完整遗忘**：主库同事务递增代次、登记远端清理引用、作废旧队列正文。分页核查全部旧操作与文档后，独立记录库的 `turns`、`deliveries`、`skips` 也须删净才标 `verified`。记录库锁定时接受清空、继续重试，不阻塞其他用户入队。

## 数据格式与恢复

主库 `bridge.sqlite3` 使用格式 8、WAL 与 `synchronous=FULL`，`scopes` 是身份和授权的投影视图。使用记录独立存放，避免记录故障拖停主队列。

- **一次迁移**：`core/storage.py` 仅在启动接受 0.5.0 业务格式 6 / 控制格式 7，锁定并校验双库、完整备份到受限 `legacy-v0.5-backup/`、单事务导入临时库、核验后原子切换。身份、操作 ID、顺序与历史绑定保持；单侧缺失、错配或空间不足拒绝空库启动。迁移标记阻止重复导入。
- **旧表交接**：若从极早期版本升级时业务库仍含非空 `corrections` 表，启动会报 `legacy_corrections_require_handover` 阻塞启动，保留原数据等待管理员手工迁入 Hindsight。
- **恢复核心规则**：整库快照恢复所有状态到备份时刻，不再拼接旧业务与新控制；若 Bank 已删，`bank_missing` 保护拒绝重建。首次启用前可停机完整还原旧双目录；稳定后删除迁移备份中的旧聊天副本。操作见 [备份与恢复](DATA-MAINTENANCE.md)。

## 管理视图与配套安装

- **前端视图**：`views/memory/` 使用原生 ES Modules 编写（分 `state`、`dom`、`setup`、`maintenance`、`memories`、`app` 六个模块），通过 AstrBot 插件视图 bridge 调用 `core/panel.py` 及其拆分的 `panel_setup.py`、`panel_upgrade.py`、`panel_scopes.py` 接口，直接继承 AstrBot Dashboard 的权限体系。
- **路由与版本**：统一入口校验登录，路由上下文协调锁、资源租约与超时。`config_revision` 保护配置，`scope_revision` 保护身份与代次，各卡片独立 `revision` 防同卡覆盖，跨卡保存互不冲突；所有作用域写操作拒绝旧页提交。
- **配套部署**：Linux 下 `core/installer.py` 生成基于固定模板的 Docker Compose 安装脚本（含 Hindsight 1.2.0、PostgreSQL 17 和对应版本 Control Plane），自动探测 AstrBot 的容器网络模式并将实际地址写入 `managed-network.json`。容器内由 `deploy/managed_service.py` 在 `8766` 端口提供仅限本容器的鉴权配置服务（不挂载 Docker socket），负责探测并验证 AstrBot 模型、写入 `models.json` 以及管理 Hindsight 子进程启停。
- **范围隔离**：连接到已有 Hindsight 时，Bridge 严格关闭所有配套服务管理入口，只操作插件自身配置和对应 Bank 的记忆业务；卸载配套服务时，脚本会严格校验安装 ID 与组件标签后再清理专属容器、网络、数据卷和安装目录，不动宿主共享网络和镜像。

配套页面交互与生命周期细节见 [安装与管理设计](SETUP-DESIGN.md)。
