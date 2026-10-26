# 部署 Hindsight 记忆服务

Hindsight 负责保存、整理和查找记忆，AstrBot 插件通过 API 连接它。普通安装使用 AstrBot 插件详情中的 **记忆管理 → 设置**，步骤见 [安装指南](../docs/INSTALL.md)。

在设置中生成安装命令，在运行 AstrBot 的 Linux 主机终端执行一次。安装器自动生成密钥、部署服务并连接容器网络；随后回到设置，选择对话、嵌入和可选重排模型，点击「检查并保存模型」，再添加 QQ 机器人与使用者账号，点击「完成设置」即可。安装命令不包含模型配置或模型密钥。主机需已安装 Docker Engine 和 Compose v2，执行账号需能使用 Docker。

下文是服务维护者可选的 **手工 Linux 部署方式**，适合自行维护后端模型、网络和数据库的场景。完成后，在设置中选择「连接已有 Hindsight」，填写服务 **API 地址**和 **连接密钥**。

向导生成的配套服务文件位于执行账号的 `~/.local/share/astrbot-memory-服务编号/`，准确路径由安装命令显示。配套部署使用独立的 Compose 项目和数据卷，AstrBot 在 Docker 中时不开放主机 API 端口，也不开放独立管理网页端口。下文的端口、服务名和数据卷名称适用于手工部署模板；维护配套服务时应使用其安装目录中的配置。

## 准备条件

- 一台安装了 Docker Engine 和 Compose v2 的 Linux 主机。
- Git 和 Python 3，用于下载部署文件和生成初始配置。
- 一个供 Hindsight 整理记忆使用的对话模型服务：API Key、接口地址和模型名。
- 一个向量模型服务：API Key、接口地址、模型名和输出维数。仓库模板使用百炼 `text-embedding-v4`，维数为 `1024`。

手工部署将这些模型配置填写到 Hindsight 的 `.env` 中。使用配套安装器时，服务安装完成后再在设置中选择模型；插件后台读取所选 AstrBot 供应商的地址和 Key，检查模型连接后应用到配套服务，无需重复填写。

服务包含 Hindsight **1.1.0 Slim** 与 PostgreSQL **17**，数据库构建包含 pgvector 和 PGroonga。模型通过远程接口调用，模板不需要本地显卡；API 调用费用按所用模型服务计费。

首发支持 Linux，Windows 适配后续推出。Docker Desktop 仅用于开发和 Linux 容器验证，不作为用户部署方式。

## 一、下载部署文件

在安装 Docker 的主机上打开终端，执行：

```bash
git clone https://github.com/Fermin214/astrbot_plugin_hindsight_bridge.git
cd astrbot_plugin_hindsight_bridge/deploy
```

后面的命令都在这个 `deploy` 目录中执行。

## 二、生成配置

```bash
python3 init-env.py
```

脚本会生成 `.env` 文件，并填好数据库密码、服务 API Key 和管理面板登录 Key。提示 `.env already exists` 表示配置文件已存在，直接编辑已有文件即可。

用文本编辑器打开 `deploy/.env`；文件名以点开头，Linux 文件管理器可能需要打开“显示隐藏文件”才能看到。

### 填写对话模型

在 `.env` 中找到以下项目，按自己的模型服务修改：

| 配置名 | 填什么 |
| --- | --- |
| `HINDSIGHT_API_LLM_PROVIDER` | 服务类型。模板填 `deepseek`；OpenAI 兼容接口可填 `openai` |
| `HINDSIGHT_API_LLM_BASE_URL` | 模型服务提供的接口地址 |
| `HINDSIGHT_API_LLM_MODEL` | 账号可用的准确模型名 |
| `HINDSIGHT_API_LLM_API_KEY` | 这个模型服务的 API Key |

### 填写向量模型

使用模板中的百炼向量服务时，填写 `HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY`，其余向量项目如下：

