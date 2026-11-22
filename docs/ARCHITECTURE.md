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
- `core/lark_source.py`：由 API 域名、App ID 与机器人 OpenID 组成稳定身份；仅处理 `p2p/text` 且未经编辑的原消息（排除引用、话题、提及、富文本与媒体），同时校验事件携带的客户端归属。

授权绑定、`source_id` 映射、命令防重记录与单调递增的时间下限均持久化在控制库 `control.sqlite3` 中，修改状态时在同一 SQLite 事务内提交控制状态与消费凭据。

若用户在聊天参考开启了 `recall_on_quoted_or_link`（默认关闭），通过了身份、私聊、客户端、时间与正文校验的引用回复（以及 Telegram 带链接/`@` 提及文字）会生成 `capture_allowed=False` 的 `Incoming`：正常进行召回、知识页注入和工具调用并计入使用记录，但不写入 `events`、`outbox`、`window`，也不补录助手回复。

## 用户消息与助手正文

入站用户原文需关联标准接收事件、原始载荷与宿主 LLM 请求；AstrBot 追加的时间、身份和群名提醒会被识别为独立的 `system_reminder` 块，消息首块仍须与入站原文严格一致，不一致时拒绝关联。

助手正文在主 Agent 完成时从 `conversation_manager` 的持久化 `history` 读取，并通过 `history_baselines`（记录前置长度与前缀摘要，以 `task_id` 关联窗口）确认归属：只有前置读取成功、历史正常增长且新增部分包含对应的用户轮次与最终纯文本回复时，才以 `role=assistant`、`delivery=unknown`、`time_source=history_observed_at` 独立入队。流式/分段发送、系统提示、思考过程（`reasoning`）、中间工具调用以及临时注入的记忆文本均不直接作为对话材料，历史若发生编辑、压缩或截断导致基线失效则拒绝补录。

## 材料构造与队列

- **文档格式**：纯用户材料采用 `user_window_v1`（沿用 `uw1` 身份）；带助手语境或助手正文采用 `dialogue_window_v2`（助手采用独立 `aw1` 与 operation UUID），单条正文上限 8,000 字符。
- **短期语境窗口**：同一账号、代次和会话在最近 30 分钟内最多保留 4 个已核对用户轮次及对应回复（上限 4,000 字符，超限整轮淘汰），帮助解决“第二个”等指代消歧；旧版已冻结的 `uw1` 任务不会事后补入新助手正文。
- **队列与持久化**：本地未完成队列默认上限为 10,000 条 / 64 MiB 正文。入队时原子写入身份、正文、摘要与 UUID；用户任务在自动召回结束后放行，重试始终沿用原冻结内容和 UUID。任务完成后清除正文，保留去重记录；面板中的「已保存」计数即统计当前有效作用域下 `state='done'` 的记录数（用户与助手各计 1 条）。

## 召回、参考记录与官方交互

自动召回严格限定在当前用户的单一 Bank，结果仅临时注入当轮 LLM 请求，不污染持久会话历史。

- 默认 `recall_timeout` 为 10 秒（范围 0.2–30 秒），覆盖查询构建、认证与检索；默认上下文上限 4,000 字符。
- 召回超时、无匹配或服务断连时只附加简短状态说明（或保持静默），不阻塞普通聊天；各用户的最近检索结果与连续失败次数在内存中按作用域独立统计，服务恢复检查完成前暂缓向远端提交队列。
- `core/reference.py` 管理按用户保存的参考设置，并将每轮真正写入请求的文本及工具返回值快照保存在业务目录的 `references.sqlite3`（按控制库身份、连接、用户、授权修订和 Bank 代次隔离；写入失败时跳过快照而不中断聊天）。
- 私聊只提供 `help`、`status`、`on`、`readonly`、`pause`、`forget` 六个命令。查看交付内容、选页和重试使用插件管理页；浏览、查找、修改和导出记忆使用 Hindsight Control Plane。

## 知识页（Knowledge Page）

`core/background.py` 在控制库 `settings` 中按连接与用户保存已选页面有序 ID 列表（格式 2 最多存 8 个，格式 1 为兼容单页读取）、授权修订、Bank/代次及启用开关，**不缓存页面正文**；页面内容始终在需要时向 Hindsight 实时拉取。

- **预算与并发**：读取前先校验页面预算（`background_max_tokens`，默认 400，64–8192）和共享预算（`recall_max_tokens` 默认 1200，`context_max_chars` 默认 4000）。认证后知识页与 Recall 并发执行，最多 4 路并发拉取；知识页共享 `background_timeout`（默认 1 秒，不超过剩余 `recall_timeout` 的四分之一）。
- **整页装载**：按用户排列顺序逐篇尝试放入，单篇超限则跳过并继续尝试下一篇，绝不截断正文。拉取时调用单模型 `detail=content` 接口（该接口返回 `is_stale=null`，因此目录的 `index_stale` 标记仅作为独立元数据展示，不代表正文是否已刷新完成）。
- **防过期守卫**：请求前冻结当前配置、Bank、代次与选项修订，返回后和注入前再次比对，一旦用户切换、撤权、遗忘或修改设置即丢弃在途旧结果。

## 按需读取与 Agent 工具

`core/agent_tools.py` 通过 `Context.add_llm_tools` 注册读取与 Reflect 工具（参数中不包含 Bank、用户、URL 或密钥，读取范围完全由当前请求上下文绑定）：

