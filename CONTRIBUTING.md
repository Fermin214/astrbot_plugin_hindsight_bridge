# 贡献指南

安装与日常操作见 [安装指南](docs/INSTALL.md) 和 [使用指南](docs/USAGE.md)。消息采集、队列、用户隔离和数据格式见 [实现说明](docs/ARCHITECTURE.md)。

## 目录

- `main.py`：AstrBot 插件入口、事件接入和用户命令。
- `core/`：身份权限、记忆材料、队列、后端客户端，以及管理 API、供应商适配与 Linux 安装命令生成。
- `views/memory/`：AstrBot 内「运行管理」和「设置」两个标签页的 HTML、JavaScript 和样式；运行管理包含服务状态与用户记忆，设置包含安装、模型配置、使用者和卸载。
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

## 修改与反馈

提交问题时提供版本、复现步骤、预期、实际结果和脱敏日志。不要提交账号凭据、真实聊天记录、运行数据库或本地环境文件。

恢复业务快照不得覆盖更新的独立控制记录。
