# Hindsight Memory

让 AstrBot 在新会话中也能记得你。

插件会把 QQ 私聊中的文字对话交给 Hindsight 整理成长期记忆。之后聊天时，机器人会自动查找相关记忆，用来理解你的偏好、经历和之前讨论过的事情。

- **自动记忆**：正常聊天即可，保存和查找记忆都在后台进行。
- **跨会话使用**：切换或新建会话后，仍可使用已经保存的记忆。
- **自己管理**：随时查看、导出、更正或清空自己的记忆，也可以暂停记录。
- **按用户分开**：每个用户拥有独立的记忆空间。

## 安装前需要什么

| 需要的东西 | 说明 |
| --- | --- |
| 已能正常聊天的 AstrBot | 使用 **4.29.1**，执行器选择 **内置 Agent**（`local`） |
| 通过 OneBot v11 接入的 QQ | 支持 **QQ 私聊纯文字** |
| Hindsight **1.1.0** 记忆服务 | 需要它的 API 地址和 API Key |

**从链接安装只会安装 AstrBot 插件。** Hindsight 是另行部署的记忆服务，负责存储和整理记忆。

已经有 Hindsight 地址和 Key，可以直接安装。还没有记忆服务，请先看 [部署 Hindsight](deploy/README.md)；这部分需要 Docker、模型服务和向量服务的配置。

## 在 AstrBot 中安装

1. 打开 AstrBot 管理面板，进入 **插件管理 → 安装 → 从链接安装**。
2. 粘贴下面的地址，点击安装。所需的 Python 依赖会自动安装。

   ```text
   https://github.com/Fermin214/astrbot_plugin_hindsight_bridge
   ```

3. 在已安装列表中找到 **Hindsight Memory**，打开 **配置**，填写以下内容。

| 配置项 | 怎么填 |
| --- | --- |
| Hindsight API 地址 | 填记忆服务的地址。与 AstrBot 在同一 Docker 网络时通常为 `http://hindsight:8888` |
| Hindsight API Key | 填记忆服务提供的 API Key；使用仓库部署模板时，取 `.env` 中的 `HINDSIGHT_API_TENANT_API_KEY` |
| 稳定服务标识 | 给这套记忆服务起一个固定名字，例如 `home-hindsight`，以后沿用 |
| 消息时区 | 中国大陆填 `Asia/Shanghai`，香港填 `Asia/Hong_Kong` |
| 允许使用记忆的平台账号 | 新增一项，填接入 QQ 的平台实例 ID 和你自己的 QQ 号 |

在与机器人的 **QQ 私聊**中发送 AstrBot 自带的 `/sid`：回复中的 **Bot ID** 就是“平台实例 ID”，**UID** 就是“用户 ID”。填写时去掉外侧的 `「」`。允许账号列表为空时，所有人都不会启用记忆。

4. 在 AstrBot 的 **配置文件 → AI** 中，执行器选择 **内置 Agent**，并在“其他配置”中关闭 **现实世界时间感知**和 **用户识别**。具体位置见 [对话设置](docs/INSTALL.md#四设置-astrbot-对话选项)。
5. 其余配置保持默认，打开配置中的 **启用记忆**，保存并重载插件。插件在列表中也应处于启用状态。

接下来直接正常聊天即可。新消息会在后台整理为记忆，相关内容会在后续聊天中自动提供给机器人。首次使用从启用后的对话开始积累记忆。

具体配置和地址填写方式见 [安装指南](docs/INSTALL.md)。

## 日常怎么用

日常聊天无需输入记忆命令。想管理记忆时，在与机器人的 QQ 私聊中发送：

| 想做什么 | 发送 |
| --- | --- |
| 查看已经记住的内容 | `/hindsight view` |
| 只使用已有记忆，停止记录新内容 | `/hindsight readonly` |
| 暂停记录和自动使用记忆 | `/hindsight pause` |
| 恢复自动记录和使用记忆 | `/hindsight on` |
| 清空自己的全部记忆 | `/hindsight forget`，再按回复确认 |

更正、导出、搜索和完整命令见 [使用指南](docs/USAGE.md)。连接有问题时，发送 `/hindsight status`，对照 [常见问题](docs/INSTALL.md#常见问题) 处理。

## 会记录哪些内容

记录启用账号的私聊文字，以及 AstrBot 保存到会话历史中的机器人最终文字回复。群聊、图片、语音、引用、转发和以 `/` 开头的命令不自动记录；安装前的聊天记录不自动导入。

记忆由 Hindsight 在后台整理，新消息不会立即出现在记忆列表里。记忆服务暂时不可用时，普通聊天仍可继续。

对话内容会发送到你连接的 Hindsight，以及它配置的模型和向量服务。清空命令删除该插件管理的个人记忆；AstrBot 自己的聊天记录仍由 AstrBot 管理。

## 更多说明

- [安装指南](docs/INSTALL.md)：在 AstrBot 中安装、填配置、处理常见问题。
- [使用指南](docs/USAGE.md)：查看、更正、导出、暂停和清空记忆。
- [部署 Hindsight](deploy/README.md)：首次搭建记忆服务。
- [兼容范围](docs/SUPPORT.md)：支持的版本、平台和消息类型。
- [备份与恢复](docs/DATA-MAINTENANCE.md)：供维护 AstrBot 的人升级、备份和迁移时使用。
- [开发与贡献](CONTRIBUTING.md)：源码结构、实现说明和开发检查。

反馈问题请到 [GitHub Issues](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/issues)，附上版本、操作步骤和错误提示。

## 许可

插件使用 [MIT License](LICENSE)。Hindsight 和其他依赖的许可见 [NOTICE.md](NOTICE.md)。
