# 兼容范围

安装步骤见 [安装指南](INSTALL.md)，日常操作见 [使用指南](USAGE.md)。

## 版本与平台

| 项目 | 支持范围 |
| --- | --- |
| AstrBot | **4.29.1 及以上**，使用内置执行器 `local` |
| Hindsight | 自托管 **1.1.0** 或 **1.2.0**，需启用 API Key 认证 |
| QQ 接入 | OneBot v11（`aiocqhttp`）、QQ 官方 WebSocket（`qq_official`）、QQ 官方 Webhook（`qq_official_webhook`） |
| 微信接入 | 原生 ClawBot（`weixin_oc`），详见 [微信接入](WEIXIN.md) |
| Telegram 接入 | 原生 Bot（`telegram`），详见 [Telegram 接入](TELEGRAM.md) |
| 飞书接入 | 原生 `lark`（支持长连接和 Webhook），详见 [飞书接入](FEISHU.md) |
| 支持的会话类型 | 上述各平台的普通私聊纯文字；不包含群聊、频道私信或主动推送 |
| 部署方式 | Linux Docker Engine 支持配套一键部署；Windows 11 x64（及任意系统）通过已有服务入口接入，详见 [Windows 原生部署](WINDOWS.md) |
| 官方页面访问 | Hindsight Control Plane 需独立 HTTPS 反向代理或 SSH 隧道 |

### QQ 官方入口说明

WebSocket 与 Webhook 两种方式的登录、验签和收发均由 AstrBot 原生管理，插件共用同一套用户授权与记忆库规则：

- 在同实例、同 AppID 和同用户 OpenID 下切换 WS 与 Webhook，原有记忆和授权自动沿用。
- 更换平台实例或 AppID 后需要重新确认机器人身份，记忆将分开保存。

对于 OneBot，建议为每个机器人单独建一个平台实例。如果多个机器人同时连到一个反向 WebSocket 入口且未指定目标，宿主无法唯一区分当前账号，插件会暂停该实例的记忆存取直到区分明确。

## 安装与管理方式

- **配套部署**（Linux Docker Engine + Compose v2）：由插件生成安装命令部署专属 Hindsight、PostgreSQL 与匹配版本的 Control Plane，并在面板里直接复用 AstrBot 的模型供应商。
- **连接已有服务**（Windows / WSL2 / Docker Desktop / 独立集群）：填写 Hindsight 的 API 地址、API 密钥和浏览器管理地址即可接入，模型、升级与数据库维护由原服务自行负责。

### 模型供应商复用

配套部署支持直接复用以下类型的 AstrBot 供应商：

| 用途 | 支持的接口类型 |
| --- | --- |
| 对话（记忆处理） | OpenAI Chat Completions、OpenAI Responses、Azure OpenAI Chat Completions、Anthropic Messages、Gemini API |
| 嵌入（向量） | OpenAI Embeddings、DashScope Text Embedding、Gemini Embedding、Ollama Embed |
| 重排（可选） | DashScope Rerank、Cohere Rerank 及兼容接口、Hugging Face TEI |

保存时会自动复用供应商的接口地址、代理、请求头与兼容参数，并探测实际向量维数。多模态嵌入、带用户名密码的代理地址或参数与记忆处理冲突的供应商暂不支持，列表里会直接写出原因。

## 哪些内容会被记录

| 消息类型 | 处理方式 |
| --- | --- |
| 已启用用户的普通私聊纯文字 | 自动记录 |
| AstrBot 写入会话历史的机器人最终纯文字回复 | 自动记录，并区分用户与助手角色 |
| 新建或切换会话后的私聊 | 共享同一用户的长期记忆 |
| 安装前、暂停或只读期间的聊天 | 不记录，也不事后补录 |
| 群聊、图片、语音、视频、文件、转发 | 不自动记录 |
| 以 `/` 开头的命令（及自定义排除前缀） | 不自动记录 |
| 工具输出、思考过程、未保存草稿 | 不作为对话正文记录 |
| 消息的编辑与撤回 | 不同步修改或删除已生成记忆 |

对于**引用回复**（支持全部五个逻辑平台）及 **Telegram 的带链接/@ 提及文字**，可在面板「聊天参考与保存」里按用户开启只读召回：开启后这些消息可以读取已有记忆、知识页和按需工具，但**消息本身和该轮助手回复都不保存为新记忆**。

## 其他执行器

Dify、Coze、百炼、DeerFlow 等外部执行器，以及 AstrBot WebChat 和其他未列出平台，暂未接入自动记录与记忆召回。

使用 `/hindsight export` 导出文件时，若 AstrBot 与 OneBot 运行在不同容器，需配好 `callback_api_base` 或共享路径，见 [导出文件失败的处理](INSTALL.md#导出-json-文件失败)。

## 默认额度与限制

| 项目 | 默认值与上限 |
| --- | --- |
| 单条对话材料 | 最多 8,000 个字符 |
| 单次带入模型的记忆 | 默认 1,200 token / 4,000 字符（检索超时默认 10 秒，可在面板调至 0.2–30 秒） |
| 本地待保存队列 | 默认最多 10,000 条、正文总量 64 MiB（超出后暂停收新，排空后恢复） |
| 单次 `/hindsight export` | 最多 10,000 条记忆、文件上限 8 MiB |

## 相关文档

- 浏览、修改或整库清除记忆的操作方法见 [使用指南](USAGE.md#在官方面板修改记忆)。
- 控制库备份、故障恢复与异常排查见 [备份与恢复](DATA-MAINTENANCE.md#异常提示)。
- 底层架构、来源校验与数据格式见 [实现说明](ARCHITECTURE.md)。
