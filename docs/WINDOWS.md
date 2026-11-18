# Windows 原生部署

在 Windows 11 x64 上使用 AstrBot 官方启动器或独立的 Python 3.12 环境运行 AstrBot。插件在「记忆管理 → 插件设置」连接 Hindsight。已有 Hindsight 1.1.0 或 1.2.0 时，直接使用其 API 地址和密钥。

## 本机安装 Hindsight

以下命令可在 Windows PowerShell 中运行。Hindsight 单独使用一个 Python 虚拟环境，内嵌的 pg0 会启动本地 PostgreSQL。首次启动需要时间下载数据库和模型，模型也会占用磁盘和内存。长期运行可使用独立 PostgreSQL 14 或以上版本及 pgvector。

```powershell
$memoryRoot = Join-Path $env:LOCALAPPDATA 'AstrBot Memory'
New-Item -ItemType Directory -Force -Path "$memoryRoot" | Out-Null
Set-Location -LiteralPath "$memoryRoot"
py -3.12 -m venv ".venv"
& ".\.venv\Scripts\python.exe" -m pip install "hindsight-api==1.2.0"
```

在该目录将 `.env` 保存为 UTF-8 无 BOM。下面以 OpenAI 记忆处理、本地中文向量模型和本地重排为例。把模型密钥和服务 API 密钥替换成不同的值。服务 API 密钥可用 `& ".\.venv\Scripts\python.exe" -c "import secrets; print(secrets.token_hex(32))"` 生成。每套服务使用不同的 pg0 实例名和 Worker ID，重启时沿用同一组值。

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

每次从同一目录启动 API，等待服务启动完成。需要停止时，在 API 窗口按 `Ctrl+C` 并等进程正常退出。

```powershell
$memoryRoot = Join-Path $env:LOCALAPPDATA 'AstrBot Memory'
Set-Location -LiteralPath "$memoryRoot"
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PG0_HOME = Join-Path $memoryRoot 'pg0'
& ".\.venv\Scripts\hindsight-api.exe" --host 127.0.0.1 --port 8888
```

已有向量和重排服务时，可在独立的空虚拟环境安装 `hindsight-api-slim[embedded-db]==1.2.0`，继续使用 pg0，并按 Hindsight [模型配置](https://github.com/vectorize-io/hindsight/blob/v1.2.0/hindsight-docs/docs/developer/configuration.mdx) 填写外部供应商。使用独立 PostgreSQL 时，将 `HINDSIGHT_API_DATABASE_URL` 换成该库的连接串，并按 [官方安装说明](https://github.com/vectorize-io/hindsight/blob/v1.2.0/hindsight-docs/docs/developer/installation.md) 准备向量扩展。

## 连接插件和 Control Plane

1. 在另一窗口执行 `Invoke-RestMethod -Uri 'http://127.0.0.1:8888/health'`，确认 API 已启动。
2. 在 AstrBot 安装 Bridge，打开「记忆管理 → 插件设置」。API 地址填 `http://127.0.0.1:8888`，密钥填 `.env` 中的 `HINDSIGHT_API_TENANT_API_KEY`。先点击「测试连接」，再点击「保存连接」。测试会验证正确密钥可以访问，空密钥与错误密钥会被拒绝。
3. 在「用户与记忆」按[安装指南](INSTALL.md#三添加用户)添加一个测试用户。私聊说出一条包含名称、日期或编号的中文事实，等保存完成后开启新会话提问，确认召回得到对应内容。

如需浏览记忆和编辑知识页，在装有 Node.js 的另一个 PowerShell 窗口运行官方 Control Plane。页面密钥与 API 密钥分开生成和填写。

```powershell
$env:HINDSIGHT_CP_DATAPLANE_API_KEY = '替换为服务API密钥'
$env:HINDSIGHT_CP_ACCESS_KEY = '替换为页面登录密钥'
npx.cmd -y '@vectorize-io/hindsight-control-plane@1.2.0' --hostname 127.0.0.1 --port 9999 --api-url 'http://127.0.0.1:8888'
```

浏览器打开 `http://127.0.0.1:9999`，输入页面登录密钥。在 Bridge 的「Hindsight 管理地址」中保存同一地址。

| 部署位置 | Bridge API 地址 | Hindsight 管理地址 |
| --- | --- | --- |
| 两个服务都在本机 Windows | `http://127.0.0.1:8888` | `http://127.0.0.1:9999` |
| API 在本机 Docker | Windows 可访问的宿主发布端口 | 浏览器可访问的宿主页面端口 |
| API 在远程主机 | AstrBot 进程可访问的 API 地址 | 浏览器可访问的页面地址或隧道地址 |
| AstrBot 在容器或 WSL2 | 从 AstrBot 所在环境核验可达的地址 | 从管理员浏览器所在网络核验可达的地址 |

## 备份与恢复

分别备份 Bridge 和 Hindsight。恢复旧 Bridge 业务快照时使用最新的控制库副本，确保清空记录、访问权限和 Bank 代次仍然生效。

用 AstrBot 的 Python 环境运行以下命令。将 `$astrbotData` 换成实际数据目录，将 `$backupRoot` 换成独立的备份位置。如使用自定义控制目录，替换 `--control-dir`。

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

单独备份 AstrBot 插件配置和 `setup.json`。恢复插件按[恢复业务数据](DATA-MAINTENANCE.md#恢复业务数据)操作。

### pg0 同环境停机备份

1. 在 API 使用的目录和 `PG0_HOME` 下查询 PostgreSQL 实际数据路径：`& ".\.venv\Scripts\python.exe" -c "from pg0 import Pg0; print(Pg0(name='astrbot-memory').info().data_dir)"`。
2. 停用 Bridge 并停止其他写入，等 Hindsight 中的任务完成。退出 API 和 Control Plane，将上一步命令中的 `data_dir` 改为 `running` 并确认输出 `False`。本文示例的实例目录是 `$env:PG0_HOME\instances\astrbot-memory`。核对其中的 `data\PG_VERSION`，再复制完整实例目录、API 与 Control Plane 配置到备份位置，并记录各软件版本与模型标识。
3. 在版本和实例路径一致的环境恢复时，先按第 2 步停机，将现有实例目录改名留作回退，再把完整备份复制回原路径。启动 API 并核对原 Bank、模型和留存的记忆，再启用 Bridge。

在线逻辑备份或跨环境迁移时，在 PostgreSQL 保持运行且业务停写的时段使用匹配实例的 `pg_dump` 和 `pg_restore`。退出 Hindsight API 可能同时停止 pg0。升级前固定源、目标版本并完成备份，按 Hindsight 的迁移步骤操作，启动后检查原 Bank 和待处理任务。

从 Linux 迁移 AstrBot 时可先沿用原 Hindsight，迁移插件配置、业务库及最新控制库，并保存「连接已有服务」。配套升级造成的暂停需先在原 Linux 部署环境核对并解除。更换后端数据库或向量模型时按后端迁移处理。
