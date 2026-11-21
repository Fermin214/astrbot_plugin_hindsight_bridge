# 部署 Hindsight 记忆服务

Hindsight 负责记忆的存储、整理与检索，Bridge 通过 API 连接它。大多数 Linux 用户直接使用 **Hindsight Bridge 插件详情 → 记忆管理 → 插件设置** 中的一键安装向导即可，详细步骤见 [安装指南](../docs/INSTALL.md)。

如果在插件里走配套安装：在 **插件设置** 生成安装命令并在运行 AstrBot 的 Linux 主机执行，回到设置页选择模型后点击 **保存并连接**，最后去 **用户与记忆** 添加使用者并确认身份（宿主机需已装好 Docker Engine 与 Compose v2，且执行用户拥有 Docker 权限）。

下文除了说明配套部署的端口、网络和登录密钥外，也提供一套独立的 **手工 Linux Docker 部署模板**（手工部署完成后，在插件设置选择「连接已有服务」并填入地址与密钥即可）。

## 配套部署目录与网络

安装向导生成的文件存放在执行用户的 `~/.local/share/astrbot-memory-<id>/`（`<id>` 为服务编号，也可通过环境变量 `HINDSIGHT_INSTALL_ROOT` 指定其他根目录；重装或卸载时必须沿用同一根目录）。

安装器会自动识别 AstrBot 的容器网络并调整配套服务的连接方式：

| AstrBot 运行方式 | 插件 API 地址 | 模型配置接口 | 官方 Control Plane 服务器本地入口 |
| --- | --- | --- | --- |
| Linux 原生 | 主机回环 `8888` | 主机回环 `8889` | 主机回环 `9999` |
| Docker 自定义 bridge | 同网络 `memory-<id>:8888` | 同网络 `memory-<id>:8766` | 主机回环 `9999` |
| Docker 默认 bridge | 网桥网关 `8888` | 网桥网关 `8889` | 主机回环 `9999` |
| Docker host | 主机回环 `8888` | 主机回环 `8889` | 主机回环 `9999` |

若需要指定加入某个已连接的 Docker 桥接网络，可在执行安装命令前先运行 `export HINDSIGHT_ASTRBOT_NETWORK=网络名`。容器重建后连接地址会保持不变。

数据库运行在内部隔离网络中不对外暴露端口；API、模型管理接口和网页管理面板均受独立密钥保护。注意：**Hindsight 所调用的模型服务地址必须能从配套服务容器内部访问到**。

默认端口为 `8888`（API）、`8889`（模型管理）、`9999`（网页管理面板）。如果宿主机端口冲突，安装器不会自动改端口，需由管理员调整安装状态中的 `api_port`、`management_port`、`control_plane_port`（及已有 `.env` 里的 `ASTRBOT_MEMORY_API_PORT`、`ASTRBOT_MEMORY_MANAGEMENT_PORT`、`ASTRBOT_MEMORY_CP_PORT`），再从 **插件设置 → 配套服务运维 → 重新部署** 生成命令。重新部署会保留已有数据库、模型设置和凭据。

### 安装目录与登录密钥

配套部署的网页登录密钥保存在安装目录的 `.env`（目录权限 `0700`，`.env` 权限 `0600`），用安装时同一用户执行以下命令即可读出（`<id>` 替换为安装输出中的服务编号）：

```bash
sed -n 's/^HINDSIGHT_CP_ACCESS_KEY=//p' "${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}/astrbot-memory-<id>/.env" | tr -d '\047\042'
```

如果是按本文下方手工部署的，则在 `deploy/` 目录执行：

```bash
sed -n 's/^HINDSIGHT_CP_ACCESS_KEY=//p' .env | tr -d '\047\042'
```

密钥分工：`HINDSIGHT_CP_ACCESS_KEY` 用于浏览器登录官方面板，仅由管理员掌握；`HINDSIGHT_API_TENANT_API_KEY` 用于服务端 API 鉴权；`ASTRBOT_MEMORY_MANAGEMENT_KEY` 用于配套模型配置接口。

---

## 手工部署准备条件

手工部署包含 **Hindsight 1.2.0 Slim** 与 **PostgreSQL 17**（含 pgvector 与 PGroonga 扩展），通过远端 API 调用模型，不需要本地显卡：

- 一台装有 Docker Engine 和 Compose v2 的 Linux 主机
- Git 和 Python 3（仅用于下载模板和生成 `.env`）
- 一个对话模型服务（接口地址、API Key、模型名）
- 一个向量模型服务（接口地址、API Key、模型名与输出维数；模板默认以阿里云百炼 `text-embedding-v4` / `1024` 维为例）

