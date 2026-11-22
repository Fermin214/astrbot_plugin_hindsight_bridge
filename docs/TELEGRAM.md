# Telegram

通过 AstrBot 原生 `telegram` 适配器接入 Bot 私聊。BotFather 建号、Token、轮询、代理和消息收发由 AstrBot 管理，Bridge 负责私聊文字的保存、召回和隔离。

## 连接与使用

1. 在 AstrBot 平台管理添加 Telegram Bot，填好 Bot Token 与网络代理设置。
2. 用自己的账号主动向机器人发送 `/sid`。
3. 打开 Bridge **用户与记忆**，点击 **+ 添加用户**，选择对应实例并填入 `/sid` 返回的数字 UID（不要填 `@username`）。
4. **确认机器人身份**，将模式设为 **读写记忆**。

Telegram 侧采用数字 User ID 与数字 Bot ID 作为稳定标识，修改 `@username` 不影响记忆归属。重新连接同一 Bot 可直接沿用原记忆库；更换 Bot Token 需要重新确认身份并启用新的记忆库。

## 消息范围

- **自动记录**：普通私聊文字、格式实体、`custom_emoji`、链接与提及，以及带文字 caption 的媒体；自定义表情保留可读标记，附件仅留媒体占位。按设置关联保存最终助手回复。
- **不自动记录**：命令（如 `/hb@MyBot status` 按宿主去后缀后执行但不入库）、转发、编辑消息、纯媒体无文字、群组、频道、callback 与 business 会话。

引用回复默认保存新正文；仅能从本地窗口按原始 ID 核验时附带简短引用背景，不采信宿主拼装的旧正文。实体范围按 UTF-16 偏移处理。

来源校验核对原生 `Update.message`、事件绑定的 Bot 客户端、发送者、私聊 chat（需与 User ID 一致）、消息 ID 与带时区的 `date`。Bot 断线重建客户端后，旧客户端遗留的排队事件会被拒绝，避免写错记忆库。

## 开发验证

来源与适配器契约验证（使用实际 AstrBot 适配器与 `python-telegram-bot` 对象及合成 `getMe` 响应）：

```bash
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-telegram-contracts \
ASTRBOT_TEST_VERSION=4.29.4 \
/path/to/venv/bin/python tests/integration/telegram_source_contracts.py \
  --output /tmp/telegram-contracts.json
```

`ASTRBOT_ROOT` 需使用独立空目录。

宿主冒烟验证（使用真实 AstrBot 管理器、合成 Telegram 载荷与本地记忆夹具）：

```bash
python3 tools/build-test-package.py --distribution --output /tmp/bridge-telegram-package
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-telegram-smoke \
ASTRBOT_TEST_VERSION=4.29.4 \
ASTRBOT_PLATFORM_FIXTURE=telegram \
DELIVERY_ROOT=/tmp/bridge-telegram-package \
ASTRBOT_DISABLE_METRICS=1 \
/path/to/venv/bin/python tests/integration/astrbot_smoke.py
```

真实 Bot 收发与完整业务联调见 [平台接入验证](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/blob/main/docs/PLATFORM-ACCEPTANCE.md)。