| 配置名 | 模板值 |
| --- | --- |
| `HINDSIGHT_API_EMBEDDINGS_PROVIDER` | `openai`，表示使用兼容接口 |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL` | `text-embedding-v4` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS` | `1024` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE` | `10` |

使用其他服务时，地址、Key、模型名和维数一起改为该服务提供的值。已有记忆后更换向量模型或维数，需要迁移已有向量数据。

默认 `HINDSIGHT_API_RERANKER_PROVIDER=rrf`，无需额外配置第三个模型。

### 自动生成的值

这些值由脚本生成，首次部署保留即可：

| 配置名 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | 数据库密码 |
| `HINDSIGHT_API_TENANT_API_KEY` | **稍后复制到 AstrBot 插件里的 API Key** |
| `HINDSIGHT_CP_ACCESS_KEY` | 浏览器打开 Hindsight 管理面板时使用的登录 Key |
| `HINDSIGHT_API_WORKER_ID` | 后台任务身份，重建容器时沿用 |

保存 `.env`。密码和 Key 只写在自己的配置中。

## 三、启动记忆服务

```bash
docker compose up -d --build --wait
```

首次启动会下载镜像并构建数据库组件。命令成功结束后，服务已经启动；失败时查看：

```bash
docker compose logs --tail 100 hindsight db
```

API 默认使用主机的 `8888` 端口，网页管理面板使用 `9999`。二者默认只允许从这台主机访问，数据库不对外开放端口。

如果启动提示端口已占用，修改 `.env` 中的 `HINDSIGHT_API_PORT` 或 `HINDSIGHT_UI_PORT`，再执行启动命令。同一 Docker 网络中的 API 地址仍使用容器端口 `8888`。

## 让 AstrBot 容器连接 Hindsight

**只有 AstrBot 也运行在 Docker 容器中时需要这一步。** 两者在同一 Docker 主机时，把 AstrBot 加入记忆服务的网络。

找到用于启动 AstrBot 的 Compose 文件，在已有配置中合入以下内容：

```yaml
services:
  astrbot:
    # 在已有服务配置中增加 networks，其他设置继续沿用。
    networks:
      - default
      - hindsight

networks:
  hindsight:
    external: true
    name: astrbot_hindsight
```

这里的 `astrbot` 要与自己的服务名一致。已有 `networks` 时，在原列表中增加 `hindsight`，保留原有网络；顶层 `networks` 中也增加对应定义。`.env` 改过 `HINDSIGHT_CLIENT_NETWORK` 时，`name` 使用修改后的名称。

在 **AstrBot 的 Compose 文件所在目录**执行：

```bash
docker compose up -d
```

插件 API 地址填写 `http://hindsight:8888`。AstrBot 直接运行在主机上时，不需要修改网络，填写 `http://127.0.0.1:8888`；改过主机 API 端口时替换相应端口。

## 四、回到 AstrBot 安装插件

准备好以下内容：

| 要填到插件里的值 | 从哪里取得 |
| --- | --- |
| Hindsight API 地址 | 同网络容器填 `http://hindsight:8888`；主机原生 AstrBot 填 `http://127.0.0.1:8888` |
| Hindsight API Key | `.env` 中 `HINDSIGHT_API_TENANT_API_KEY=` 后面的值 |

按 [安装指南](../docs/INSTALL.md) 在 AstrBot 中从链接安装并填写配置。

AstrBot 位于另一台主机时，需要给它提供可访问的 HTTPS API 地址，具体网络入口由服务维护者配置。

## 可选：打开 Hindsight 管理面板

在部署主机的浏览器中打开 `http://127.0.0.1:9999`，输入 `.env` 中的 `HINDSIGHT_CP_ACCESS_KEY` 登录。

服务部署在远程 Linux 主机时，可在自己电脑的终端建立转发：

```bash
ssh -N -L 9999:127.0.0.1:9999 your-user@your-server
```

把 `your-user@your-server` 换成服务器登录信息，保持终端打开，再访问自己电脑的 `http://127.0.0.1:9999`。改过服务端网页端口时，替换命令中第二个 `9999`。

这个独立管理面板用于后端维护。日常查看、搜索、更正、导出、模式切换和完整遗忘都在 AstrBot 插件的「记忆管理」视图中完成。

## 日常维护

以下命令在本指南的 `deploy` 目录执行：

| 操作 | 命令 |
| --- | --- |
| 查看服务状态 | `docker compose ps` |
| 查看最近日志 | `docker compose logs --tail 100 hindsight db` |
| 停止服务 | `docker compose stop` |
| 再次启动 | `docker compose start` |
| 修改 `.env` 后加载新配置 | `docker compose up -d` |

数据库保存在 Docker 数据卷 `astrbot-hindsight_postgres_data` 中；自行改过 Compose 项目名时，卷名前缀也会变化。停止服务或重建容器会沿用已有数据。

完整备份需要包含数据库和 `.env`，以及插件数据。操作步骤见 [备份与恢复](../docs/DATA-MAINTENANCE.md)。数据库密码只在空数据库首次初始化时生效，已有数据库改密要使用 PostgreSQL 的密码修改流程。

模板设置 `HINDSIGHT_API_OPERATION_RETENTION_DAYS=0`，持续保留后台操作记录，供任务重试和数据恢复使用。磁盘维护通过数据库备份和管理工具进行，不直接删除数据卷。
