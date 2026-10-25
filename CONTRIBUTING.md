# 开发与贡献

## 目录

- 根目录 Python 模块：AstrBot 接入、身份权限、记忆材料、队列和后端客户端。
- `deploy/`：独立 Hindsight/PostgreSQL 部署模板。
- `tests/`：可靠写入、隔离、用户控制、导出、更正与双方历史的既有回归。
- `tools/`：安装包元数据、文档链接和文件边界检查。

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
