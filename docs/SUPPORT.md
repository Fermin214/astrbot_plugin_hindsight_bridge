# 兼容范围

安装步骤见 [安装指南](INSTALL.md)，日常操作见 [使用指南](USAGE.md)。

## 版本与平台

| 项目 | 适用范围 |
| --- | --- |
| AstrBot | **4.29.1 及以上**，使用内置执行器 `local` |
| Hindsight | 自托管 **1.1.0 / 1.2.0**，启用 API Key 认证，官方管理面板与服务版本一致 |
| QQ 接入 | OneBot v11（`aiocqhttp`）、官方 WS（`qq_official`）、官方 Webhook（`qq_official_webhook`） |
| 微信接入 | 原生 ClawBot（`weixin_oc`），配置与消息范围见 [微信接入](WEIXIN.md) |
| 对话类型 | OneBot、微信 ClawBot 私聊纯文字；官方 QQ 机器人 C2C 私聊纯文字，不含频道私信、群聊或主动推送 |
| 部署方式 | 默认同一台 Linux 服务器；AstrBot 原生运行或使用 Docker 自定义 bridge、默认 bridge、host 网络，Hindsight 运行于 Linux Docker |
| 管理访问 | 官方面板使用独立 HTTPS 地址；无域名时通过 SSH 隧道访问 |

### QQ 官方入口

两种入口均由 AstrBot 管理登录、事件接收和消息发送。AppID、AppSecret、事件订阅和 Webhook 回调地址在 AstrBot 与 QQ 开放平台配置；Bridge 使用已有机器人实例和私聊 `/sid` 返回的用户 OpenID。

| 项目 | 官方 WS | 官方 Webhook |
| --- | --- | --- |
| 宿主类型 | `qq_official` | `qq_official_webhook` |
| 私聊文字与跨会话回忆 | 支持 | 支持 |
| 正常、只读与停用记忆 | 支持 | 支持 |
| 普通、流式和工具调用后的最终回复 | 支持 | 支持 |
| 记忆查看、搜索和 JSON 导出 | 支持 | 支持 |
| 同实例切换连接方式 | 保留已有记忆与授权 | 保留已有记忆与授权 |

官方 QQ 联调环境为 AstrBot 4.29.4、qq-botpy 1.2.1；4.29.1 已完成宿主接口兼容检查。

Hindsight 1.2.0 的安装、1.1.0 旧库升级、知识页、暂停/遗忘和重载已在 AstrBot 4.29.1 / 4.29.4 与 PostgreSQL 17 的独立环境验证，模型协议覆盖两版 Hindsight。此处的宿主验证使用合成平台端；既有真实 QQ 账号验收使用 Hindsight 1.1.0。

切换连接方式时，保持原平台实例、AppID 和使用者 OpenID。更换实例或机器人账号后，记忆按新账号分别保存，需在用户与记忆中确认机器人身份。

列表与详情提供内容预览，完整内容可通过 JSON 或官方 Control Plane 导出。QQ 文件发送受平台权限和文件大小限制。连接与发送问题的处理见 [安装指南](INSTALL.md#官方机器人没有回复)。

## 安装与管理

插件详情中的「记忆管理」分为「用户与记忆」与「插件设置」：配置连接、添加使用者、调整记忆模式、查看保存进度和处理错误。查看、更正、删除及导入导出记忆，使用独立的 Hindsight 官方面板。

配套安装需要 Docker Engine 和 Compose v2，会安装记忆服务和官方管理面板。已有 Hindsight 可直接填写 API 地址与连接密钥。官方面板通过域名反向代理或 SSH 隧道访问，地址和登录密钥与 API 连接分开配置，见 [部署说明](../deploy/README.md)。

### 模型供应商复用

| 用途 | 支持范围 |
| --- | --- |
| 对话 | OpenAI Chat Completions、OpenAI Responses、Azure OpenAI Chat Completions、Anthropic Messages、Gemini API |
| 嵌入 | OpenAI Embeddings、DashScope Text Embedding、Gemini Embedding、Ollama Embed |
| 重排 | DashScope Rerank、Cohere Rerank 及兼容接口、Hugging Face TEI；可不选 |

配套服务可使用 AstrBot 已配置的模型，无需重复填写地址和密钥。

可沿用供应商的 HTTP(S) 地址、代理、额外请求头和请求参数。与记忆处理冲突的参数会在保存时提示修改。

选择嵌入模型时会检查实际向量维数。多模态嵌入、未适配的供应商和含用户名密码的代理地址暂不支持，模型列表会说明原因。

连接已有 Hindsight 时，模型由该服务管理员配置，插件中的模型选择只用于配套服务。操作步骤见 [安装指南](INSTALL.md)。

## 哪些内容会自动记忆

| 内容 | 处理方式 |
| --- | --- |
| 启用账号发来的普通私聊文字 | 自动记录 |
| AstrBot 保存到会话历史中的机器人最终文字回复 | 自动记录，并区分机器人与用户的陈述 |
| 切换或新建会话后的聊天 | 使用同一用户已有的记忆 |
| 安装前、暂停期间的聊天记录 | 不自动导入或补录 |
| 群聊、图片、语音、引用、转发 | 不自动记录 |
| 以 `/` 开头的命令 | 不自动记录 |
| 工具输出、思考过程、未保存的回复草稿 | 不作为对话正文记录 |
| 聊天消息的编辑与撤回 | 不自动同步为记忆修改或删除 |

## 其他执行器与平台

Dify、Coze、百炼、DeerFlow 等执行器，以及其他聊天平台和 AstrBot WebChat，没有适配自动记录与记忆注入。

QQ 文件导出使用 AstrBot 的文件发送功能。AstrBot 和 OneBot 程序分开运行时，需要配置文件回调地址或共享路径，见 [导出文件失败的处理](INSTALL.md#导出-json-文件失败)。

## 数量与长度

| 项目 | 限制 |
| --- | --- |
| 单条新对话文字 | 最多 8,000 个字符 |
| 每次自动提供给模型的记忆 | 最多 4,000 个字符，查询默认等待 10 秒，可设为 0.2 到 30 秒 |
| 本地尚未完成的记录 | 默认最多 10,000 条，请求正文总量最多 64 MiB |
| 单次 JSON 导出 | 最多 10,000 条记忆、8 MiB |

自动查询超时后继续普通聊天。队列达到上限时，暂时停止接收新记录，已入队的内容继续处理；错误提示可从 `/hindsight status` 查看。

## 记忆管理

内容管理直接使用官方 Control Plane，打开面板不改变模式。完整清空使用插件入口同步作废来源和旧待办。步骤见 [使用指南](USAGE.md#在官方面板修改记忆)。

删除整个记忆库后，该用户的记忆功能会暂停。需要恢复原库，或清空旧记忆重新开始，具体操作见 [备份与恢复](DATA-MAINTENANCE.md#异常提示)。

## 相关文档

消息来源校验、机器人回复关联、发送接口限制和数据格式见 [实现说明](ARCHITECTURE.md)。备份与恢复操作见 [备份与恢复](DATA-MAINTENANCE.md)。
