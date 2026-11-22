# 飞书

通过 AstrBot 原生飞书适配器 `lark` 接入单聊。应用凭证、事件订阅、长连接和 Webhook 均由 AstrBot 管理，Bridge 负责单聊文字的保存、召回与账号隔离，支持飞书（中国版）与 Lark（国际版）API 域名。

## 连接与使用

1. 在飞书开放平台创建企业自建应用并开启机器人能力，为测试用户开通应用可见范围。
2. 在 AstrBot 添加飞书机器人，填写 App ID 和 App Secret，按需要启用长连接或配置 Webhook。
3. 在飞书事件与权限中订阅 `im.message.receive_v1`，开通读取机器人单聊消息和以应用身份发送消息权限并发布应用版本。
4. 私聊机器人发送 `/sid`，复制回复中的用户 UID（飞书使用的是 `ou_` 开头的 OpenID，不要填手机号、邮箱、Union ID 或昵称）。
5. 打开 Bridge **用户与记忆**，点击 **+ 添加用户** 选择飞书实例并填入 OpenID，**确认机器人身份** 后把记忆模式设为 **读写记忆**。

确认身份时如果提示失败，优先查看 AstrBot 日志里的机器人信息请求、应用权限与消息收发记录。只要平台实例、API 域名、App ID、机器人 OpenID 和使用者 OpenID 保持不变，在长连接与 Webhook 之间切换或重载机器人都可以沿用同一个记忆库；更换应用或机器人后需重新确认身份。

## 消息范围

- **自动记录**：普通单聊纯文字与对应的最终助手文字回复（按 AstrBot 规则去除首尾空白；普通文字里包含网址仍正常记录）。
- **图文与引用**：`post` 的文字和同消息附件按段提取，媒体仅留简短占位；引用默认保存新正文，背景只按原始 ID 查本地窗口。
- **不自动记录**：命令、群聊、话题、转发、卡片、纯媒体以及已编辑消息。

来源校验基于原生 `LarkMessageEvent`、SDK `EventMessage`、原始 `message_id`、`chat_type=p2p`、`message_type=text` 及毫秒级 `create_time`。事件携带的客户端与机器人 OpenID 必须匹配当前已确认的实例；实例重启更换客户端后，旧客户端残留的排队事件会被跳过。

## 开发验证

契约测试基于 AstrBot `4.29.1 / 4.29.4` 的 `lark` 适配器与 `lark-oapi` 事件结构：

```bash
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-lark-contracts \
ASTRBOT_TEST_VERSION=4.29.4 \
/path/to/venv/bin/python tests/integration/lark_source_contracts.py \
  --output /tmp/lark-contracts.json
```

`ASTRBOT_ROOT` 需使用独立空目录。脚本验证长连接与 Webhook 转换、原始时间、正文一致性、消息过滤、客户端替换隔离以及主库去重。

完整宿主冒烟验证（使用真实 AstrBot 管理器、合成飞书载荷与本地记忆夹具）：

```bash
python3 tools/build-test-package.py --distribution --output /tmp/bridge-lark-package
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-lark-smoke \
ASTRBOT_TEST_VERSION=4.29.4 \
ASTRBOT_PLATFORM_FIXTURE=lark \
DELIVERY_ROOT=/tmp/bridge-lark-package \
ASTRBOT_DISABLE_METRICS=1 \
/path/to/venv/bin/python tests/integration/astrbot_smoke.py
```

真实飞书开放平台应用联调与端到端业务验证见 [平台接入验证](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/blob/main/docs/PLATFORM-ACCEPTANCE.md)。
