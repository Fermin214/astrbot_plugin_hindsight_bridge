# 微信 ClawBot

通过 AstrBot 原生微信 ClawBot 适配器 `weixin_oc` 接入个人微信私聊。扫码登录、Token 维持与消息收发由 AstrBot 管理，Bridge 负责私聊文字的保存、召回和隔离（不含公众号、企业微信或其他第三方微信协议）。

## 连接与使用

1. 在 AstrBot 平台管理添加微信 ClawBot，按指引扫码登录。
2. 用微信向机器人发送 `/sid`，复制回复里的用户 UID（不要填微信号、手机号或昵称）。
3. 打开 Bridge **用户与记忆**，点击 **+ 添加用户**，选择平台实例并填入 UID，然后 **确认机器人身份**。
4. 切换模式为 **读写记忆**，发送普通私聊测试即可。

机器人身份取自宿主登录的 `account_id`。只要用同一个微信账号重新登录或刷新 Token，原有的记忆库会自动延续；一旦更换了其他微信账号，旧绑定会立即失效，需要重新确认身份并使用独立的记忆库（若日后再换回原微信账号，也需重新点击一次确认）。

> 微信的回复上下文由 AstrBot 维持：刚连上时先发一条私聊消息给机器人；如果长时间未沟通或宿主提示上下文失效，再发一条消息后重试发送即可。

## 消息范围

- **自动记录**：普通私聊纯文字与对应的最终助手文字回复（按 AstrBot 规则去除首尾空白并用换行拼接文字条目）。
- **不自动记录**：命令、群聊、引用、转发、语音转写、图片、文件、视频及混合消息；生成中和已删除的事件也不会保存，同一 `message_id` 的后续状态更新不会重复入库（可在聊天参考中按用户开启引用消息的只读召回）。

来源校验基于原始 `to_user_id`（需匹配当前已登录机器人）、`from_user_id`、`message_id`、毫秒时间 `create_time_ms`、入站方向 `USER=1` 与状态 `NEW=0` / `FINISH=2`。排队事件绑定的是发送当时的原始接收者身份，换号后旧账号的在途消息不会误写进新账号记忆库。

## 开发验证

协议类型参考 `@tencent-weixin/openclaw-weixin 2.4.10`（`src/api/types.ts`），事件转换基于 AstrBot `4.29.1 / 4.29.4` 的 `weixin_oc` 适配器。

来源与控制库契约验证：

```bash
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-weixin-contracts \
ASTRBOT_TEST_VERSION=4.29.4 \
/path/to/venv/bin/python tests/integration/weixin_source_contracts.py \
  --output /tmp/weixin-contracts.json
```

`ASTRBOT_ROOT` 需使用独立空目录。脚本验证原始 ID 精度、毫秒时间、媒体/引用排除、换号队列隔离、时间边界与防重。

宿主冒烟验证（使用真实 AstrBot 管理器、合成微信载荷与本地记忆夹具）：

```bash
python3 tools/build-test-package.py --distribution --output /tmp/bridge-weixin-package
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-weixin-smoke \
ASTRBOT_TEST_VERSION=4.29.4 \
ASTRBOT_PLATFORM_FIXTURE=weixin_oc \
DELIVERY_ROOT=/tmp/bridge-weixin-package \
ASTRBOT_DISABLE_METRICS=1 \
/path/to/venv/bin/python tests/integration/astrbot_smoke.py
```

真实扫码收发与完整业务联调见 [平台接入验证](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/blob/main/docs/PLATFORM-ACCEPTANCE.md)。
