# 微信 ClawBot

使用 AstrBot 的原生微信 ClawBot 适配器 `weixin_oc`。登录与收发由 AstrBot 管理，Bridge 处理私聊文字的记忆保存、召回和账号隔离。此入口不包含公众号、企业微信或其他微信协议。

## 连接与使用

1. 在 AstrBot 平台管理中添加微信 ClawBot，按宿主扫码入口连接账号。
2. 用测试微信私聊 `/sid`，复制回复中的用户 UID。
3. 在 Bridge 的「用户与记忆」中选择该平台实例，填写 UID，确认机器人身份。
4. 选择「读写记忆」，发送普通私聊文字。

UID 使用 `/sid` 返回的值，不使用手机号、微信号或昵称。机器人身份取宿主登录的 `account_id`，与平台实例 ID 分开保存。

同账号重新登录或 Token 轮换沿用同一记忆库。更换机器人账号会使旧账号绑定失效，必须确认新身份；新账号使用独立记忆库。恢复原机器人账号也需要重新确认，普通配置保存不会恢复旧绑定。

微信的发送上下文由 AstrBot 管理。连接后先向机器人发送一条消息；长时间没有交流或宿主提示上下文失效时，再发一条消息后重试发送操作。

## 消息范围

自动记录普通私聊的纯文字和可关联的最终持久助手文字。命令、群聊、引用、转发、语音转写、图片、文件、视频和混合消息不自动记录。文字条目按 AstrBot 的转换规则去除各条目两端空白，再用换行拼接。

来源必须包含匹配已登录机器人的原始 `to_user_id`、匹配使用者的 `from_user_id`、稳定 `message_id` 和原始毫秒时间 `create_time_ms`。宿主生成的 UUID、接收时间、`msg_id` 或正文摘要不能替代这些凭据。入站方向为 `USER=1`，状态只接受 `NEW=0` 或 `FINISH=2`；生成中、删除和后续更新事件不作为新文字保存。

原始接收者用于核对排队事件的身份。插件不从可变 adapter 的事后账号值推断历史消息归属；换账号后，旧接收者事件不能写入新账号的记忆库。不同实例和平台也分别保存。

## 开发验证

协议参考腾讯 `@tencent-weixin/openclaw-weixin 2.4.10` 的 `src/api/types.ts`；宿主转换参考 AstrBot `4.29.1 / 4.29.4` 的 `weixin_oc` 适配器。

使用实际 AstrBot 适配器和合成协议载荷执行来源及控制库检查：

```bash
PYTHONPATH=/path/to/AstrBot \
ASTRBOT_ROOT=/tmp/bridge-weixin-contracts \
ASTRBOT_TEST_VERSION=4.29.4 \
/path/to/venv/bin/python tests/integration/weixin_source_contracts.py \
  --output /tmp/weixin-contracts.json
```

`ASTRBOT_ROOT` 必须是独立空目录。脚本检查原始 ID 精度、毫秒时间、正文转换、媒体与引用排除、换账号前的排队事件、权限时间边界、去重和状态命令防重。报告区分原生宿主合成载荷与真实平台登录；脚本不调用记忆服务或模型。

原生插件安装、重载、请求钩子、临时参考与后台提交使用已有宿主冒烟流程：

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

此流程使用真实 AstrBot 管理器与会话历史、合成微信载荷和本机 HTTP 记忆服务夹具。真实扫码、平台发送和 Hindsight 业务闭环使用[平台接入验证](PLATFORM-ACCEPTANCE.md)中的专属环境。
