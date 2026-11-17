# Telegram

通过 AstrBot 原生 `telegram` 适配器接入 Bot 私聊。BotFather 创建 Bot，Token、轮询、代理和发送由 AstrBot 管理；Bridge 只处理原生事件与对应的记忆库。

## 连接与使用

1. 在 AstrBot 平台管理添加 Telegram Bot，填写测试 Bot Token 和宿主所需网络配置。
2. 用测试用户主动开始私聊，发送 `/sid`。
3. 在 Bridge 的「用户与记忆」中选择平台实例，填写 `/sid` 返回的数字用户 UID。
4. 确认机器人身份，再启用读写记忆。

使用数字 UID，不使用 `@username`。机器人身份也使用初始化后的数字 Bot ID；用户名变更不改变记忆库。重新连接同一 Bot 可以恢复原范围，更换 Bot 后必须重新确认身份。

## 消息范围

接受普通私聊的纯文字，以及不改变可见正文的加粗、斜体、代码等格式实体。命令后缀由宿主处理，例如 `/hindsight@MyBot status` 按 `/hindsight status` 执行；命令不自动记录。

含 `mention`、`text_link` 或 `text_mention` 实体的消息不自动记录。宿主可能把这些消息转换为提及组件或追加链接文字。引用、转发、编辑、媒体、caption、群组、频道、callback、business 与其他扩展会话也不自动记录。

来源核对原始 `Update.message`、事件携带的 Bot 客户端、发送者、私聊 chat、消息 ID 和带时区的 `date`。chat 必须与用户 ID 一致；同一个 chat 内的消息 ID 连同用户 Scope 参与去重。不同 Bot、用户、实例和平台使用独立记忆库。

断线重建客户端后，旧事件仍绑定原来的客户端，不能改用新 Bot 身份。权限确认前积压的消息和已执行的状态命令继续受公共时间边界与防重记录约束。

## 开发验证

使用实际 AstrBot 适配器与 python-telegram-bot 对象，通过合成 `getMe` 响应初始化 Bot：

```bash
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-telegram-contracts \
ASTRBOT_TEST_VERSION=4.29.4 \
/path/to/venv/bin/python tests/integration/telegram_source_contracts.py \
  --output /tmp/telegram-contracts.json
```

`ASTRBOT_ROOT` 必须是独立空目录。报告记录 AstrBot、SDK 与代码摘要，区分合成登录响应和真实平台登录；检查不调用记忆服务或模型。

原生安装、升级、请求钩子、临时参考、知识页和重连路径复用宿主冒烟流程：

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

此流程使用真实 AstrBot 管理器与会话历史、合成 Telegram HTTP/消息数据和本机 HTTP 记忆服务夹具。真实 Bot 收发与 Hindsight 业务闭环见[平台接入验证](PLATFORM-ACCEPTANCE.md)。
