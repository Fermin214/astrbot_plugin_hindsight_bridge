# Hindsight Memory

通过自部署 Hindsight 为 AstrBot 提供私聊长期记忆：保存已核对用户原文与标准持久化历史中的最终助手正文，跨会话召回，并提供本人查看、导出、显式更正、模式切换和完整遗忘。

## 开始使用

需要 AstrBot **4.29.1**、内置 `local` 执行器、OneBot v11 私聊，以及启用 API Key 的 Hindsight **1.1.0**。

首次安装 Hindsight，请先按 [从零安装](INSTALL.md#从零安装-hindsight) 部署后端，再安装插件。已有后端可直接进行以下步骤。

1. 在 AstrBot 插件管理中选择通过链接安装，粘贴下面的仓库地址；安装程序自动读取 `requirements.txt`。
2. 填写 API 地址、Key、固定服务标识、时区和精确的平台实例—用户组合。
3. 为独立控制目录提供持久存储，启用插件并重载。
4. 在开放的私聊中发送 `/hindsight status`，确认连接、认证和执行器。
5. 告诉机器人一项偏好，等待任务完成，使用 `/hindsight view` 查看，再新建会话询问。

完整步骤、Docker 网络、文件传输和后端部署见 [安装说明](INSTALL.md)，配置样例见 [config.example.json](config.example.json)。

```text
https://github.com/Fermin214/astrbot_plugin_hindsight_bridge
```

`main` 分支用于源码安装。正式版本以 GitHub Release 和对应标签分发。

## 命令

| 命令 | 作用 |
| --- | --- |
| `/hindsight help` | 常用操作和采集范围 |
| `/hindsight status` | 连接、认证、模式、本人队列、错误原因和清空进度 |
| `/hindsight view [页码]` | 查看本人记忆，每页 10 条 |
| `/hindsight show 记忆ID` | 查看类型、时间、来源和显式更正 |
| `/hindsight recall 问题` | 本人范围内的手动检索 |
| `/hindsight export` | UTF-8 JSON 文件，包含事实、整合观察、源文档和更正 |
| `/hindsight correct 记忆ID 更正原文` | 提交更正，最多 1,000 字符；保留来源关联 |
| `/hindsight on` | 开启采集和自动召回，继续有效代次的原队列 |
| `/hindsight readonly` | 停止采集，保留自动召回，冻结未提交队列 |
| `/hindsight pause` | 停止采集和自动召回，冻结未提交队列 |
| `/hindsight retry` | 登记本人失败任务的显式重试 |
| `/hindsight cancel 任务编号` | 停止指定任务，继续核对迟到子任务；已形成的记忆保留 |
| `/hindsight forget` | 展示完整清空范围并生成有效期 120 秒的确认码 |
| `/hindsight forget 确认码` | 作废旧代次、清理旧 Bank，转入暂停的新代次 |

查看、导出和手动召回在暂停模式下仍可使用；更正须先开启采集。管理命令不进入普通用户消息采集窗口。状态和诊断不展示聊天原文或凭据。

更正示例：先用 `view` 取得记忆 ID，再发送 `/hindsight correct 记忆ID 周五改成下午看展，上午计划作废。`。更正使用新文档和操作身份，后台抽取期间也会作为显式更正提供给后续召回；旧事实保留来源，完整遗忘清除这些内容。

## 支持范围

官方 AstrBot **4.29.1**、内置 `local` 执行器、OneBot v11 / `aiocqhttp` 私聊纯文本，连接启用 API Key 的 Hindsight **1.1.0**。参考集成环境为 Linux AstrBot 和 Linux Docker 后端。

用户消息沿用标准接收事件与原始载荷核对规则。助手正文取自标准 `conversation_manager` 的持久化 `history`，只采集与同一来源账号、代次和会话内已核对用户事件关联的最终纯文本。保存后独立入队，无需等待用户再次发消息。

助手关联以成功读取的前置历史为基线，`history_baselines` 保存历史长度与前缀摘要。只有持久历史增长、原前缀不变且新增部分包含对应用户轮次及最终助手正文时才关联。读取失败不建立助手锚点；压缩、编辑或截断改变边界时拒绝误配，用户原文采集独立进行。

助手记录标明 `role=assistant`、`delivery=unknown`、`time_source=history_observed_at`，不声称 QQ 送达。发送回执仅作可选附加信息；普通、分段和流式发送接口的真实限制见 [支持矩阵](SUPPORT.md)，各路径的最终正文采集以实际验证记录为依据。

不采集 system/tool/reasoning、工具调用中间消息、临时召回块、压缩摘要和未保存草稿。助手建议不能直接记成用户接受，承诺和自称操作完成均属于助手陈述；实际完成需独立来源佐证。

## 数据与可靠性

每条新增用户消息沿用 `uw1` 文档身份；每条最终助手正文使用独立 `aw1` 文档和 operation UUID。新建双方材料为 `dialogue_window_v2`，附带同账号、代次、会话最近 30 分钟内最多 4 个已核对用户轮次及对应助手，双方历史正文合计最多 4,000 字符，超限按最旧完整轮次移除。窗口用于解释“第二个”等指代；旧 `uw1` 冻结请求保持原样，不补入后来正文。

先原子保存身份、正文、摘要和操作 UUID。用户任务在限时召回结束后放行，助手任务独立放行。提交重试使用同一操作和材料；明确失败通过 Operations retry 处理。

每个稳定逻辑用户使用独立 Bank，账号权限按平台实例—用户逐项检查。会话变化不改变长期归属。召回仅临时注入模型，不改写人格和持久聊天历史；召回超时后继续聊天。

业务 SQLite 保存窗口、队列、去重和显式更正；schema 3→4 新增短期 `replies` 和 `history_baselines` 表，沿用既有身份、Bank 与操作 UUID。暂停、只读、清空和移除账号权限均切断窗口及未冻结助手关联。移除权限删除对应 `window`，外键级联删除 replies/baselines；恢复采集从新核对事件与成功读取的前置历史建立语境，迟到助手不能跨边界回填。

独立控制库保存身份、代次、模式、删除记录和操作接收凭据，不含聊天正文。恢复历史业务快照时保留最新控制库，清除旧窗口及失效 `replies`、`history_baselines`，完成旧范围清理后再开放读写；旧 history 不能复活已遗忘内容。备份、升级和恢复见 [数据维护](DATA-MAINTENANCE.md)。

参考后端通过 `deploy/` 提供。插件只连接公开 HTTP API，不安装 Hindsight 到 AstrBot Python 环境；抽取、整合和向量服务按后端配置运行。

## 开发与反馈

开发环境、既有回归和修改方式见 [贡献说明](CONTRIBUTING.md)。问题反馈请提交至 [GitHub Issues](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/issues)，附上版本、复现步骤和脱敏错误信息。

## 许可

插件代码使用 MIT License，见 [LICENSE](LICENSE)。依赖和 Hindsight 部署材料的许可见 [NOTICE.md](NOTICE.md) 与 `deploy/HINDSIGHT-LICENSE`。
