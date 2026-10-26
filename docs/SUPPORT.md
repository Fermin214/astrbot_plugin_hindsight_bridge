# 兼容范围

安装步骤见 [安装指南](INSTALL.md)，日常操作见 [使用指南](USAGE.md)。

## 版本与平台

| 项目 | 适用范围 |
| --- | --- |
| AstrBot | **4.29.1**，使用内置执行器 `local` |
| Hindsight | **1.1.0**，开启 API Key 认证 |
| QQ 接入 | **OneBot v11**，AstrBot 中的平台类型为 `aiocqhttp` |
| 对话类型 | QQ 私聊纯文字 |
| 部署方式 | Linux 原生 AstrBot，或使用桥接网络的 Docker AstrBot；Hindsight 运行于 Linux Docker |

首发面向 Linux。Windows 原生运行、Windows AstrBot 连接 Linux Hindsight 及其他 Windows 部署方式安排在后续版本。Docker Desktop 仅用于开发和 Linux 容器验证，不作为用户部署方式。

## 安装与管理

记忆管理视图位于 AstrBot 插件详情内，分为「记忆管理」与「设置」两个标签。配套 Linux 安装命令自动处理密钥、服务和网络，主机需已安装 Docker Engine 与 Compose v2，执行账号需能管理 Docker。已有 Hindsight 可直接填写地址与 Key 连接。

### 模型供应商复用

| 用途 | 支持范围 |
| --- | --- |
| 对话 | OpenAI Chat Completions、OpenAI Responses、Azure OpenAI、Anthropic Messages、Google Gemini |
| 嵌入 | OpenAI Embeddings、百炼 DashScope 原生文本嵌入、Google Gemini、Ollama `/api/embed` |
| 重排 | 百炼原生及兼容接口、Cohere 格式接口（含硅基流动、Jina、vLLM）、Hugging Face TEI；也可不选，使用 RRF |

供应商地址与 Key 由服务端读取。DeepSeek 官方的 Chat Completions 和 Responses 配置均可复用，包括 `temperature` 等额外请求参数。OpenRouter、Groq、智谱、小米等 AstrBot 内置 OpenAI 兼容适配器也可选择。

支持自定义 HTTP(S) 地址、HTTP(S) 代理、额外请求头和请求参数。Azure 沿用部署名称、`api-version` 和 API Key；Claude 与 Gemini 沿用思考参数。覆盖模型、输入、工具或结构化输出的参数会给出冲突提示，避免影响记忆抽取。

百炼、Gemini、Ollama 等原生接口通过随配套服务安装的内部转换服务接入，不需要重新添加供应商。转换服务不开放主机端口，按所选供应商分别调用原来的地址，不经过 Magpie 或其他模型网关；只有所选供应商本身配置了网关时才使用它。Gemini 嵌入不额外归一化；OpenAI 嵌入遵循 AstrBot 的维度发送设置，存储维数另行检查。

设置向导会检查实际嵌入向量并读取或自动检测维数。多模态嵌入、未实现的第三方适配器，以及含用户名密码的代理地址不在支持范围，列表会说明原因，不会换用其他模型。已有 Hindsight 的模型由该服务维护，插件中的模型选择仅用于安装配套服务。

协议检查使用 Hindsight **1.1.0** 的 SDK 和本机模拟接口，覆盖请求路径、参数、认证与响应解析。真实模型链路使用 WorkBuddy DeepSeek 与百炼验证；协议兼容不等于逐个验证所有厂商、模型和代理服务。开发者可在 Hindsight 镜像内运行 `python tools/check-model-protocols.py` 复现协议检查。

详细步骤见 [安装指南](INSTALL.md)，手工维护见 [Linux 部署](../deploy/README.md)。

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

机器人提出的建议会按机器人陈述保存，不会直接当成用户已经同意。机器人说“已完成”的文字也不会当成实际操作成功的凭据。

## 其他执行器与平台

Dify、Coze、百炼、DeerFlow 等执行器，以及 QQ 官方机器人、其他聊天平台和 AstrBot WebChat，没有适配自动记录与记忆注入。

QQ 文件导出使用 AstrBot 的文件发送功能。AstrBot 和 OneBot 程序分开运行时，需要配置文件回调地址或共享路径，见 [导出文件失败的处理](INSTALL.md#导出-json-文件失败)。

## 数量与长度

| 项目 | 限制 |
| --- | --- |
| 单条新对话文字 | 最多 8,000 个字符 |
| 单次更正 | 最多 1,000 个字符 |
| 每次自动提供给模型的记忆 | 最多 4,000 个字符，查询最长等待 4 秒 |
| 本地尚未完成的记录 | 默认最多 10,000 条，请求正文总量最多 64 MiB |
| 单次 JSON 导出 | 最多 10,000 条记忆、8 MiB |

自动查询超时后继续普通聊天。队列达到上限时，暂时停止接收新记录，已入队的内容继续处理；错误提示可从 `/hindsight status` 查看。

## 开发参考

消息来源校验、机器人回复关联、发送接口限制和数据格式见 [实现说明](ARCHITECTURE.md)。备份与恢复操作见 [数据维护](DATA-MAINTENANCE.md)。
