# Hindsight Bridge

[简体中文](README.md) | [English](README.en.md)

连接自托管 Hindsight，让 AstrBot 在新会话中也能记得你。

正常聊天，插件就会把 QQ、微信 ClawBot、Telegram 私聊文字交给 [Hindsight](https://github.com/vectorize-io/hindsight) 整理成长期记忆，并在之后的聊天中自动查找相关内容。不同用户的记忆分开保存。

在 AstrBot 插件页配置连接、添加使用者和调整记忆模式；在 Hindsight 官方管理页面（Control Plane）浏览、搜索和管理记忆。可以连接已有 Hindsight，也可以通过安装向导部署配套服务，复用 AstrBot 已配置的模型。

## 安装

支持 **Linux 服务端**、**AstrBot 4.29.1 及以上的内置执行器 `local`** 和 **Hindsight 1.1.0 / 1.2.0**。平台支持 OneBot v11（`aiocqhttp`）、QQ 官方机器人 C2C（WebSocket `qq_official` 与 Webhook `qq_official_webhook`）、微信原生 [ClawBot](docs/WEIXIN.md)（`weixin_oc`）和 [Telegram Bot](docs/TELEGRAM.md)（`telegram`）私聊。可连接已有服务，也可使用配套安装器同机部署。完整限制见 [兼容范围](docs/SUPPORT.md)。

1. 在 AstrBot 打开 **插件管理 → 安装 → 从链接安装**，输入：

   ```text
   https://github.com/Fermin214/astrbot_plugin_hindsight_bridge
   ```

2. 安装后打开 **Hindsight Bridge 插件详情 → 记忆管理 → 插件设置**。
3. 选择安装方式：
   - **配套服务**：生成安装命令，在运行 AstrBot 的 Linux 主机上执行。
   - **已有服务**：填写 Hindsight 1.1.0 或 1.2.0 的 API 地址、API Key 和官方管理地址，点击 **测试服务连接**，再点击 **保存连接**。
4. 使用配套服务时，在设置中选择记忆处理模型与向量模型（可选重排模型），点击 **保存并连接**。
5. 进入 **用户与记忆**，点击 **添加用户** 并选择机器人实例：OneBot 填写 QQ 号；QQ 官方机器人、微信 ClawBot 或 Telegram 填写私聊 `/sid` 返回的用户 UID，核对并确认机器人身份后即可开始聊天。

配套部署的记忆处理模型与向量模型直接复用 AstrBot 已配置的供应商，无需重复填写地址与密钥。若缺少嵌入模型，可在 AstrBot「模型供应商 → 嵌入」中添加。

两种部署的管理分工如下：

| 能力 | 配套部署 | 连接已有服务 |
| --- | --- | --- |
| 记忆存取、知识页选择、用户管理 | 在插件中操作 | 在插件中操作 |
| 记忆预算、超时、查询与队列参数 | 在插件面板中调整 | 在插件面板中调整 |
| 模型配置 | 在插件中选择 AstrBot 模型并应用 | 由已有服务自身维护 |
| 服务备份与升级 | 插件提供备份步骤与升级命令，升级后核对结果 | 由已有服务自身维护 |
| 重新部署与卸载服务 | 插件按安装记录生成专属命令 | 由已有服务自身维护 |

详细步骤见 [安装指南](docs/INSTALL.md)。手工管理后端见 [Linux 部署说明](deploy/README.md)。

## 升级

升级前建议备份插件业务数据与控制数据（详见 [备份与恢复](docs/DATA-MAINTENANCE.md)）。在 AstrBot 插件管理中更新并重载插件；若提示确认账号，核对机器人身份后确认即可。

配套 Hindsight 服务的升级按 [服务升级步骤](docs/DATA-MAINTENANCE.md#升级-hindsight-服务) 操作（新配套安装默认使用 1.2.0，更新插件本体不改变 Hindsight 服务）。使用 QQ 官方机器人时，若在 WebSocket 与 Webhook 之间切换，保持原实例、AppID 和使用者 OpenID 不变即可无缝沿用已有记忆。

## 使用与管理

日常聊天无需发送记忆命令。在 **用户与记忆** 中选择用户，点击 **读写记忆 / 只读记忆 / 停用记忆** 即可切换模式，并可查看已保存、待保存、失败数量及最近检索时间。移除用户后立即撤销访问权限，已有记忆保留。

在 **插件设置 → 连接与访问** 复制官方管理地址，在浏览器打开 Hindsight 官方 Control Plane。复制 **Bank ID**（按机器人和用户隔离的记忆库），登录后即可浏览、搜索与管理记忆。远程访问与登录密钥的获取方式见 [部署说明](deploy/README.md#五打开官方-control-plane)。

- **聊天知识页**：可选择最多 8 篇 Hindsight 按主题生成的文档，按设定预算让机器人参考完整页面。在官方 Control Plane 中创建与更新，在插件面板中选择与排序，详见 [知识页说明](docs/USAGE.md#可选知识页)。
- **细节记忆**：默认使用 **自动召回**；也可选择 **按需工具**，由 AstrBot Agent 主动调用工具搜索知识页、阅读原文或检索事实，详见 [按需记忆工具](docs/USAGE.md#按需记忆工具)。
- **Reflect**：可在细节记忆中独立开启 Reflect，由 Hindsight 综合记忆回答复杂问题，详见 [Reflect](docs/USAGE.md#reflect)。
- **记忆与知识页参数**：在 **插件设置 → 记忆与知识页** 调整长度、超时、查询 token、分词编码、队列容量与排除前缀。
- **清空记忆**：删除所选记忆库的全部长期记忆、知识页、来源材料与待保存记录。AstrBot 自身的本地聊天记录保持不变。
- **状态恢复**：配套服务升级或异常留下的读写暂停，可通过 **查看暂停状态** 核对并通过后解除。完整步骤见 [使用指南](docs/USAGE.md)。

使用者也可在 QQ、微信 ClawBot 或 Telegram 私聊中通过命令管理记忆：

| 想做什么 | 命令 |
| --- | --- |
| 查看记忆 | `/hindsight view` |
| 查看已选知识页 | `/hindsight background [序号]` |
| 停止记录新内容，继续使用已有记忆 | `/hindsight readonly` |
| 暂停记录和自动使用记忆 | `/hindsight pause` |
| 开启保存和自动使用记忆 | `/hindsight on` |
| 清空自己的记忆 | `/hindsight forget`，再按回复确认 |

如需卸载配套服务，可在 **插件设置 → 服务管理** 生成专属卸载命令，在原主机执行以清理该服务的容器与数据。

## 记忆范围

记录已启用账号的私聊文字，以及 AstrBot 保存到会话历史中的机器人最终文字回复。群聊、图片、语音、引用、转发和命令不自动记录；安装前的聊天记录不自动导入。

新消息会在后台整理，服务暂时不可用时普通聊天仍可继续，待办会在连接恢复后继续处理。对话内容会发送到所连接的 Hindsight 及其模型服务，查看和搜索显示官方服务中的记忆，更正也在官方页面完成。

## 文档

- [安装指南](docs/INSTALL.md)：向导安装与连接已有服务。
- [使用指南](docs/USAGE.md)：面板管理和私聊命令。
- [兼容范围](docs/SUPPORT.md)：版本、供应商、平台与消息类型。
- [备份与恢复](docs/DATA-MAINTENANCE.md)：插件数据备份与恢复、配套服务备份与升级。
- [贡献指南](CONTRIBUTING.md)：源码结构、接口边界和提交要求。

反馈问题请到 [GitHub Issues](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/issues)，附上版本、操作步骤和错误提示。

## 许可

插件使用 [MIT License](LICENSE)。依赖许可见 [NOTICE.md](NOTICE.md)。
