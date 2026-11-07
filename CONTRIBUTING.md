# 贡献指南

安装与日常操作见 [安装指南](docs/INSTALL.md) 和 [使用指南](docs/USAGE.md)。消息采集、队列、用户隔离和数据格式见 [实现说明](docs/ARCHITECTURE.md)。

## 目录

- `main.py`：AstrBot 插件入口、事件接入和用户命令。
- `core/`：身份权限、记忆材料、队列、后端客户端，以及管理 API、供应商适配与 Linux 安装命令生成。
- `views/memory/`：AstrBot 内「用户与记忆」和「插件设置」两个标签页的 HTML、JavaScript 和样式；用户页管理用户、记忆模式和知识页选择，设置页管理模型、连接与服务。
- `.astrbot-plugin/i18n/`：插件视图标题的中文和英文资源。
- `docs/`：安装、使用、兼容范围、数据维护和实现说明。
- `examples/`：插件配置示例。
- `deploy/`：独立 Hindsight/PostgreSQL 部署模板。
- `scripts/`：备份等数据维护脚本。
- `tests/`：可靠写入、隔离、用户控制、导出、更正与双方历史的回归测试。
- `tools/`：安装包元数据、文档链接和文件边界检查。

## 文档分工

README 介绍用途、安装入口和常用操作。`docs/INSTALL.md` 说明向导安装、模型选择与已有服务连接；`docs/USAGE.md` 说明管理视图和私聊命令；`deploy/README.md` 提供维护者可选的手工 Linux 部署；`docs/DATA-MAINTENANCE.md` 面向服务维护者；`docs/ARCHITECTURE.md` 保存核心实现细节；[安装与管理设计](docs/SETUP-DESIGN.md) 说明向导、供应商复用和管理视图的边界。

## 提交要求

保持 AstrBot 与 Hindsight 的公开接口边界，不修改宿主全局设置或其他插件。格式或身份规则变化需附迁移步骤。

提交前确认安装包包含全部运行模块、页面资产和部署材料，相关检查通过；涉及安装、升级或数据恢复的修改须验证对应使用流程。

## 后端兼容验证

CI 使用固定 Hindsight 1.1.0 / 1.2.0 镜像分别运行 `tools/check-model-protocols.py`，覆盖已支持的聊天、嵌入、重排协议和新版 schema 工具调用，不调用外部模型。

`python tools/run-backend-compat.py --output /tmp/新的测试目录` 使用独立配套服务、PostgreSQL 和两版 AstrBot，验证旧库升级、知识页、模型配置失败恢复、在途任务重启和 Bank 删除。需要 Docker 权限、本地已准备的官方镜像与测试依赖；模型与平台端为合成数据，不证明真实账号投递或模型质量。可用 `--baseline` 指定包含旧版安装材料的提交；`--scope deletion` 只验证新版删除路径，`--scope maintenance` 验证原生整合任务替代、解除保存和查询暂停、双宿主和删除，报告会标明范围。删除检查在专属库停止 Worker 后构造非空图与实体后台任务队列，记录各表前后计数，避免被后台排空掩盖缺陷。工具结束后清理自己创建的资源，证据与数据库备份保留在指定目录，请妥善保管。

## 修改与反馈

`python tools/run-feedback-e2e.py --output /tmp/新的测试目录` 使用独立的真实 AstrBot/Hindsight、原生 Dashboard 与合成平台/模型验证知识页反馈、0.2.6 升级、外部控制目录拒绝、停机迁移和旧业务快照恢复。支持 `--astrbot-version 4.29.1|4.29.4` 与 `--hindsight-version 1.1.0|1.2.0`，不调用外部模型，不使用真实 QQ 账号。需要 Docker、本地官方镜像及测试依赖；结束后清理自身容器和数据卷，证据保留在指定目录。

添加 `--scope single-page` 验证 0.2.7 升级后的单页引用与默认值、独立页面预算、共享 token/字符边界、可配置读取子限时及原生页面长度与索引提示。页面包装与最终临时聊天请求一并取证，覆盖整页使用和整页跳过。

提交问题时提供版本、复现步骤、预期、实际结果和脱敏日志。不要提交账号凭据、真实聊天记录、运行数据库或本地环境文件。

恢复业务快照不得覆盖更新的独立控制记录。
