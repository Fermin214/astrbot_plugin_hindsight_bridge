# 开发与贡献

安装与日常操作见 [安装指南](docs/INSTALL.md) 和 [使用指南](docs/USAGE.md)。消息采集、队列、用户隔离和数据格式见 [实现说明](docs/ARCHITECTURE.md)。

## 目录

- `main.py`：AstrBot 插件入口、事件接入和用户命令。
- `core/`：身份权限、记忆材料、队列和后端客户端。
- `docs/`：安装、使用、兼容范围、数据维护和实现说明。
- `examples/`：插件配置示例。
- `deploy/`：独立 Hindsight/PostgreSQL 部署模板。
- `scripts/`：备份等数据维护脚本。
- `tests/`：可靠写入、隔离、用户控制、导出、更正与双方历史的既有回归。
- `tools/`：安装包元数据、文档链接和文件边界检查。

## 文档分工

README 介绍用途、安装入口和常用操作。`docs/INSTALL.md` 说明界面步骤及配置值的来源；`docs/USAGE.md` 按用户要做的事说明命令；`deploy/README.md` 说明后端部署；`docs/DATA-MAINTENANCE.md` 面向服务维护者；`docs/ARCHITECTURE.md` 保存实现细节。

安装流程以保存配置并开始聊天结束。模型效果、故障恢复和新版本回归由开发流程验证，不列为用户安装步骤。

## 本地检查

使用 Python 3.12。在仓库根目录执行：

```bash
python -m pip install -r requirements-dev.txt
python tests/run.py
python tools/check-package.py
```

核心回归不启动 AstrBot 或 Hindsight，不调用模型服务。实际集成另用 Linux 上的 AstrBot 4.29.1 和独立 Hindsight 1.1.0，记录具体执行器、平台、模型服务及数据版本。

涉及采集、临时注入或生命周期的改动，通过正式插件管理器安装与重载，检查最终模型请求和持久历史。涉及网络故障、遗忘或恢复的改动，使用隔离账号、Bank 和数据副本覆盖相关边界。

## 修改与反馈

提交问题时提供版本、复现步骤、预期、实际结果和脱敏日志。不要提交账号凭据、真实聊天记录、运行数据库或本地环境文件。

保持 AstrBot 与 Hindsight 的公开接口边界。格式或身份规则变化需附迁移步骤，恢复业务快照不得覆盖更新的独立控制记录。
