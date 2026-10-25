# 安装与配置

## 安装顺序

已有 Hindsight：直接阅读“连接已有 Hindsight”。首次部署：先部署后端、接通 AstrBot 网络，再安装和配置插件。

仓库地址：`https://github.com/Fermin214/astrbot_plugin_hindsight_bridge`。通过 AstrBot 插件管理器的链接安装入口安装，不需要把 Hindsight 安装进 AstrBot 的 Python 环境。

## 从零安装 Hindsight

参考后端使用 Linux Docker Engine 或 Docker Desktop 的 Linux 容器，以及 Docker Compose v2。组件为 Hindsight 1.1.0 Slim、PostgreSQL 17、pgvector 和 PGroonga。

在运行 Docker 的主机执行：

```bash
git clone https://github.com/Fermin214/astrbot_plugin_hindsight_bridge.git
cd astrbot_plugin_hindsight_bridge/deploy
python3 init-env.py
```

Windows PowerShell 使用 `python init-env.py`。脚本创建 `.env`，独立生成数据库密码、服务 API Key 和管理面板访问 Key；已有文件不会被覆盖。

打开生成的 `.env`，填写抽取/整合模型和向量服务的 API Key。模型服务、地址、模型名和向量维数必须与所用账号匹配。示例采用远程服务，默认重排使用 RRF；支持的其他 provider 按对应 Hindsight 版本的配置说明填写。

```bash
docker compose --env-file .env -f compose.yaml up -d --build --wait
docker compose ps
```

数据库和 Hindsight 应显示 healthy。首次构建需要下载基础镜像和 PostgreSQL 扩展，启动时需要连接模型服务。故障日志用 `docker compose logs --tail 100 hindsight db` 查看。

API 默认绑定主机回环端口 `8888`，管理面板绑定 `9999`。本机浏览器打开 `http://127.0.0.1:9999`，使用 `.env` 中的 `HINDSIGHT_CP_ACCESS_KEY` 登录。远程主机可通过 SSH 隧道访问：

```bash
ssh -N -L 9999:127.0.0.1:9999 your-user@your-server
```

管理面板使用访问 Key；插件使用 `HINDSIGHT_API_TENANT_API_KEY`。不要把这两个 Key 混用。

### 让 AstrBot 连接后端

后端创建的客户端网络默认名为 `astrbot_hindsight`。AstrBot 与后端运行在同一 Docker 主机时，在 AstrBot 的 Compose 中增加该网络，并为插件控制目录增加持久挂载：

```yaml
services:
  astrbot:
    volumes:
      - ./hindsight-control:/AstrBot/hindsight-control
    networks:
      - default
      - hindsight

networks:
  hindsight:
    external: true
    name: astrbot_hindsight
```

把这段合入已有 Compose，保留 AstrBot 的数据挂载和原有网络。服务名不是 `astrbot` 时替换为实际服务名。为 `hindsight-control` 创建 AstrBot 运行用户可读写的目录，再从 AstrBot 的 Compose 目录执行 `docker compose up -d`。

插件 API 地址填写 `http://hindsight:8888`。AstrBot 原生运行在宿主机时，填写 `http://127.0.0.1:8888`；不同主机之间使用可达且受保护的 HTTPS 地址。容器里的 `localhost` 指向容器自身。

后端持久数据、日常命令和认证检查见 [后端部署说明](deploy/README.md)。

## 连接已有 Hindsight

AstrBot 使用内置 `local` 执行器。关闭对话设置中的自动时间提示和用户身份提示（`provider_settings.datetime_system_prompt=false`、`provider_settings.identifier=false`），避免附加文本改变保存的用户正文，影响双方历史的原文关联。

1. 在 AstrBot 插件管理中选择通过链接安装，粘贴 `https://github.com/Fermin214/astrbot_plugin_hindsight_bridge`。
2. AstrBot 自动安装 `requirements.txt` 中的依赖；依赖失败时按日志提示在 AstrBot 使用的 Python 环境中安装，Docker 部署使用容器内的 Python。重载插件。
3. 打开插件配置，填写 Hindsight API 地址、Key 和固定的服务标识。后端启用 API Key 认证。
4. 在“允许使用记忆的平台账号”中逐项填写 AstrBot 的平台实例 ID 与对应用户 ID。空列表关闭全部账号。使用实例 ID，不填写平台显示名称。
5. 填写 IANA 时区，例如 `Asia/Shanghai`。容器部署把独立控制目录设为 `/AstrBot/hindsight-control` 并挂载持久目录。
6. 启用插件并重载。在允许范围内的 QQ 私聊发送 `/hindsight status`，连接应显示“可用，认证已启用”，执行器显示“内置执行器”。
7. 发送一句要记住的话，等待任务完成，用 `/hindsight view` 查看。新建会话后询问相关内容，用 `/hindsight show 记忆ID` 查看来源。

