# 贡献指南

用户向的安装与使用说明分别见 [安装指南](docs/INSTALL.md) 和 [使用指南](docs/USAGE.md)；模块职责、身份模型与存储结构见 [实现说明](docs/ARCHITECTURE.md)。

## 代码导航

| 模块 / 路径 | 职责 | 对应测试入口 |
| --- | --- | --- |
| `core/*_source.py` / `core/materials.py` | 平台来源校验、用户原文冻结、持久化 history 关联 | `python tools/run-feedback-e2e.py --scope assistant-capture --output /tmp/bridge-capture` |
| `core/outbox.py` / `core/control.py` | 队列、去重、控制库事务、遗忘与快照恢复 | `python tools/run-feedback-e2e.py --scope recovery --output /tmp/bridge-recovery` |
| `core/panel*.py` / `views/memory/*` | 管理面板前端（ES Modules）与后台管理 API | `node --test tests/test_memory_view.mjs` |
| `core/installer.py` / `core/managed_connections.py` | 配套 Docker 安装命令、接入状态机与模型探测 | `python tools/run-managed-e2e.py --scenario connection --output /tmp/bridge-connection` |

核心概念速查：
- **`scope_id` / `source_id`**：同一个平台来源标识，业务库用 `scope_id`，控制库映射用 `source_id`。
- **`principal_id` / `owner_id`**：逻辑用户及归属标识，队列按 `owner_id` 串行投递。
- **Bank 与代次（generation）**：用户在 Hindsight 的独立记忆库及版本；每次「清空记忆」推进代次并作废旧 Bank 与未完成队列。
- **`background` / `reading` / `reflection`**：知识页选择、按需读取方式和 Reflect 设置。

兼容边界、恢复流程与面板规范分别见 [兼容范围](docs/SUPPORT.md)、[备份与恢复](docs/DATA-MAINTENANCE.md) 和 [安装与管理设计](docs/SETUP-DESIGN.md)。

## 开发与提交要求

- **保持宿主与上游边界**：只调用 AstrBot 和 Hindsight 的公开接口，不修改宿主全局配置或其他插件；持久化格式或身份规则改动必须提供向下兼容或迁移路径。
- **发布完整性**：合入 `main` 时若稳定版已有 tag 且运行文件有变化，须同步提升 `metadata.yaml` 版本号并更新 `CHANGELOG.md` 首条标题；同时确保 `python tools/check-package.py` 通过（包含模块、静态页资产、文档链接与锚点校验）。

## 按改动选验证

提交和验收遵循“改什么测什么”，避免无效全量重跑；同时重点覆盖状态交界处的竞态：

| 改动范围 | 必须验证的场景 |
| --- | --- |
| 安装、升级、服务管理或面板操作 | 走一遍空卷新装或受影响入口；更新镜像时同时验证无本地镜像的新装与带旧数据卷的重装；连接已有服务时确认未越权改动服务配置 |
| 身份、授权、遗忘或异步写入 | 在请求发出与返回之间切换账号、更换连接或执行清空，再放行旧请求，确认最终写入、召回与迟到拒绝均归属正确（含同账号切换传输方式） |
| 助手正文、知识页、工具或参考记录 | 让子 Agent 抢先完成，对照主请求实际交付内容、持久回复与使用记录；筛选与限额组合测试时加入足以占满预算的排除条目；验证选页全失效时的逐一清理 |
| 命令与消息分类 | 使用宿主识别的完整命令输入（含前缀与尾随参数）验证，并确认只读状态查询不会篡改原诊断状态 |
| 表单草稿、轮询与保存 | 在页面修改但未保存时触发后台轮询和切页，确认草稿保留；切换服务后确认旧草稿清零；保存并重载后核对持久化结果 |
| 备份、恢复或跨系统文件操作 | 用旧业务库配对最新控制库恢复，测试同一目标并发替换与（如涉及）Windows 文件占用与超时分支 |

整理 PR 时对照检查标题、版本号与变更记录，写明受影响的用户入口、界面变化和升级注意事项。

## 自动化测试与 CI

本地与 CI 按变更文件自动选择检查范围（纯文档变更仅运行基础语法、元数据、打包与文档链接检查）：

| 检查项 | 覆盖范围 | 触发条件 |
| --- | --- | --- |
| `check` | Python 回归 / 前端单测 / JS 语法 / 元数据与打包链接 | 基础检查始终执行，回归按改动文件选择 |
| `windows` | Windows 原生单元测试、分发包安装与宿主重载 | 每次插件检查 |
| `astrbot-smoke` | 真实 AstrBot 宿主安装、重载、钩子、知识页与卸载 | 核心源码、配置、宿主脚本或工作流改动 |
| `hindsight-model-contracts` | Hindsight 1.1.0 / 1.2.0 镜像下的模型协议兼容性（`tools/check-model-protocols.py`） | 供应商适配、协议转换、运行依赖或 `compose.yaml` 改动 |
| `managed-install` | 无预置镜像冷构建、pgvector 扩展与旧卷重装 | 安装器、数据库镜像、部署模板改动（及每周主分支定时巡检） |

本地常用专项验收工具（需 Docker 与对应镜像；工具会在结束时清理自身创建的容器与卷，证据目录需手动保留或清理）：
- `python tests/run.py`：本地 Python 单元测试。
- `node --test tests/test_memory_view.mjs`：管理面板前端逻辑单元测试。
- `python tools/check-package.py`：元信息、语法、资源引用及 Markdown 链接/锚点检查。
- `python tests/integration/install_startup.py --output <目录>`：冷构建与数据库扩展初始化检查（加 `--fresh-only` 仅测冷构建空卷）。
- `python tools/run-backend-compat.py --output <目录>`：双版 AstrBot + PostgreSQL 升级、任务中断与删除路径校验（支持 `--baseline`、`--scope deletion`、`--scope maintenance`）。
- `python tools/run-feedback-e2e.py --output <目录>`：真实 AstrBot/Hindsight/Dashboard 联调（支持 `--astrbot-version`、`--hindsight-version` 及 `--scope single-page | multi-page | multi-page-recovery | read-tools`）。
- `python tools/run-parameters-e2e.py --mode existing|managed --output <目录>`：面板参数到实际聊天的端到端校验（支持 `--scope references | advanced`）。
- `python tools/build-test-package.py --distribution --revision <提交> --output <目录>`：构建带文件摘要的分发验收包。

真实浏览器验收为每次运行单独创建镜像标签和文件租约，结束后随专属容器一起清理。Linux 可定期运行 `python tools/run-live-e2e.py --sweep-browsers`，回收异常退出超过一天的浏览器容器和镜像标签，活跃租约不受影响。启动要求至少 10 GiB 可用空间，低于 15 GiB 时提示。`--output` 指定的证据目录由调用方归档或清理。

提交 Issue 请附版本、复现步骤、预期/实际结果和脱敏日志，注意切勿提交真实账号凭据、聊天原文或运行数据库。
