---
name: hindsight-bridge-platforms
description: Hindsight Bridge 的多平台消息边界与真实宿主验证。
---

# 平台消息与宿主验证

遵循 docs/ARCHITECTURE.md 和 docs/PLATFORM-ACCEPTANCE.md 的身份契约，仅通过 AstrBot 扩展和 Hindsight 官方接口工作。

- 平台实例、机器人账号、用户三维隔离；校验原始收件人及客户端归属。换号前积压事件不得进入新账号的 Bank。
- 无关事件元数据可忽略，消息主体只接收已支持的纯文本；启用的引用和链接只触发读取，不作为用户原话保存。
- 异步请求返回前重新校验身份、Bank 代次和权限；清空后的迟到结果不得交付，旧任务不得复活。
- 使用真实 AstrBot 验证私聊命令、事件监听器、Agent 钩子、主请求与子 Agent、full 和 skills_like 工具模式。宿主可见中文描述必须齐全。
- 按 docs/PLATFORM-ACCEPTANCE.md 验证受影响平台。协议模拟须记录模拟边界，真实账号收发结果单独记录。
- 根据改动使用 tools/run-feedback-e2e.py 的受影响 scope，以及 tools/run-live-e2e.py --scenario full；比对模型实收内容、持久化队列与使用记录。
- 未知 Hindsight 父、子操作状态继续在途核对，不判完成或取消，不放行维护恢复。
