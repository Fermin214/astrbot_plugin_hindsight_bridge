# 飞书

使用 AstrBot 的原生飞书适配器 `lark`。应用凭证、事件订阅、长连接和 Webhook 均由 AstrBot 管理，Bridge 处理私聊文字的记忆保存、召回和用户隔离。支持飞书与 Lark 的官方 API 域名。

## 连接与使用

1. 在飞书开放平台创建企业自建应用，启用机器人能力，为测试使用者开放应用可用范围。
2. 在 AstrBot 添加飞书机器人，填写 App ID、App Secret。选择长连接，或按 AstrBot 的原生入口配置 Webhook。
3. 在飞书配置接收消息事件 `im.message.receive_v1`，开通读取机器人单聊消息及以应用身份发送消息的权限，发布应用版本。
4. 私聊机器人发送 `/sid`，复制用户 UID。飞书使用 `ou_` 开头的 OpenID，不使用手机号、邮箱、Union ID 或昵称。
5. 在 Bridge 的「用户与记忆」中添加该平台实例和 OpenID，确认机器人身份，选择「读写记忆」。

应用登录成功后，AstrBot 会读取机器人 OpenID。身份确认失败时，先检查 AstrBot 日志中的机器人信息请求、应用权限和消息收发。

保持平台实例、API 域名、App ID、机器人 OpenID 和使用者 OpenID 不变，重载机器人或在长连接与 Webhook 之间切换可沿用同一记忆库。更换应用或机器人身份后，需要重新确认；新身份使用独立记忆库，恢复旧应用也需要重新确认。

飞书 App Secret 仅配置在 AstrBot。Bridge 配置不接收或保存飞书凭证。

## 消息范围

自动记录普通私聊纯文字和可关联的最终持久助手文字。命令、群聊、话题、引用、转发、富文本 `post`、卡片、语音、图片、文件、视频及混合消息不自动记录。普通文字中的网址仍按文字处理。

来源使用原生 `LarkMessageEvent`、SDK `EventMessage`、原始 `message_id`、`chat_type=p2p`、`message_type=text` 和毫秒 `create_time`。正文保留原始文字，宿主的 Plain 组件与请求文字必须符合 AstrBot 去除两端空白的转换规则。缺失原始时间时不回填宿主接收时间，编辑时间晚于创建时间的消息不新增记录。

AstrBot 的 `raw_message` 只保留 SDK 的消息对象，使用者 OpenID 取自原生事件的发送者；Bridge 不从消息对象推造发送者或租户字段。事件携带的 API 客户端和机器人 OpenID 必须与已确认的宿主实例相符。旧客户端产生的排队事件不能进入重载后的实例。

## 开发验证

宿主转换参考 AstrBot `4.29.1 / 4.29.4` 的 `lark` 适配器及 `lark-oapi` 的 `P2ImMessageReceiveV1`、`EventMessage`。来源与身份检查使用实际 AstrBot、SDK 对象和合成事件：

```bash
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-lark-contracts \
ASTRBOT_TEST_VERSION=4.29.4 \
/path/to/venv/bin/python tests/integration/lark_source_contracts.py \
  --output /tmp/lark-contracts.json
```

`ASTRBOT_ROOT` 使用独立空目录。脚本覆盖长连接与 Webhook 的事件转换、原始时间、正文一致性、消息排除、客户端更换、身份隔离和控制库去重。

安装、升级、重载、请求钩子与后台保存使用原生宿主冒烟流程：

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

此流程使用真实 AstrBot 管理器与会话历史、合成飞书载荷及本机 HTTP 记忆服务夹具。真实应用的事件订阅、平台收发与 Hindsight 业务闭环使用[平台接入验证](PLATFORM-ACCEPTANCE.md)中的专属环境。
