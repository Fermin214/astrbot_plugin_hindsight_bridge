# Windows 原生部署

在 Windows 11 x64 上通过 AstrBot 官方启动器或独立 Python 3.12 环境运行 AstrBot 时，在 **记忆管理 → 插件设置** 选择 **连接已有服务**，接入本机或远程的 Hindsight 1.1.0 / 1.2.0。

## 本机安装 Hindsight

下面的命令在 Windows PowerShell 中运行。Hindsight 建议安装在独立 Python 虚拟环境中，通过内嵌的 `pg0` 启动本地 PostgreSQL（首次启动需要时间下载数据库与模型；长期大规模运行也可外接独立 PostgreSQL 14+ 与 pgvector）。

```powershell
$memoryRoot = Join-Path $env:LOCALAPPDATA 'AstrBot Memory'
New-Item -ItemType Directory -Force -Path "$memoryRoot" | Out-Null
Set-Location -LiteralPath "$memoryRoot"
py -3.12 -m venv ".venv"
& ".\.venv\Scripts\python.exe" -m pip install "hindsight-api==1.2.0"
```

在 `$memoryRoot` 目录创建 `.env`（**保存为 UTF-8 无 BOM 格式**）。下面以 OpenAI 对话、本地中文向量与本地重排为例；使用 `& ".\.venv\Scripts\python.exe" -c "import secrets; print(secrets.token_hex(32))"` 为服务 API 密钥生成随机字符串（不要与模型密钥相同）：

```dotenv
HINDSIGHT_API_DATABASE_URL=pg0://astrbot-memory
HINDSIGHT_API_WORKER_ID=astrbot-memory-worker
HINDSIGHT_API_OPERATION_RETENTION_DAYS=0
HINDSIGHT_API_TENANT_EXTENSION=hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
HINDSIGHT_API_TENANT_API_KEY=替换为服务API密钥
HINDSIGHT_API_LLM_PROVIDER=openai
HINDSIGHT_API_LLM_API_KEY=替换为模型供应商密钥
HINDSIGHT_API_LLM_MODEL=gpt-4o-mini
HINDSIGHT_API_EMBEDDINGS_PROVIDER=local
HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=BAAI/bge-small-zh-v1.5
HINDSIGHT_API_RERANKER_PROVIDER=local
```

> **路径编码提醒**：`PG0_HOME` 必须使用**只含纯 ASCII 字符**的绝对路径（如 `C:\AstrBotMemory\pg0`），避免路径带中文时 PostgreSQL 初始化报 UTF-8 错误；AstrBot 本身的数据目录可以含中文或空格。按 `Ctrl+C` 并等待进程正常退出即可停机。

启动服务：

```powershell
$memoryRoot = Join-Path $env:LOCALAPPDATA 'AstrBot Memory'
Set-Location -LiteralPath "$memoryRoot"
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PG0_HOME = 'C:\AstrBotMemory\pg0'
& ".\.venv\Scripts\hindsight-api.exe" --host 127.0.0.1 --port 8888
```