首次开放的账号使用开启模式。`readonly` 保留自动召回并停止采集；`pause` 关闭采集和自动召回。两者均允许本人显式查看与手动召回。

用户消息保持标准接收事件与原始载荷核对；助手只从标准 `conversation_manager` 持久化 `history` 读取最终纯文本，并关联同一来源账号、代次和会话内已核对用户事件。最终助手正文保存后即独立入队，不必再发一条用户消息触发。记录使用 `role=assistant`、`delivery=unknown`、`time_source=history_observed_at`，不声称 QQ 送达，发送回执可选。各路径的能力与限制见 [支持矩阵](SUPPORT.md)。

助手锚点要求先成功读取前置历史，并在 `history_baselines` 保存长度和前缀摘要；只有后续持久历史增长、原前缀一致且新增对应用户轮次时，才关联最终助手正文。读取失败不建助手锚点，用户原文仍可采集。压缩、编辑或截断改变边界时拒绝关联，不以历史中的相同文本补配。

双方语境采用 `dialogue_window_v2`，历史限最近 30 分钟内最多 4 个已核对用户轮次及对应助手，双方历史正文合计最多 4,000 字符，超限按最旧完整轮次移除，用于理解“第二个”等指代。system/tool/reasoning、工具调用中间消息、临时召回块、压缩摘要和未保存草稿不采集。助手建议不是用户接受，承诺和自称操作完成都需与实际执行区分；实际完成需独立来源。

只读、暂停、清空及移除账号权限均切断双方窗口。移除权限删除对应 `window`，通过外键级联清除 `replies` 和 `history_baselines`；恢复采集从新核对的用户事件及成功读取的前置历史建立语境，迟到助手不能跨边界回填。

配置样例见 [config.example.json](config.example.json)。样例默认关闭；填写实际账号与凭据后再启用。

## Docker 数据目录

```yaml
services:
  astrbot:
    volumes:
      - ./astrbot-data:/AstrBot/data
      - ./hindsight-control:/AstrBot/hindsight-control
```

业务目录与控制目录分别持久保存。业务恢复覆盖 `astrbot-data` 时，独立保留最新 `hindsight-control`。两个目录须允许 AstrBot 运行用户读写。

同一 Docker 网络中的 API 地址可使用 `http://hindsight:8888`。容器里的 `localhost` 是容器自身。原生 AstrBot 使用宿主机可达地址；远程网络使用 HTTPS。

## QQ 文件导出

`/hindsight export` 通过 AstrBot 标准 `File` 消息发送 UTF-8 JSON，包含本人 Bank 中的事实、Observation、关联源文档和插件保存的显式更正。队列与聊天历史由各自的备份流程管理。

当 AstrBot 与 OneBot 分别运行在不同容器时，协议端不能读取 AstrBot 容器的本地路径。需在 AstrBot 全局配置中设置协议端可达的 `callback_api_base`，例如同一私有网络中的 `http://astrbot:6185`；也可采用双方可读取的同一路径。该地址使用 AstrBot 的标准文件服务。

先验证协议端能发送普通文件，再使用导出命令。临时导出文件在发送调用结束后删除；导出最多 10,000 条记忆和 8 MiB，超过限制会提示使用后端数据导出。读取期间数据变化或来源缺失会中止，避免把部分内容作为完整导出。

## 后端操作历史

后端保留操作历史，配置 `HINDSIGHT_API_OPERATION_RETENTION_DAYS=0`。队列重试和备份恢复依赖这些操作记录。长期保留与容量调整参见 [数据维护](DATA-MAINTENANCE.md)。

数据会发送至部署的 Hindsight 及其配置的抽取、整合、向量服务；插件不额外调用摘要模型，自动 reflect 关闭。

## 升级已有数据

升级前按 [数据维护](DATA-MAINTENANCE.md) 创建业务快照并单独保存最新控制库。业务格式 2→3 增加 `corrections` 表，3→4 新增短期 `replies` 和 `history_baselines` 表；升级沿用既有部署与逻辑身份、Bank、代次、原文摘要和 operation UUID。用户文档沿用 `uw1`，旧冻结请求不改写；缺少历史基线的旧任务不从后来历史补造助手锚点。新建双方材料使用 `dialogue_window_v2`，最终助手正文使用独立 `aw1` 文档与 operation。

替换插件后重载，按最新控制库检查模式、权限、代次、采集下限和删除记录，清除旧窗口及失效助手关联和历史基线。恢复旧业务快照不能从旧 history 重新灌入已遗忘内容；回退也须保留最新独立控制库。

状态显示恢复检查完成后再使用管理命令。控制库缺失或身份不匹配时，恢复匹配的控制目录后重载。
