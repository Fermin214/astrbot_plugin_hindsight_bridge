# 部署 Hindsight 记忆服务

Hindsight 负责保存、整理和查找记忆，AstrBot 插件通过 API 连接它。普通安装使用 **Hindsight Memory 插件详情 → 记忆管理 → 设置**，步骤见 [安装指南](../docs/INSTALL.md)。

在设置中生成安装命令，在运行 AstrBot 的 Linux 主机终端执行一次。安装器自动生成密钥、部署服务并连接容器网络；随后回到设置，选择聊天、嵌入和可选重排模型，点击 **验证并保存模型**，再添加 QQ 机器人与使用者账号，点击 **完成设置** 即可。安装命令不包含模型配置或模型密钥。主机需已安装 Docker Engine 和 Compose v2，执行账号需能使用 Docker。

下文是服务维护者可选的 **手工 Linux 部署方式**，适合自行维护后端模型、网络和数据库的场景。完成后，在设置中选择「连接已有 Hindsight」，填写服务 **API 地址**和 **连接密钥**。

向导生成的配套服务文件位于执行账号的 `~/.local/share/astrbot-memory-<id>/`，`<id>` 为服务编号，准确路径由安装命令显示。设置 `HINDSIGHT_INSTALL_ROOT` 可改变安装根目录；重新部署和卸载必须使用同一根目录。配套部署使用独立的 Compose 项目和数据卷。

| AstrBot 运行方式 | 插件 API 地址 | 模型配置接口 | 官方页面的服务器入口 |
| --- | --- | --- | --- |
| Linux 原生 | 主机环回 `8888` | 主机环回 `8889` | 主机环回 `9999` |
| Docker 桥接网络 | 同网络 `memory-服务编号:8888` | 同网络 `memory-服务编号:8766` | 主机环回 `9999` |

Docker 安装器只为 Hindsight 加入 AstrBot 的一个可出站自定义桥接网络。需要选择时，在执行生成命令的终端先设置 `export HINDSIGHT_ASTRBOT_NETWORK=网络名`；网络必须已连接 AstrBot。模型服务地址在该网络及 Hindsight 的出站网络中需要可达。

API 地址供 AstrBot 连接服务；“官方管理地址”供管理员浏览器访问。容器名称与 `8766` 接口用于插件通信，不能作为浏览器管理地址。两种部署方式都需要 HTTPS 域名反向代理或 SSH 隧道，才能从管理员电脑打开服务器面板；浏览器里的 `127.0.0.1` 指管理员自己的电脑。面板在模型配置成功后随 Hindsight 启动，安装结束后先完成模型设置。

重新部署沿用数据库、模型配置卷和 `.env` 中的已有凭据，只为缺少官方页面配置的安装补入独立登录密钥和端口。数据库密码、API Key、模型管理密钥、后台任务身份或已保存端口与生成命令不一致时，安装器会报错，需从对应插件的 **设置 → 服务维护** 重新生成命令。

端口被其他进程占用时，先释放端口。需要更换端口时，由维护者调整安装状态中的 `api_port`、`management_port`、`control_plane_port` 后重新生成命令；默认值分别为 `8888`、`8889`、`9999`。已有安装的 `.env` 中，对应的 `ASTRBOT_MEMORY_API_PORT`、`ASTRBOT_MEMORY_MANAGEMENT_PORT`、`ASTRBOT_MEMORY_CP_PORT` 也需同步修改。安装器不会自动换端口。

下文的端口、服务名和数据卷名称适用于手工部署模板；维护配套服务时应使用其安装目录中的配置。

### 安装目录与登录密钥

配套服务的网页登录密钥自动生成，保存在安装目录的 `.env`，重新部署时沿用。目录权限为 `0700`，`.env` 权限为 `0600`。在安装时使用的同一服务器账号下执行：

```bash
sed -n 's/^HINDSIGHT_CP_ACCESS_KEY=//p' "${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}/astrbot-memory-<id>/.env" | tr -d '\047\042'
```

把 `<id>` 替换为安装输出中的服务编号；默认文件路径示例为 `~/.local/share/astrbot-memory-<id>/.env`。自定义过安装目录时，按安装输出替换路径。插件页也提供按服务编号生成的读取命令，这条命令需在服务器执行。将输出的密钥填入官方网页登录框。

手工部署的密钥也在其 `deploy/.env` 中，可在该目录执行：

```bash
sed -n 's/^HINDSIGHT_CP_ACCESS_KEY=//p' .env | tr -d '\047\042'
```

连接已有服务时，向维护者获取网页登录密钥。`HINDSIGHT_CP_ACCESS_KEY` 用于网页登录，`HINDSIGHT_API_TENANT_API_KEY` 用于服务 API，`ASTRBOT_MEMORY_MANAGEMENT_KEY` 用于模型配置接口。官方管理地址 `control_plane_url` 填写浏览器中的面板首页地址。

## 准备条件

- 一台安装了 Docker Engine 和 Compose v2 的 Linux 主机。
- Git 和 Python 3，用于下载部署文件和生成初始配置。
- 一个供 Hindsight 整理记忆使用的对话模型服务：API Key、接口地址和模型名。
- 一个向量模型服务：API Key、接口地址、模型名和输出维数。仓库模板使用百炼 `text-embedding-v4`，维数为 `1024`。