> 如果已经有自行部署的 Hindsight 1.1.0，也可以直接连接使用；配套服务从 1.1.0 升至 1.2.0 的流程见 [配套服务升级](../docs/DATA-MAINTENANCE.md#升级-hindsight-服务)。

## 一、下载部署文件

```bash
git clone https://github.com/Fermin214/astrbot_plugin_hindsight_bridge.git
cd astrbot_plugin_hindsight_bridge/deploy
```

后续命令均在 `deploy` 目录执行。

## 二、生成配置

```bash
python3 init-env.py
```

脚本会生成权限为 `0600` 的 `.env` 文件以及随机密码和密钥（再次运行会沿用已有 `.env`，不会覆盖现有凭据与端口）。编辑 `.env`（带点号的隐藏文件）填入你的模型信息：

### 对话模型

| 配置名 | 说明 |
| --- | --- |
| `HINDSIGHT_API_LLM_PROVIDER` | 接口类型，模板默认为 `deepseek`；OpenAI 兼容接口填 `openai` |
| `HINDSIGHT_API_LLM_BASE_URL` | 模型 API 地址 |
| `HINDSIGHT_API_LLM_MODEL` | 模型名称 |
| `HINDSIGHT_API_LLM_API_KEY` | 模型 API Key |

### 向量模型

如使用模板默认的百炼向量服务，只需填写 `HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY`：

| 配置名 | 模板默认值 |
| --- | --- |
| `HINDSIGHT_API_EMBEDDINGS_PROVIDER` | `openai` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL` | `text-embedding-v4` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS` | `1024` |
| `HINDSIGHT_API_EMBEDDINGS_OPENAI_BATCH_SIZE` | `10` |

换成其他向量模型时同步修改上述地址、模型名和维数（注意记忆库一旦开始写入，更换向量模型或维数需同步迁移已有向量数据）。重排模型默认使用 `HINDSIGHT_API_RERANKER_PROVIDER=rrf`，无需额外配置。

### 自动生成的关键变量

| 配置名 | 用途 |
| --- | --- |
| `POSTGRES_PASSWORD` | PostgreSQL 数据库密码 |
| `HINDSIGHT_API_TENANT_API_KEY` | **稍后填入 AstrBot 插件的 API 密钥** |
| `HINDSIGHT_CP_ACCESS_KEY` | 浏览器打开官方 Control Plane 的登录密钥 |
| `HINDSIGHT_API_WORKER_ID` | 后台任务标识，重建容器时需保留 |

## 三、启动记忆服务

```bash
docker compose up -d --build --wait
```

首次启动会构建带扩展的数据库镜像并拉起服务。启动失败可通过以下命令排查：

```bash
docker compose logs --tail 100 hindsight db
```

默认将 `8888`（API）和 `9999`（网页管理）绑定在主机 `127.0.0.1`。如端口已被占用，可修改 `.env` 中的 `HINDSIGHT_API_PORT` 或 `HINDSIGHT_UI_PORT` 后重新执行启动命令。

## 让 AstrBot 容器连接 Hindsight

**只有 AstrBot 同样跑在 Docker 容器中时才需要这一步。** 把 AstrBot 加入模板创建的 `astrbot_hindsight` 网络：

在 AstrBot 的 Compose 配置中合并以下片段（`astrbot` 换成实际服务名；如改过 `HINDSIGHT_CLIENT_NETWORK` 则对应调整 `name`）：

```yaml
services:
  astrbot:
    networks:
      - default
      - hindsight

networks:
  hindsight:
    external: true
    name: astrbot_hindsight
```

在 AstrBot Compose 文件所在目录执行 `docker compose up -d` 重建网络后，插件里的 API 地址填写 `http://hindsight:8888`（宿主机原生运行 AstrBot 时填 `http://127.0.0.1:8888`）。

## 四、回到 AstrBot 连接插件

在 **插件设置 → Hindsight 服务连接** 选择 **连接已有服务**，填入 API 地址与 `.env` 里的 `HINDSIGHT_API_TENANT_API_KEY`，测试并保存即可（如果 AstrBot 位于另一台机器，请为 API 端口配置可达的 HTTPS 入口）。

## 五、打开官方 Control Plane

在 **插件设置 → Hindsight 服务连接** 复制 **Hindsight 管理地址** 用浏览器打开，登录密钥填 `.env` 中的 `HINDSIGHT_CP_ACCESS_KEY`。在 **用户与记忆** 顶部复制用户的 **Bank ID** 即可查找和管理对应记忆（Bank 在首次保存聊天时自动创建；日常编辑直接在官方面板进行，彻底清空整库则使用插件里的 **清空记忆**，详见 [记忆库恢复](../docs/DATA-MAINTENANCE.md#在官方面板修改记忆)）。

由于服务上的网页面板默认监听在 `127.0.0.1:9999`，从自己的电脑浏览器访问需通过下面两种方式之一（API 地址保持不变）：

### 无域名：SSH 隧道

在自己的电脑终端执行并保持开启（`9999` 被占用时把第一个端口改成如 `19999`，管理地址同步填 `http://127.0.0.1:19999`）：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:9999:127.0.0.1:9999 your-user@your-server
```

随后在插件「Hindsight 管理地址」填入 `http://127.0.0.1:9999`。

### 有域名：独立 HTTPS 反向代理

在同主机为 `127.0.0.1:9999` 分配一个独立的域名根路径（不要挂在 AstrBot 的子路径下），例如使用 Caddy：

```caddyfile
memory.example.com {
    reverse_proxy 127.0.0.1:9999
}
```

配置好 HTTPS 证书后，管理地址填写 `https://memory.example.com`。只需反代网页端口 `9999`（官方页面在容器内部已通过 `HINDSIGHT_CP_DATAPLANE_API_URL` 连接 API，浏览器端无需再填 API 密钥）。如果反向代理本身跑在 Docker 容器里，请确保它能路由到宿主机的 `9999` 端口。

## 服务状态与日志

在 `deploy` 目录常用运维命令：

| 操作 | 命令 |
| --- | --- |
| 查看运行状态 | `docker compose ps` |
| 查看最近日志 | `docker compose logs --tail 100 hindsight db` |

手工模板的数据库存放在 Docker 卷 `astrbot-hindsight_postgres_data`（模板默认设置 `HINDSIGHT_API_OPERATION_RETENTION_DAYS=0` 以保留后台操作记录用于恢复核对）。插件自身的备份恢复见 [备份与恢复](../docs/DATA-MAINTENANCE.md)。

## 卸载边界

使用面板配套安装时，可在 **插件设置 → 配套服务运维 → 卸载** 生成专属卸载命令，在原主机执行后会核对安装编号与目录，清除专属容器、数据卷、专用网络及安装目录。手工部署的服务请直接通过 `docker compose down` 按需清理。
