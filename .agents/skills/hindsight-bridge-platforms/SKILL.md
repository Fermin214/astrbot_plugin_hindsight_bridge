---
name: hindsight-bridge-platforms
description: Hindsight Bridge 的多平台消息边界与真实宿主验证。
---

# 平台消息与宿主验证

遵循 docs/ARCHITECTURE.md 和 docs/PLATFORM-ACCEPTANCE.md 的身份契约，仅通过 AstrBot 扩展和 Hindsight 官方接口工作。

- 平台实例、机器人账号、用户三维隔离；校验原始收件人及客户端归属。换号前积压事件不得进入新账号的 Bank。
- 公共提取在 `core/source.py`：文字混媒体保留正文与极简占位，QQ/TG 表情保留可读标记，纯媒体跳过。核验各平台原始接收者、客户端和时间，不信宿主拼装正文。
- 引用与链接私聊默认正常保存；引用背景仅按原始 ID 查同作用域本地窗口，最多 200 字，关闭保存助手时不得附被引用助手正文。图文轮次须同时核查用户入库、工具可用与助手历史关联。
- 异步请求返回前重新校验身份、Bank 代次和权限；清空后的迟到结果不得交付，旧任务不得复活。
- 使用真实 AstrBot 验证私聊命令、事件监听器、Agent 钩子、主请求与子 Agent、full 和 skills_like 工具模式。宿主可见中文描述必须齐全。
- 按 docs/PLATFORM-ACCEPTANCE.md 验证受影响平台。协议模拟须记录模拟边界，真实账号收发结果单独记录。
- 根据改动使用 tools/run-feedback-e2e.py 的受影响 scope，以及 tools/run-live-e2e.py --scenario full；比对模型实收内容、持久化队列与使用记录。
- 工具改动分别使用 `--scope read-tools --tool-schema-mode full` 与 `skills_like`，保留真实主备切换、并发子 Agent 和子先结束证据。`gateway` 覆盖段级采集、跳过与超时组合；迁移使用 `upgrade`，不以协议模拟代替真实宿主闭环。
- 未知 Hindsight 父、子操作状态继续在途核对，不判完成或取消，不放行维护恢复。
