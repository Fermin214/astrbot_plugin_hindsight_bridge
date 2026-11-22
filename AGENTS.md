# Hindsight Bridge

Bridge 只连接 AstrBot 与 Hindsight，通过原生扩展和官方接口完成消息转换、身份映射、记忆传递与连接可靠性，不修改双方源码或接口语义。AstrBot 负责 Agent、模型、会话与平台；Hindsight 负责记忆处理。

管理范围限于插件自身配置、数据和自行部署的专属配套资源。连接已有 Hindsight 时，只操作插件对应 Bank，不管理服务全局配置、部署或运维。配套资源与已有资源隔离，不干预 AstrBot 全局配置或其他插件，双方须能独立运行。

## 工程边界

- 身份、队列、重试、恢复与遗忘必须保障三维身份隔离、断线不丢消息和清空彻底删除。接口、配置和持久化格式保持向下兼容，破坏兼容须提供迁移方案。
- 手写代码不加解释性注释；AstrBot 指令、事件监听器、钩子的中文 docstring 和 LLM 工具描述属于宿主可见文案，必须编写并保留。
- 架构与数据流见 [实现说明](docs/ARCHITECTURE.md)；部署与资源归属见 [安装与管理设计](docs/SETUP-DESIGN.md)；代码导航与验证范围见 [贡献指南](CONTRIBUTING.md)。

## 界面约束

- 长度统一展示 token；可编辑设置点击保存后生效；同类按钮统一边框、字号和颜色。
- 宿主 iframe 内可用页内 `dialog`，外链和 `window.open` 受限；外部地址提供复制按钮，操作指南放页内，参见 `CHANGELOG.md` 的 0.2.4 修复记录。
- Hindsight 原生功能沿用所支持版本官方中文词汇；无官方译名时保留原生术语，不改变配置、API 或持久化字段含义。

## 验证与发布

- 宿主钩子、初始化、生命周期、页面集成、安装升级或恢复改动须走真实 AstrBot/Hindsight 受影响路径。证据记录代码、模型、平台版本和配置，后续只重验受改动影响的路径。
- 根目录回归：`python tests/run.py`（含 Node 页面测试）；前端单独检查：`node tests/test_memory_view.mjs`。
- JavaScript 语法：`for asset in views/memory/*.js; do node --check "$asset" || exit 1; done`。
- 版本与打包门禁：`python tools/check-package.py --release-base main`。
- 分发 ZIP：`python tools/build-test-package.py --distribution --revision <提交> --output <仓库外目录>`，检查实际包清单与包内链接。
- 真实联调：`python tools/run-feedback-e2e.py --scope <范围> --output <仓库外目录>`；完整收发与恢复：`python tools/run-live-e2e.py --scenario full --output <仓库外目录>`。
- 代码审查使用共享 `codex-review` Skill。
- 合并到 `main` 即进入对外发布流程，必要验证须在合并前完成。涉及版本更新时同步 `metadata.yaml` 与 `CHANGELOG.md`，变更记录只记主要功能、重要兼容和必要迁移。
