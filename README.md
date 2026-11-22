# Hindsight Bridge

[简体中文](README.md) | [English](README.en.md)

连接 AstrBot 与自托管 [Hindsight](https://github.com/vectorize-io/hindsight)，让机器人在新会话里继续记得你的私聊对话。

日常聊天时，插件会自动提取 QQ、微信 ClawBot、Telegram 和飞书的私聊文字，交由 Hindsight 整理成长期记忆，并在后续对话中按语义召回。每位用户拥有独立的记忆库（Bank），彼此隔离。

## 核心特性

- **自动沉淀与召回**：正常私聊即可保存对话与回复；后续聊天支持**自动召回**，也可切为 **Agent 按需工具**按需搜索。
- **知识页与综合推理**：支持在对话中按顺序附带最多 8 篇 Hindsight 知识页（Knowledge Pages），并可按用户开启 Reflect 综合推理。
- **精细控制与可追溯**：每位用户可单独切换「读写 / 只读 / 停用」模式、自定义记忆提示、选择引用消息只读召回，并在面板查看每轮交付给模型的原文记录。
- **多平台与独立隔离**：支持 OneBot v11、QQ 官方机器人（WS / Webhook）、微信 ClawBot、Telegram、飞书私聊；按机器人账号与用户严格隔离。
- **部署省心、断线不丢数据**：Linux 下可直接生成配套 Docker 安装命令并复用 AstrBot 已配置的模型；消息先入本地队列，Hindsight 短暂不可用时照常聊天，恢复后继续处理。

## 支持环境

| 项目 | 要求 |
| --- | --- |
| AstrBot | **4.29.1 及以上**，使用内置执行器 `local` |
| Hindsight | 自托管 **1.1.0** 或 **1.2.0** |
| 支持平台（私聊） | OneBot v11（`aiocqhttp`）、QQ 官方（`qq_official` / `qq_official_webhook`）、微信 ClawBot（`weixin_oc`）、Telegram（`telegram`）、飞书（`lark`） |
| 部署环境 | Linux（Docker Engine + Compose v2）支持配套一键部署；Windows 及其他环境可连接已有 Hindsight |

完整兼容说明见 [兼容范围](docs/SUPPORT.md)。

## 安装

1. 在 AstrBot 打开 **插件管理 → 安装 → 从链接安装**，填入仓库地址：

   ```text
   https://github.com/Fermin214/astrbot_plugin_hindsight_bridge
   ```

2. 安装完成后，打开 **Hindsight Bridge 插件详情 → 记忆管理 → 插件设置**：
   - **安装配套服务**（推荐 Linux 使用）：点击 **生成安装命令**，在运行 AstrBot 的 Linux 主机终端执行；回到页面在「配套服务模型」选择记忆处理模型与向量模型（重排模型可选），点击 **保存并连接**。
   - **连接已有服务**：填写 Hindsight 1.1.0 或 1.2.0 的 API 地址、API 密钥和官方管理地址，点击 **测试连接**，确认无误后点击 **保存连接**。
3. 切换到 **用户与记忆**，点击 **+ 添加用户**，选择平台实例：OneBot 填用户 QQ 号，QQ 官方、微信 ClawBot、Telegram 或飞书填私聊 `/sid` 返回的 UID。核对后 **确认机器人身份**，即可正常聊天。

配套部署会直接复用 AstrBot 中已配置好的模型供应商，无需重复填写接口地址与密钥（若列表缺少向量模型，请先到 AstrBot「模型供应商 → 嵌入」添加）。

| 管理事项 | 配套部署 | 连接已有服务 |
| --- | --- | --- |
| 用户、记忆模式、知识页与参数 | 插件面板 | 插件面板 |
| 记忆浏览、搜索与内容更正 | 官方 Control Plane | 官方 Control Plane |
| 模型选择与验证 | 插件面板 | 已有服务自行管理 |
| 服务备份、升级、重装与卸载 | 插件面板提供命令与引导 | 已有服务自行管理 |

更多安装细节见 [安装指南](docs/INSTALL.md)、[Windows 原生部署](docs/WINDOWS.md) 和 [Linux 手工部署](deploy/README.md)。

## 升级

> **0.4.0 升级提醒**：升级前请先备份插件业务数据、控制目录与插件配置（详见 [备份与恢复](docs/DATA-MAINTENANCE.md)）。**0.4.0 会自动将控制库迁移至格式 7，保留原有记忆库、配置和请求记录，升级后无法直接降级回旧版插件。**

在 AstrBot 插件管理中更新并重载即可；若页面提示重新确认身份，核对机器人账号后点击确认。更新插件本体不会改动已有的 Hindsight 服务；配套服务的版本升级请按 [服务升级步骤](docs/DATA-MAINTENANCE.md#升级-hindsight-服务) 操作（新配套安装默认使用 1.2.0）。

## 使用与管理

日常聊天不需要输入命令。在插件面板的 **用户与记忆** 页签中，你可以随时：

- **切换记忆模式**：在顶部总览卡直接切换 **读写记忆 / 只读记忆 / 停用记忆**，实时查看已保存、待保存、保存失败数及最近检索结果。
- **配置聊天记忆**：设置细节记忆读取方式（**自动召回** 或 **按需工具**）、自定义记忆提示与检索预算、开关引用/带链接消息只读召回、查看或复制每轮交付给模型的原文记录。
- **管理知识页与 Reflect**：在 **知识页（Knowledge Pages）** 中选择并排序最多 8 篇主题文档，开关随时可切换；在 **Reflect 综合推理** 中按用户开启多记忆综合回答。
- **在官方面板管理内容**：从 **插件设置 → Hindsight 服务连接** 复制管理地址打开 Hindsight 原生 Control Plane，配合用户页的 **Bank ID** 即可浏览、搜索、更正或导出记忆（获取登录密钥见 [部署说明](deploy/README.md#五打开官方-control-plane)）。
- **状态恢复与清空**：遇到服务升级或异常留下的暂停状态时，点击 **查看暂停状态** 核对后恢复；Reflect 超时不阻塞恢复和清空；需要彻底删除数据时点击 **清空记忆**（仅清除插件记忆库，不影响 AstrBot 本地聊天历史）。

常用私聊命令（完整列表与细节见 [使用指南](docs/USAGE.md)）：

| 想做什么 | 私聊命令 |
| --- | --- |
| 查看命令说明 | `/hb help` |
| 查看状态与错误诊断 | `/hb status` |
| 切为只读（停止保存，继续召回） | `/hb readonly` |
| 暂停（停止保存与自动召回） | `/hb pause` |
| 恢复读写 | `/hb on` |
| 清空个人记忆 | `/hb forget`，按提示发送确认码 |

0.5.0 退休 `correct/view/show/export/recall/background/retry/cancel`。交付内容、选页和重试使用插件面板；浏览、检索、修改与导出使用 Hindsight Control Plane。不再提供单项任务私聊取消，清空仍作废积压并删除个人 Bank。

## 记录边界

- **会记录**：已添加用户的普通私聊纯文字，以及 AstrBot 写入会话历史的机器人最终文字回复。
- **不自动记录**：群聊、图片/语音/文件等媒体消息、转发消息、命令（默认 `/` 开头），以及安装插件前的历史聊天（若开启「引用回复或带链接的私聊也检索」，此类消息仅读取已有记忆，不写入新记忆）。
- **隐私与去向**：对话内容会发送到你自行部署或连接的 Hindsight 及对应模型服务；Bridge 不向第三方回传数据。

## 文档

- [安装指南](docs/INSTALL.md)：插件安装、配套部署、已有服务接入与常见问题
- [使用指南](docs/USAGE.md)：面板各项配置、知识页、按需工具、Reflect 与私聊命令
- [兼容范围](docs/SUPPORT.md)：AstrBot/Hindsight 版本、模型供应商与平台限制
- [备份与恢复](docs/DATA-MAINTENANCE.md)：数据备份、故障恢复与配套服务升级
- [贡献指南](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/blob/main/CONTRIBUTING.md)：架构入口、验证命令与提交规范

遇到问题请在 [GitHub Issues](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/issues) 提交反馈，并附上版本、复现步骤与错误提示。

## 许可

基于 [MIT License](LICENSE) 开源，第三方依赖许可见 [NOTICE.md](NOTICE.md)。
