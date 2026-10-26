# Hindsight Memory

让 AstrBot 在新会话中也能记得你。

正常聊天，插件就会把 QQ 私聊文字交给 Hindsight 整理成长期记忆，并在之后的聊天中自动查找相关内容。不同用户的记忆分开保存。

**插件设置和记忆管理都在 AstrBot 内完成。** 可以复用 AstrBot 已配置的模型，查看、更正、导出和遗忘记忆时无需另开管理网页。配套服务初次安装只需在 Linux 主机执行一次向导生成的命令。

## 安装

适用于 **Linux**、**AstrBot 4.29.1**、**内置 Agent** 和 **OneBot v11 QQ 私聊**。Windows 部署支持安排在后续版本。

1. 在 AstrBot 打开 **插件管理 → 安装 → 从链接安装**，粘贴：

   ```text
   https://github.com/Fermin214/astrbot_plugin_hindsight_bridge
   ```

2. 安装完成后打开 **Hindsight Memory 插件详情 → 记忆管理 → 设置**。
3. 选择安装方式：生成配套服务安装命令，在运行 AstrBot 的 Linux 主机执行；已有 Hindsight 1.1.0 则填写服务地址与 API Key。
4. 配套服务安装完成后，在设置中选择聊天与嵌入模型，可选重排模型，点击 **检查并保存模型**。
5. 选择 QQ 机器人并添加使用者的 QQ 号，点击 **完成设置**。

配套安装的主机需已安装 Docker Engine 和 Compose v2。安装器自动生成密钥、启动服务并连接 Docker 网络，无需编辑环境文件。保存设置后无需重载插件，之后正常聊天即可。

对话与嵌入模型从 AstrBot 供应商列表选择，无需重复填写模型地址和 Key。嵌入模型用于查找相关记忆，与平常聊天的模型用途不同；缺少时，向导会提示去 AstrBot「模型供应商 → 嵌入」添加。重排模型可不选。

长期记忆的总开关使用 AstrBot 插件面板的启停。配套服务可在 **设置 → 服务维护** 生成完整卸载命令，删除该服务及其全部记忆数据；插件本体通过 AstrBot 卸载入口移除。

详细步骤及常见问题见 [安装指南](docs/INSTALL.md)。手工维护后端见 [Linux 部署说明](deploy/README.md)。

## 使用与管理

日常聊天无需发送记忆命令。保存使用者后，即可在 **记忆管理** 中选择用户、切换正常记忆、只读和暂停；产生记忆后可以查看、搜索、更正和导出。完整遗忘需要再次确认。

使用者也可以在自己的 QQ 私聊中发送：

| 想做什么 | 命令 |
| --- | --- |
| 查看记忆 | `/hindsight view` |
| 停止记录新内容，继续使用已有记忆 | `/hindsight readonly` |
| 暂停记录和自动使用记忆 | `/hindsight pause` |
| 恢复记忆 | `/hindsight on` |
| 清空自己的记忆 | `/hindsight forget`，再按回复确认 |

完整操作见 [使用指南](docs/USAGE.md)。

## 记忆范围

记录已启用账号的私聊文字，以及 AstrBot 保存到会话历史中的机器人最终文字回复。群聊、图片、语音、引用、转发和命令不自动记录；安装前的聊天记录不自动导入。

新消息会在后台整理，不会立刻出现在列表中。服务暂时不可用时普通聊天仍可继续，待办会在连接恢复后继续处理。

对话内容会发送到你连接的 Hindsight 及其模型服务。管理员可以管理已开放账号的记忆；面板更正会记录管理员来源。清空记忆不会删除 AstrBot 自身的聊天记录。

## 文档

- [安装指南](docs/INSTALL.md)：向导安装与连接已有服务。
- [使用指南](docs/USAGE.md)：面板管理和私聊命令。
- [兼容范围](docs/SUPPORT.md)：版本、供应商、平台与消息类型。
- [备份与恢复](docs/DATA-MAINTENANCE.md)：服务维护、升级和迁移。
- [开发与贡献](CONTRIBUTING.md)：源码结构、检查及实现说明。

反馈问题请到 [GitHub Issues](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/issues)，附上版本、操作步骤和错误提示。

## 许可

插件使用 [MIT License](LICENSE)。依赖许可见 [NOTICE.md](NOTICE.md)。
