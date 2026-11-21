# 平台接入验证

平台登录、收发和媒体处理由 AstrBot 原生适配器负责；Bridge 负责来源校验、记忆存取和账号隔离。每个平台在独立分支、测试宿主和专用测试账号下完成真实端到端验证。

## 启动测试宿主

在仓库根目录生成并启动独立测试环境（以微信 ClawBot 为例，Telegram 用 `--platform telegram`，飞书用 `--platform lark` 并先按 [飞书接入](FEISHU.md) 创建开放平台应用）：

```bash
python3 tools/prepare-platform-acceptance.py \
  --platform weixin_oc \
  --output /tmp/bridge-weixin \
  --dashboard-port 6188
docker compose -f /tmp/bridge-weixin/compose.yaml up -d
```

脚本只接收尚未存在的新输出目录，并为每套环境生成独立的项目名、数据目录和管理员初始密码（密码保存在输出目录的 `.env`，用户名为 `astrbot`；端口绑定 `127.0.0.1`，远程调试可通过 SSH 隧道转发）。

如果本机不用 Docker，可在已配好依赖的 AstrBot 环境中直接启动原生进程（两种方式共用同一数据目录，不要同时运行）：

```bash
sh /tmp/bridge-weixin/start-native.sh \
  /path/to/venv/bin/python /path/to/AstrBot/main.py
```

启动后在 AstrBot 平台管理中添加对应适配器完成登录，并让测试账号私聊发送 `/sid` 获取平台实例 ID 与 UID。

## 单次字段采样

测试宿主预装了「Bridge 平台字段采样」辅助插件（默认关闭）。填入测试平台实例 ID 与测试用户 UID 并启用后，即可按消息类型采样：

1. 私聊发送 `/bridge_probe arm <标签>`（可选标签：`text`、`command`、`image`、`voice`、`file`、`video`、`reply`、`forward`、`formatted`、`text-link`、`mention`、`relogin`）。
2. 在 120 秒内发送一条对应的测试消息（中途可用 `/bridge_probe cancel` 取消）。
3. 到 `data/plugin_data/bridge_platform_probe/` 查看生成的 JSON 报告。

采样只检查字段完整性、类型、时间戳转换、正文处理与客户端/接收者归属；所有 ID 与文本仅记录本地随机密钥生成的 HMAC 指纹（`fingerprint.key` 保存在测试目录用于同环境重登比对），不落盘原始载荷、正文或平台 Token。

| 平台 | 重点核对字段 |
| --- | --- |
| 微信 ClawBot | `from_user_id`、`to_user_id`、原始 `message_id`、`create_time_ms`、方向、状态、文字条目与组件转换 |
| Telegram | 普通 `Update.message`、数字 Bot ID、事件客户端、`chat`/发送者一致性、消息 ID、带时区 `date` 与实体转换 |
| 飞书 | 原生 API 客户端、应用与机器人 OpenID、SDK 消息 ID、毫秒 `create_time`、`p2p/text` 类型与引用转换 |

## 端到端业务验证清单

将候选包安装到测试宿主，按 [安装指南](INSTALL.md) 接入独立 Hindsight，添加测试账号并切换为「读写记忆」后，逐项验证：

| 验证场景 | 预期结果 |
| --- | --- |
| 基础保存与召回 | 发送含具体偏好的文字，确认用户发言与助手回复分别入库，开启新会话可召回 |
| 三种模式切换 | 读写正常保存召回；只读只召回不新增；停用不保存也不召回 |
| 知识页与按需工具 | 选中并排序知识页，核对自动注入与按需工具读取的真实输出 |
| 多用户/实例隔离 | 不同账号或实例互不串库，私聊命令仅查询本人数据 |
| 消息去重与命令防重 | 重放同一原始消息只入库一次；旧状态命令不覆盖较新的暂停状态 |
| 重登与换号 | 同账号重登延续原记忆库；换号后旧绑定失效、在途消息不写入新账号 |
| 权限撤销与迟到请求 | 移除账号或暂停期间，不再保存新消息，也不注入迟到的检索结果 |
| 断连与重载恢复 | Hindsight 恢复连接后继续处理冻结任务；宿主重载后身份、模式与 Bank 不变 |
| 清空记忆（遗忘） | 彻底删除旧 Bank 及待办任务，旧轮次回调和查询不污染新代次 |
| 长消息与 JSON 导出 | 核对超长文本分段及 `/hindsight export` 导出文件，平台拒收时返回明确错误 |

验收报告需记录对应的代码 commit、文件摘要、AstrBot/Hindsight/SDK 版本、模型标识、平台指纹及实测结果。