如果已经有外部向量/重排服务，可改用轻量包 `hindsight-api-slim[embedded-db]==1.2.0`，配置方式见 Hindsight [模型配置文档](https://github.com/vectorize-io/hindsight/blob/v1.2.0/hindsight-docs/docs/developer/configuration.mdx)；使用独立 PostgreSQL 时将 `HINDSIGHT_API_DATABASE_URL` 改为对应连接串并按 [官方安装说明](https://github.com/vectorize-io/hindsight/blob/v1.2.0/hindsight-docs/docs/developer/installation.md) 启用 pgvector。

## 连接插件与 Control Plane

1. 打开另一个 PowerShell 窗口运行 `Invoke-RestMethod -Uri 'http://127.0.0.1:8888/health'` 确认服务就绪。
2. 在 AstrBot 安装 Bridge 后打开 **记忆管理 → 插件设置**，选择 **连接已有服务**：API 地址填 `http://127.0.0.1:8888`，API 密钥填 `.env` 中的 `HINDSIGHT_API_TENANT_API_KEY`，依次点击 **测试连接** 和 **保存连接**。
3. 进入 **用户与记忆** 按 [添加用户](INSTALL.md#三添加用户) 配好测试账号，在私聊中发送一条具体事实，等保存完成后开启新会话提问测试召回。

如需使用官方 Control Plane 浏览器面板（需要 Node.js），再开一个 PowerShell 窗口启动（网页登录密钥与 API 密钥分开设置）：

```powershell
$env:HINDSIGHT_CP_DATAPLANE_API_KEY = '替换为服务API密钥'
$env:HINDSIGHT_CP_ACCESS_KEY = '替换为页面登录密钥'
npx.cmd -y '@vectorize-io/hindsight-control-plane@1.2.0' --hostname 127.0.0.1 --port 9999 --api-url 'http://127.0.0.1:8888'
```

浏览器打开 `http://127.0.0.1:9999` 输入页面登录密钥，并把该地址填回插件的 **Hindsight 管理地址**。

| 部署组合 | 插件 API 地址 | Hindsight 管理地址 |
| --- | --- | --- |
| AstrBot 与 Hindsight 均在 Windows 本机 | `http://127.0.0.1:8888` | `http://127.0.0.1:9999` |
| Hindsight 在本机 Docker | 宿主机映射的 API 端口 | 宿主机映射的网页端口 |
| Hindsight 在远程服务器 | AstrBot 可访问的 API 地址 | 浏览器可访问的地址或 SSH 隧道 |
| AstrBot 在容器或 WSL2 | 从容器/WSL2 内可访问的地址 | 从宿主机浏览器可访问的地址 |

## 备份与恢复

Bridge 和 Hindsight 需分别备份。恢复旧业务快照时保留最新的控制库副本，使已有的清空记录和账号权限继续生效。

在 AstrBot 所用的 Python 环境中执行备份（将 `$astrbotData` 与 `$backupRoot` 换成实际路径；使用自定义控制目录时同步修改 `--control-dir`）：

```powershell
$astrbotData = 'C:\AstrBot\Data'
$backupRoot = 'D:\AstrBot Backups'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
python "$astrbotData\plugins\astrbot_plugin_hindsight_bridge\scripts\backup.py" `
  --data-dir "$astrbotData\plugin_data\astrbot_plugin_hindsight_bridge" `
  --control-dir "$astrbotData\plugin_data\astrbot_plugin_hindsight_bridge_control" `
  --output "$backupRoot\bridge-$stamp" `
  --latest-control "$backupRoot\bridge-control-latest\control.sqlite3" `
  --backend-version 1.2.0
```

同时另存 AstrBot 插件配置和 `setup.json`。恢复插件见 [恢复业务数据](DATA-MAINTENANCE.md#恢复业务数据)。

### pg0 本机停机备份

1. 先记下实例数据目录：`& ".\.venv\Scripts\python.exe" -c "from pg0 import Pg0; print(Pg0(name='astrbot-memory').info().data_dir)"`（按本页路径通常位于 `$env:PG0_HOME\instances\astrbot-memory`）。
2. 在插件中停用 Bridge 并等待任务完成，关闭 Hindsight API 和 Control Plane，执行上面命令将 `data_dir` 替换为 `running` 确认返回 `False`。
3. 核对 `data\PG_VERSION` 后，将整个实例目录连同 `.env`、版本和模型记录一起复制到备份目录。
4. 恢复时按第 2 步停机，把当前实例目录改名保留以便回退，再把备份完整拷回原路径，启动服务核对记忆后再重新启用 Bridge。

如果需要在线逻辑备份或跨系统迁移，请在停写期间使用与 PostgreSQL 版本匹配的 `pg_dump` / `pg_restore`。

## 从 Linux 迁移 AstrBot 到 Windows

1. 若原 Linux 部署使用了配套服务且正处于升级暂停中，先在原环境核对升级结果并解除暂停（或选择 **暂不升级**），除非确保 Windows 上仍能连到原服务的管理接口。
2. 停止 AstrBot，把插件配置、业务库 `runtime.sqlite3` 与最新控制库 `control.sqlite3` 一起迁到 Windows，保持平台实例 ID 与用户账号配置不变（如设置过 `control_state_dir`，更新为 Windows 下的专属子目录路径）。
3. 如果继续连接原来的 Hindsight，保留配置中的 `service_id` 和密钥；若 API 地址有变化，在配置文件中更新 `api_url` 并把整数 `service_address_revision` 加 1。
4. 启动 AstrBot 后，在 **插件设置 → Hindsight 服务连接** 选择 **连接已有服务** 测试并保存地址，核对现有用户和记忆库是否正常。
