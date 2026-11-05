# Hindsight Bridge

连接自托管 Hindsight，让 AstrBot 在新会话中也能记得你。

正常聊天，插件就会把 QQ 私聊文字交给 [Hindsight](https://github.com/vectorize-io/hindsight) 整理成长期记忆，并在之后的聊天中自动查找相关内容。不同用户的记忆分开保存。

在 AstrBot 插件页配置连接、添加使用者和调整记忆模式；在 Hindsight 官方管理页面（Control Plane）浏览、搜索和管理记忆。可以连接已有 Hindsight，也可以通过安装向导部署配套服务，复用 AstrBot 已配置的模型。

## 安装

支持 **Linux 服务端**、**AstrBot 4.29.1 及以上的内置执行器 `local`**和 **Hindsight 1.1.0 / 1.2.0**。QQ 私聊可使用 OneBot v11（`aiocqhttp`），或官方机器人 C2C 的 WebSocket（`qq_official`）与 Webhook（`qq_official_webhook`）入口。默认在同一台服务器部署 AstrBot 与 Hindsight，可连接已有服务，也可使用配套安装器。版本和消息限制见 [兼容范围](docs/SUPPORT.md)。

1. 在 AstrBot 打开 **插件管理 → 安装 → 从链接安装**，粘贴：

   ```text
   https://github.com/Fermin214/astrbot_plugin_hindsight_bridge
   ```

2. 安装完成后打开 **Hindsight Bridge 插件详情 → 记忆管理 → 设置**。
3. 选择安装方式：生成配套服务安装命令，在运行 AstrBot 的 Linux 主机执行；已有 Hindsight 1.1.0 或 1.2.0 则填写服务 API 地址、API Key 和官方管理地址。
4. 配套服务安装完成后，在设置中选择聊天与嵌入模型，可选重排模型，点击 **验证并保存模型**。
5. 选择 QQ 机器人并添加使用者：OneBot 填 QQ 号，官方机器人填私聊 `/sid` 返回的用户 UID（OpenID）。点击 **保存连接和使用者名单**，再到设置中点击 **确认账号授权**，核对机器人账号并确认。

配套安装的主机需已安装 Docker Engine 和 Compose v2。保存设置并确认授权后，正常聊天即可。

对话与嵌入模型从 AstrBot 供应商列表选择，无需重复填写模型地址和 Key。嵌入模型用于查找相关记忆，与平常聊天的模型用途不同；缺少时，向导会提示去 AstrBot「模型供应商 → 嵌入」添加。重排模型可不选。

详细步骤及常见问题见 [安装指南](docs/INSTALL.md)。手工管理后端见 [Linux 部署说明](deploy/README.md)。

## 从 0.1.x 升级

升级前备份插件数据和 Hindsight 服务，步骤见 [备份与恢复](docs/DATA-MAINTENANCE.md)。在 AstrBot 更新并重载插件；页面提示确认账号时，核对当前机器人后确认即可。

新配套安装使用 Hindsight 1.2.0。已有服务升级按 [服务升级步骤](docs/DATA-MAINTENANCE.md#升级-hindsight-服务) 操作，更新插件后另行生成并执行服务升级命令。

使用 QQ 官方机器人时，在设置中添加私聊 `/sid` 返回的 OpenID。保持原实例、AppID 和使用者不变，在 WebSocket 与 Webhook 之间切换可继续使用原有记忆；连接方式由 AstrBot 和 QQ 开放平台配置。

## 使用与管理

日常聊天无需发送记忆命令。保存并确认账号授权后，在 **记忆与保存 → 用户记忆** 中选择用户，可切换“保存并自动使用记忆 / 只自动使用已有记忆 / 暂停保存和自动使用”，查看保存进度、重试失败记录，或清空此用户的记忆。

在 **查看和管理记忆** 中复制“官方管理地址”和“记忆库 ID”，用浏览器打开官方页面，登录后找到对应 Bank。**Bank 就是每个用户的记忆库**，在首次保存聊天时自动创建。远程访问与登录密钥的获取方式见 [部署说明](deploy/README.md#五打开官方-control-plane)。

可选 **知识页（Knowledge Page）**：选择一份 Hindsight 按主题生成的短文档，让机器人在聊天时参考，例如项目目标与约束。记忆召回（Recall）按问题查找相关记忆，知识页提供选定主题的文档；默认关闭，创建和更新在官方 Control Plane 完成，选页步骤见 [知识页说明](docs/USAGE.md#可选知识页)。

浏览、搜索和导出无需暂停保存和查询。修改或删除记忆前，先在 **此用户的保存与查询** 中暂停保存和查询，等页面显示“可以修改记忆”后再操作，完成后恢复原记忆模式。具体步骤可点击页面上的问号，或查看 [使用指南](docs/USAGE.md#在官方面板修改记忆)。

**清空此用户的记忆** 需要点击 **确认清空记忆**，会删除该用户的全部长期记忆、知识页及其历史、来源材料和待保存记录，无法恢复。清理完成后需重新选择“保存并自动使用记忆”，AstrBot 自身的聊天记录保留。

使用者也可以在自己的 QQ 私聊中发送：

| 想做什么 | 命令 |
| --- | --- |
| 查看记忆 | `/hindsight view` |
| 查看已选知识页 | `/hindsight background` |
| 停止记录新内容，继续使用已有记忆 | `/hindsight readonly` |
| 暂停记录和自动使用记忆 | `/hindsight pause` |
| 开启保存和自动使用记忆 | `/hindsight on` |
| 清空自己的记忆 | `/hindsight forget`，再按回复确认 |

插件页各区域提供用途说明和操作帮助；完整操作见 [使用指南](docs/USAGE.md)。

插件总开关位于 AstrBot 插件面板。卸载配套服务时，在 **设置 → 服务升级与卸载** 生成卸载命令；该操作会删除整套配套服务及其中所有用户的记忆数据。插件本体通过 AstrBot 卸载入口移除。

## 记忆范围

记录已启用账号的私聊文字，以及 AstrBot 保存到会话历史中的机器人最终文字回复。群聊、图片、语音、引用、转发和命令不自动记录；安装前的聊天记录不自动导入。

新消息会在后台整理，不会立刻出现在列表中。服务暂时不可用时普通聊天仍可继续，待办会在连接恢复后继续处理。

对话内容会发送到所连接的 Hindsight 及其模型服务。查看和搜索显示官方服务中的记忆，更正也在官方页面完成。

## 文档

- [安装指南](docs/INSTALL.md)：向导安装与连接已有服务。
- [使用指南](docs/USAGE.md)：面板管理和私聊命令。
- [兼容范围](docs/SUPPORT.md)：版本、供应商、平台与消息类型。
- [备份与恢复](docs/DATA-MAINTENANCE.md)：数据库与插件备份、服务升级和迁移。
- [贡献指南](CONTRIBUTING.md)：源码结构、接口边界和提交要求。

反馈问题请到 [GitHub Issues](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/issues)，附上版本、操作步骤和错误提示。

## 许可

插件使用 [MIT License](LICENSE)。依赖许可见 [NOTICE.md](NOTICE.md)。