- `reading:` 设置按连接与用户保存在控制库，默认 `automatic`；切换为 `tools` 仅关闭自动 Recall，不影响消息保存与自动知识页。
- 每条入站事件拥有独立的工具账本：跨续轮共享最多 4 次调用，单次输出限 `recall_max_tokens` / `context_max_chars` / 8 KiB，累计输出限 `2 * recall_max_tokens` / 16 KiB（预留 32 token / 128 字节提示空间）。单次工具超时 3 秒（且不超过 `recall_timeout`），主 Agent 结束后关闭账本（最长存活 120 秒）。
- 仅在模型支持工具调用、会话未禁用记忆工具且未配置回退模型（公开上下文无法可靠识别回退后的模型能力）以及未发生子代理交接时开放按需工具；`on_agent_begin` 通过请求文本与临时随机标识精确绑定账本，避免独立/子 Agent 错领额度。
- `core/reading.py` 对官方树、页、模型和历史接口的流式读取限流 256 KiB；长页按行续读并校验内容摘要与刷新时间，避免拼接错乱。工具调用与返回可进入 AstrBot 自身日志，Bridge 不将工具消息本身再次入队。

## Reflect 与执行终点

`core/reflection.py` 按连接与用户保存 `reflect:` 开关与等待时长（默认关闭，等待 3–30 秒，默认 12 秒）。每次触发都会固定使用 low budget 与受限输出额度向 Hindsight 发起一次综合推理（每轮最多 1 次，计入上述 4 次工具配额）。

由于 Hindsight 1.1.0 / 1.2.0 缺少查询单个 Reflect 请求是否完成的公开接口，Bridge 从控制库格式 7 起新增 `reflection_requests` 表来保证一致性：

1. HTTP 发送前先记录 `prepared`，紧接着与范围复核同事务推进为 `dispatched`。
2. 请求传输线程最长跟踪 120 秒（即使前端工具已因超时返回给聊天，后台仍继续等待结果以确认终点，晚到正文不再回填聊天）。
3. 拿到明确响应或确定未发出时结束等待；若因进程重启、断网或超时无法确认终点，状态转为 `unknown`。
4. 处于 `unknown` 状态时，对应 Bank 的后续 Reflect、解除维护暂停和清空删库都会阻塞等待；直到管理员确认 Hindsight 已重启或无残留任务后，在面板手动解除阻塞将其标记为 `overridden`（保留原记录、操作人与时间，不重放请求）。

## 暂停与 Bank 协调

- **读写暂停**：按受影响 Bank 持久化在控制库，暂停期间停止新采集、召回注入与队列提交，同时继续追踪已发往后端的在途任务。Hindsight 1.2.0 中被新整合替代的操作可能标记为 `cancelled`；恢复前会通过单项接口复核是否已带 `completed_at` 且无子任务，确认收尾才放行。
- **Bank 缺失**：已创建的 Bank 若在 Hindsight 侧被整库删除，Bridge 会报 `bank_missing` 并停止读写（禁止走首次建库流程自动重建），需从备份恢复后点「核对记忆库」，或直接「清空记忆」。
- **完整遗忘**：清空流程记录独立控制日志并递增 Bank 代次，先阻塞新写入、停止旧作用域请求、等待遗忘清理前的 Reflect 任务结束，再分页（每批最多 4 路并发、45 秒超时）核查全部已知操作与文档均已删除，最后才标记清理完成。

## 数据格式与恢复

Bridge 数据分为两库：业务库 `runtime.sqlite3`（窗口、队列、去重）与控制库 `control.sqlite3`（身份、Bank、代次、模式、暂停、删除日志、防重凭据与格式 7 的 Reflect 请求）。

- **格式演进**：格式 5 引入了授权修订、命令摘要防重与宿主接收时间高水位（基于平台原始时间与 AstrBot `created_at` 校验，不受本地时钟回拨影响）；格式 6 兼容至 0.3.x；**0.4.0 升级到格式 7**，旧版插件会拒绝打开格式 7。
- **旧表交接**：若从极早期版本升级时业务库仍含非空 `corrections` 表，启动会报 `legacy_corrections_require_handover` 阻塞启动，保留原数据等待管理员手工迁入 Hindsight。
- **恢复核心规则**：控制库记录了不可逆的清空操作、账号授权下限和 Reflect 解阻证据，因此任何情况下恢复旧业务快照都必须配对最新的控制库，不得用旧备份回滚控制状态。

## 管理视图与配套安装

- **前端视图**：`views/memory/` 使用原生 ES Modules 编写（分 `state`、`dom`、`setup`、`maintenance`、`memories`、`app` 六个模块），通过 AstrBot 插件视图 bridge 调用 `core/panel.py` 及其拆分的 `panel_setup.py`、`panel_upgrade.py`、`panel_scopes.py` 接口，直接继承 AstrBot Dashboard 的权限体系。
- **配套部署**：Linux 下 `core/installer.py` 生成基于固定模板的 Docker Compose 安装脚本（含 Hindsight 1.2.0、PostgreSQL 17 和对应版本 Control Plane），自动探测 AstrBot 的容器网络模式并将实际地址写入 `managed-network.json`。容器内由 `deploy/managed_service.py` 在 `8766` 端口提供仅限本容器的鉴权配置服务（不挂载 Docker socket），负责探测并验证 AstrBot 模型、写入 `models.json` 以及管理 Hindsight 子进程启停。
- **范围隔离**：连接到已有 Hindsight 时，Bridge 严格关闭所有配套服务管理入口，只操作插件自身配置和对应 Bank 的记忆业务；卸载配套服务时，脚本会严格校验安装 ID 与组件标签后再清理专属容器、网络、数据卷和安装目录，不动宿主共享网络和镜像。

配套页面交互与生命周期细节见 [安装与管理设计](SETUP-DESIGN.md)。
