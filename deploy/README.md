# Hindsight 后端部署

完整首次安装顺序见 [安装说明](../INSTALL.md#从零安装-hindsight)。本目录只部署记忆服务与数据库，AstrBot 使用已有安装。

## 配置与启动

```bash
python3 init-env.py
# 编辑 .env，填写模型服务凭据与模型参数
docker compose up -d --build --wait
docker compose ps
```

Windows PowerShell 使用 `python init-env.py`。初始化脚本不会覆盖已有 `.env`，也不显示生成的秘密。

| 配置 | 作用 |
| --- | --- |
| `POSTGRES_PASSWORD` | 数据库密码；初始化脚本生成 URL 可用的随机十六进制值 |
| `HINDSIGHT_API_TENANT_API_KEY` | 插件连接 API 的 Key，管理面板在服务端也使用此 Key |
| `HINDSIGHT_CP_ACCESS_KEY` | 浏览器登录管理面板的访问 Key |
| `HINDSIGHT_API_WORKER_ID` | Worker 稳定身份，重建容器时沿用 |
| `HINDSIGHT_CLIENT_NETWORK` | AstrBot 加入的外部网络，默认 `astrbot_hindsight` |
| `HINDSIGHT_API_LLM_*` | 抽取及整合模型服务 |
| `HINDSIGHT_API_EMBEDDINGS_*` | 向量模型服务；维数须与模型输出一致 |

API 与管理面板默认只绑定 `127.0.0.1:8888`、`127.0.0.1:9999`。同一 Docker 网络中的插件使用 `http://hindsight:8888`。数据库没有宿主端口。

从 AstrBot 发送 `/hindsight status`，插件会验证无 Key 和错误 Key 均被拒绝，再检查正确 Key 能访问记忆 API。

## 数据与日常操作

PostgreSQL 数据存于 Compose 命名卷 `postgres_data`，默认完整名称为 `astrbot-hindsight_postgres_data`。`.env` 保存密码、服务身份及模型配置。备份需覆盖数据库和 `.env`，插件业务库与独立控制库按 [数据维护](../DATA-MAINTENANCE.md) 分别管理。

```bash
docker compose logs --tail 100 hindsight db
docker compose stop
docker compose start
```

更改 `.env` 后用 `docker compose up -d` 重建服务以加载配置。停止服务和重建容器都会沿用已有数据卷。数据库初始化密码只在空数据库首次建立时生效；已有数据迁移或改密按 PostgreSQL 流程执行。

后端固定保留操作历史，用于幂等提交与恢复核对。应监测数据库增长；不要通过直接删除卷或数据库文件回收空间。
