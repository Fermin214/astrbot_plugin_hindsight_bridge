---
name: hindsight-bridge-delivery
description: Hindsight Bridge 的改动范围映射、回归验证、打包与候选发布。
---

# 开发交付

按 CONTRIBUTING.md 的代码导航选择验证入口。身份、队列、遗忘和恢复改动验证断线不丢消息、不同人不串库、清空彻底删除；宿主钩子、生命周期、页面和升级改动使用真实 AstrBot/Hindsight 环境。

1. 基于 main 建立开发分支，按可独立验证的批次提交。
2. 运行受影响的验收范围；发布候选运行 `python tests/run.py`、`node tests/test_memory_view.mjs` 和 `node --check views/memory/*.js` 对应的逐文件语法检查。
3. 同步 metadata.yaml 与 CHANGELOG.md 的版本。运行 `python tools/check-package.py --release-base main`。
4. 使用 `python tools/build-test-package.py --distribution --revision <提交> --output <仓库外目录>` 检查导出的文件清单和包内链接。证据记录提交、包树摘要、模型、平台版本及配置。
5. Push 并创建 PR，由 CI 完成完整回归；使用独立本地普通审查对话执行 `/review`，每个 PR 至多两次，复用同一对话。修复有效反馈并针对性验证。
6. 归档脱敏证据，删除验收容器、卷、临时镜像标签与输出目录。仅回收本次创建的资源；异常遗留浏览器用 `python tools/run-live-e2e.py --sweep-browsers` 清理。

PR 提供用户入口变化、升级注意事项和实际验证结果。合并由用户操作。