手工部署将这些模型配置填写到 Hindsight 的 `.env` 中。使用配套安装器时，服务安装完成后再在设置中选择模型；插件后台读取所选 AstrBot 供应商的地址和 Key，检查模型连接后应用到配套服务，无需重复填写。

服务包含 Hindsight **1.1.0 Slim** 与 PostgreSQL **17**，数据库构建包含 pgvector 和 PGroonga。模型通过远程接口调用，模板不需要本地显卡；API 调用费用按所用模型服务计费。

服务端支持 Linux；管理员电脑可以使用 Windows、macOS 或 Linux。

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

脚本会生成权限为 `0600` 的 `.env` 文件，并分别生成数据库密码、服务 API Key 和管理面板登录 Key。再次执行会提示使用已有 `.env`，凭据、Worker 身份和端口继续沿用；需要修改时直接编辑该文件。

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

如果启动提示端口已占用，修改 `.env` 中的 `HINDSIGHT_API_PORT` 或 `HINDSIGHT_UI_PORT`，再执行启动命令。这两个变量只改变主机映射，容器内的 API 和 CP 仍分别使用 `8888`、`9999`。保留已有密码、Key、Worker 和数据卷。

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

## 五、打开官方 Control Plane

官方管理页面使用 Hindsight 1.1.0 原生 Control Plane，提供浏览、搜索、更正、删除、导入和导出。在插件页 **运行管理 → 用户记忆 → 查看和管理记忆** 中复制官方管理地址和记忆库编号，登录后找到对应 Bank。Bank 是每个用户的记忆库，首次保存聊天时自动创建，无需在官方页面另建。

浏览、搜索和导出无需维护。修改、删除或导入前，先在插件页开始维护，等页面显示“可以修改记忆”后再操作，完成后结束维护。待保存记录的处理及整库删除后的恢复见 [数据维护](../docs/DATA-MAINTENANCE.md#官方内容维护)。

官方页面在服务器上的入口为 `http://127.0.0.1:9999`。配套安装和手工 Compose 都提供此入口；改过网页主机端口时使用修改后的值。管理员电脑上的浏览器不能直接通过这个地址访问服务器，需要下面的 SSH 隧道或 HTTPS 反向代理。登录使用 `.env` 中的 `HINDSIGHT_CP_ACCESS_KEY`，获取命令见 [安装目录与登录密钥](#安装目录与登录密钥)。

### 无域名：SSH 隧道

服务部署在远程 Linux 主机时，在管理员电脑的终端执行；Linux 原生 AstrBot 与 Docker AstrBot 使用相同步骤，不需要域名：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:9999:127.0.0.1:9999 your-user@your-server
```

把 `your-user@your-server` 换成服务器登录信息，保持终端打开，在自己电脑访问 `http://127.0.0.1:9999`。此时电脑的本地端口经隧道连接服务器面板；关闭终端后隧道断开。网页登录密钥仍从服务器安装目录的 `.env` 读取。

命令中第一个 `9999` 是管理员电脑的端口，最后一个是服务器 CP 主机端口。电脑的 `9999` 已占用时，第一个改为 `19999`，浏览器地址也改为 `http://127.0.0.1:19999`；服务器使用其他端口时修改最后一个值。将实际浏览器地址填入 `control_plane_url`，不改动插件的 API 地址。

### 有域名：独立 HTTPS 入口

反向代理与 Hindsight 位于同一台 Linux 主机时，为官方面板分配独立域名，例如 `memory.example.com`。保留 CP 的环回绑定，反向代理接入服务器 `127.0.0.1:9999`，公开入口使用 HTTPS。Caddy 可使用如下独立站点：

```caddyfile
memory.example.com {
    reverse_proxy 127.0.0.1:9999
}
```

将域名解析到服务器，按所用反向代理配置证书和 HTTPS，官方管理地址填写 `https://memory.example.com`，登录仍使用 `HINDSIGHT_CP_ACCESS_KEY`。AstrBot 使用自己的独立入口。反向代理若运行在容器内，它的 `127.0.0.1` 指向代理容器，需要单独配置可到达官方页面的网络或主机访问方式。

只代理官方页面的网页端口，API 与模型配置接口继续用于内部通信。官方页面通过容器内 `HINDSIGHT_CP_DATAPLANE_API_URL` 和服务 API Key 访问记忆服务，浏览器无需填写这个 Key。使用独立域名的根路径，例如 `https://memory.example.com`，不要放在 AstrBot 的子路径下。

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

## 卸载边界

配套安装在插件的 **设置 → 服务维护** 生成专属卸载命令。它核对安装身份、目录和 Compose 资源标签，仅删除该安装所属的容器、数据卷、专属网络和安装目录；这套服务中所有用户的记忆都会删除，无法恢复。共享的 AstrBot 网络、镜像、Docker、AstrBot 本身及外部已有 Hindsight 服务均独立管理。

手工 Compose 由部署者维护，在自己的部署目录运行 `docker compose stop` 可停止服务并保留数据。决定删除该手工部署的记忆数据前先完成备份，再核实 Compose 项目、卷名和归属；不要使用全局 Docker 清理命令。
