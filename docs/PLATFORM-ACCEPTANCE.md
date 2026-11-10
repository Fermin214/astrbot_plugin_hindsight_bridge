# 平台接入验证

平台登录、网络、收发和媒体处理使用 AstrBot 原生适配器。Bridge 验证来源、保存与召回记忆，不持有平台 Token，不提供扫码或轮询客户端。每个平台使用独立开发分支、测试宿主和测试账号。

## 创建测试宿主

在仓库根目录执行：

```bash
python3 tools/prepare-platform-acceptance.py \
  --platform weixin_oc \
  --output /tmp/bridge-weixin \
  --dashboard-port 6188
docker compose -f /tmp/bridge-weixin/compose.yaml up -d
```

Telegram 使用 `--platform telegram`、另一个输出目录和端口。工具只接受新目录，为每套环境生成独立名称、数据目录和初始管理密码。密码在输出目录的 `.env` 中，登录用户名为 `astrbot`。Docker 端口绑定 `127.0.0.1`；远程访问使用 SSH 隧道。

没有 Docker 时，在已安装官方 AstrBot 及其依赖的独立 Python 环境启动：

```bash
sh /tmp/bridge-weixin/start-native.sh \
  /path/to/venv/bin/python /path/to/AstrBot/main.py
```

此入口通过 `ASTRBOT_ROOT` 选择专属数据目录，Dashboard 使用指定的本机端口。两种启动方式使用同一份测试数据，一次只启动其中一种。

在 AstrBot 平台管理中添加原生微信 ClawBot（`weixin_oc`）或 Telegram（`telegram`），由宿主管理扫码、Bot Token 和网络。测试用户先私聊 `/sid`，记录平台实例 ID 与 UID。

## 单次字段采样

测试宿主自带「Bridge 平台字段采样」插件，默认关闭。在插件配置中填写测试平台实例 ID 和测试用户 UID，再启用单次采样。

1. 私聊发送 `/bridge_probe arm text`。
2. 看到提示后，在 120 秒内发送一条合成测试文字。
3. 到 `data/plugin_data/bridge_platform_probe/` 读取生成的 JSON。
4. 对其他样本重新执行 `arm`；`/bridge_probe cancel` 取消一次采样。

标签为 `text`、`command`、`image`、`voice`、`file`、`video`、`reply`、`forward`、`formatted`、`text-link`、`mention`、`relogin`。每次只采下一条符合账号范围和接收时间的私聊事件。插件重载清除采样选择。

报告保存字段是否存在、类型、组件类型、原始 ID 与宿主 ID 是否一致、时间转换、正文转换和接收者匹配结果。ID 和正文只保存使用本地密钥计算的 HMAC；不同测试安装的指纹不能直接比较。`fingerprint.key` 留在测试数据目录，用于同一安装内的重新登录对照。

`producer_at_arm` 与 `producer_at_observation` 是观察时的原生身份快照。排队事件的来源证明仍须依据原始接收者或事件携带的原生客户端。报告不保存原始载荷、正文、平台 Token、`context_token`、二维码或媒体地址与密钥。

| 平台 | 字段与事件核对 |
| --- | --- |
| 微信 ClawBot | `from_user_id`、`to_user_id`、原始 `message_id`、`create_time_ms`、方向、状态、文字条目与组件转换 |
| Telegram | 普通 `Update.message`、数字 Bot ID、事件客户端、chat/发送者关系、消息 ID、带时区的 date、实体与正文转换 |

## Bridge 业务流程

使用仓库候选包安装到测试宿主，按[安装指南](INSTALL.md)连接独立 Hindsight。选择测试平台与 UID，确认机器人身份，启用读写记忆。

| 场景 | 操作与预期 |
| --- | --- |
| 保存与回忆 | 发送含独特合成偏好的文字；用户与最终助手文字分别保存；新会话仍能召回 |
| 三种模式 | 读写记忆保存并召回；只读停止新增；停用停止保存和召回 |
| 知识页与工具 | 在本人记忆库选择知识页；核对自动参考与按需工具的实际结果 |
| 用户与实例隔离 | 使用不同用户或实例发送不同合成内容；查询和命令只作用于本人记忆库 |
| 去重与命令防重 | 原生对象重放同一消息；材料只保存一次，旧状态命令不覆盖较新的暂停 |
| 重新登录与换账号 | 同账号身份保持连续；不同账号重新确认；旧排队事件不能写入新账号 |
| 权限与迟到结果 | 撤销使用者或暂停期间，不采集新内容，不注入迟到查询结果 |
| 断线与重载 | Hindsight 恢复后沿用冻结任务；宿主与插件重载后身份、模式和记忆库保持一致 |
| 完整遗忘 | 清除所选记忆范围及旧待办；旧查询、旧助手回调不能进入新代次 |
| 长文与文件 | 核对原生发送、分段和 JSON 导出；平台拒绝发送时显示实际错误 |

证据记录代码提交与文件摘要、AstrBot/Hindsight/SDK 版本、模型配置标识、平台实例指纹、操作和实际结果。平台收发、原生宿主合成载荷、真实 Hindsight 与模型调用分别记录。

