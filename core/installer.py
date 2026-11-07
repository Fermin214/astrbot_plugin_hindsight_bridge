"""Install and remove one owned Linux service without model credentials."""

import base64
import gzip
import io
import json
from pathlib import Path
import re
import secrets
import shlex
import tarfile
from urllib.parse import urlsplit

HINDSIGHT_VERSION = "1.2.0"
SUPPORTED_VERSIONS = ("1.1.0", HINDSIGHT_VERSION)
POSTGRES_IMAGE = "astrbot-hindsight-postgres:17-pgroonga4.0.8-pgvector0.8.6-r2"


COMPOSE = """\
services:
  memory-db:
    build: ./postgres
    image: astrbot-hindsight-postgres:17-pgroonga4.0.8-pgvector0.8.6-r2
    restart: unless-stopped
    environment:
      POSTGRES_USER: hindsight
      POSTGRES_DB: hindsight
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?}
    volumes:
      - memory_postgres:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U hindsight -d hindsight"]
      interval: 5s
      timeout: 5s
      retries: 30
    networks: [memory_database]
  memory-settings-init:
    image: ghcr.io/vectorize-io/hindsight:1.2.0-slim
    user: "0:0"
    entrypoint: [python, -c, "import os; os.chown('/settings',1000,1000); os.chmod('/settings',0o700)"]
    volumes: [memory_settings:/settings]
    network_mode: none
    restart: "no"
  memory-api:
    image: ghcr.io/vectorize-io/hindsight:1.2.0-slim
    entrypoint: [python, /opt/astrbot-memory/managed_service.py]
    restart: unless-stopped
    init: true
    stop_grace_period: 90s
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on:
      memory-db:
        condition: service_healthy
      memory-settings-init:
        condition: service_completed_successfully
    volumes:
      - ./service:/opt/astrbot-memory:ro
      - memory_settings:/settings
    env_file: .env
    environment:
      HINDSIGHT_API_DATABASE_URL: postgresql://hindsight:${POSTGRES_PASSWORD:?}@memory-db:5432/hindsight
      HINDSIGHT_API_TENANT_EXTENSION: hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
      HINDSIGHT_API_TENANT_API_KEY: ${HINDSIGHT_API_TENANT_API_KEY:?}
      HINDSIGHT_API_WORKER_ID: ${HINDSIGHT_API_WORKER_ID:?}
      HINDSIGHT_API_PORT: "8888"
      HINDSIGHT_API_WORKER_MAX_SLOTS: "2"
      HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS: "1"
      HINDSIGHT_API_DB_POOL_MIN_SIZE: "2"
      HINDSIGHT_API_DB_POOL_MAX_SIZE: "10"
      HINDSIGHT_API_OPERATION_RETENTION_DAYS: "0"
      HINDSIGHT_API_VECTOR_EXTENSION: pgvector
      HINDSIGHT_API_TEXT_SEARCH_EXTENSION: pgroonga
      HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS: "8192"
      HINDSIGHT_API_CONSOLIDATION_MAX_COMPLETION_TOKENS: "8192"
      HINDSIGHT_ENABLE_CP: "true"
      HINDSIGHT_CP_PORT: "9999"
      HINDSIGHT_CP_DATAPLANE_API_URL: http://127.0.0.1:8888
      HINDSIGHT_CP_DATAPLANE_API_KEY: ${HINDSIGHT_API_TENANT_API_KEY:?}
      HINDSIGHT_CP_ACCESS_KEY: ${HINDSIGHT_CP_ACCESS_KEY:?}
      HINDSIGHT_API_LOG_LEVEL: warning
      HINDSIGHT_API_OTEL_ENABLED: "false"
    ports:
      - "127.0.0.1:${ASTRBOT_MEMORY_API_PORT:-8888}:8888"
      - "127.0.0.1:${ASTRBOT_MEMORY_MANAGEMENT_PORT:-8889}:8766"
      - "127.0.0.1:${ASTRBOT_MEMORY_CP_PORT:-9999}:9999"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=3).close()"]
      interval: 5s
      timeout: 5s
      start_period: 10s
      retries: 15
    networks:
      memory_database:
      memory_clients:
        aliases: [MEMORY_ALIAS]
networks:
  memory_database:
    internal: true
  memory_clients:
    name: MEMORY_NETWORK
volumes:
  memory_postgres:
  memory_settings:
"""

SCRIPT = r"""#!/bin/bash
set -euo pipefail
umask 077
fail() { printf '\n安装未完成：%s\n' "$1" >&2; exit 1; }
[ "$(uname -s)" = Linux ] || fail '只支持 Linux 主机。'
command -v docker >/dev/null || fail '请先在服务器安装 Docker Engine。'
docker info >/dev/null 2>&1 || fail '无法使用 Docker，请使用有 Docker 权限的服务器终端。'
docker compose version >/dev/null 2>&1 || fail '请先安装 Docker Compose v2。'
project=PROJECT_VALUE
expected_host=HOST_VALUE
setup_path=SETUP_PATH_VALUE
target="${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}/$project"
[ ! -L "$target" ] || fail '安装目录不能是符号链接。'
astrbot_id=''
if [ -n "$expected_host" ]; then
  for container in $(docker ps -q); do
    if [ "$(docker inspect --format '{{.Config.Hostname}}' "$container")" = "$expected_host" ]; then
      if [ -n "$setup_path" ]; then
        docker exec "$container" python -c 'import json,sys; assert json.load(open(sys.argv[1])).get("id")==sys.argv[2]' "$setup_path" ID_VALUE < /dev/null 2>/dev/null || continue
      fi
      [ -z "$astrbot_id" ] || fail '找到多个同名 AstrBot 容器，请设置唯一 hostname 后重新生成命令。'
      astrbot_id="$container"
    fi
  done
  [ -n "$astrbot_id" ] || fail '请在运行这套 AstrBot 的 Docker 主机执行安装命令。'
fi
mkdir -p "$target"
chmod 700 "$target"
stage="$(mktemp -d "$target/.install.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
printf '%s' 'ARCHIVE_VALUE' | base64 -d | tar -xzf - -C "$stage"
if [ -f "$target/identity" ]; then
  cmp -s "$stage/identity" "$target/identity" || fail '安装目录属于另一套服务。'
elif [ -n "$(ls -A "$target" | grep -v '^\.install\.' || true)" ]; then
  fail '安装目录非空且缺少服务标识，请检查路径。'
fi
if [ -f "$target/.env" ]; then
  for field in POSTGRES_PASSWORD HINDSIGHT_API_TENANT_API_KEY ASTRBOT_MEMORY_MANAGEMENT_KEY HINDSIGHT_API_WORKER_ID; do
    old="$(grep -m1 "^${field}=" "$target/.env" || true)"
    new="$(grep -m1 "^${field}=" "$stage/.env" || true)"
    [ "$old" = "$new" ] || fail '服务身份发生变化，请沿用原来的插件配置。'
  done
  for field in ASTRBOT_MEMORY_API_PORT ASTRBOT_MEMORY_MANAGEMENT_PORT ASTRBOT_MEMORY_CP_PORT; do
    old="$(grep -m1 "^${field}=" "$target/.env" || true)"
    new="$(grep -m1 "^${field}=" "$stage/.env" || true)"
    [ -z "$old" ] || [ "$old" = "$new" ] || fail "端口配置不一致：$field。请按安装目录 .env 中的端口重新生成命令。"
  done
  # Preserve credentials and local settings; add only fields absent from older installs.
  cp "$target/.env" "$stage/.env.saved"
  for field in HINDSIGHT_CP_ACCESS_KEY ASTRBOT_MEMORY_CP_PORT; do
    if ! grep -q "^${field}=" "$target/.env"; then
      printf '\n%s\n' "$(grep -m1 "^${field}=" "$stage/.env")" >> "$stage/.env.saved"
    fi
  done
  mv "$stage/.env.saved" "$stage/.env"
fi
connection_mode=native
if [ -n "$astrbot_id" ]; then
  existing="$(docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$astrbot_id")"
  selected=''
  fallback=''
  for shared in $existing; do
    [ "$shared" = NETWORK_VALUE ] && continue
    [[ "$shared" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail 'AstrBot 网络名称无法写入配置。'
    [ -z "${HINDSIGHT_ASTRBOT_NETWORK:-}" ] || [ "$shared" = "$HINDSIGHT_ASTRBOT_NETWORK" ] || continue
    [ "$(docker network inspect --format '{{.Driver}} {{.Internal}}' "$shared")" = 'bridge false' ] || continue
    if [ "$shared" = bridge ] && [ -z "${HINDSIGHT_ASTRBOT_NETWORK:-}" ]; then
      fallback=bridge
      continue
    fi
    selected="$shared"
    break
  done
  selected="${selected:-$fallback}"
  if [ -n "$selected" ] && [ "$selected" != bridge ]; then
    connection_mode=custom
    printf 'services:\n  memory-api:\n    networks:\n      astrbot:\n        aliases: [ALIAS_VALUE]\nnetworks:\n  astrbot:\n    external: true\n    name: "%s"\n' "$selected" > "$stage/compose.override.yaml"
  elif [ "$selected" = bridge ]; then
    connection_mode=bridge
  elif [ "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$astrbot_id")" = host ] && [ -z "${HINDSIGHT_ASTRBOT_NETWORK:-}" ]; then
    connection_mode=host
    sed -i '/^    ports:$/a\      - "127.0.0.1:API_PORT_VALUE:8888"\n      - "127.0.0.1:MANAGEMENT_PORT_VALUE:8766"' "$stage/compose.yaml"
  else
    fail 'AstrBot 需要 host、默认 bridge 或可出站的自定义桥接网络；可用 HINDSIGHT_ASTRBOT_NETWORK 选择已连接的桥接网络。'
  fi
fi
owned_ports=''
for container in $(docker ps -q --filter "label=com.docker.compose.project=$project" --filter 'label=com.docker.compose.service=memory-api'); do
  owned_ports="$owned_ports
$(docker inspect --format '{{range .NetworkSettings.Ports}}{{range .}}{{println .HostIp .HostPort}}{{end}}{{end}}' "$container")"
done
host_ports='CP_PORT_VALUE'
if [ "$connection_mode" = native ] || [ "$connection_mode" = host ]; then
  host_ports="$host_ports API_PORT_VALUE MANAGEMENT_PORT_VALUE"
fi
for port in $host_ports; do
  if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    printf '%s\n' "$owned_ports" | grep -qx "127.0.0.1 $port" ||
      fail "主机端口 $port 已占用。请释放该端口，或修改安装状态中的端口后重新生成命令；已有安装需同步修改 .env 中的端口。"
  fi
done
REDEPLOY_BEFORE
UPGRADE_BEFORE
printf '正在安装记忆服务。首次安装需要下载镜像和构建数据库，请保持终端打开。\n'
service_changed=0
if [ -f "$target/identity" ]; then
  for file in managed_service.py model_probe.py model_transport.py; do
    cmp -s "$stage/service/$file" "$target/service/$file" || service_changed=1
  done
fi
cp -R "$stage/." "$target/"
# Remove only the installer's obsolete network override when changing modes.
if [ ! -f "$stage/compose.override.yaml" ]; then
  rm -f "$target/compose.override.yaml"
fi
chmod 600 "$target/.env"
chmod 755 "$target/service"
chmod 644 "$target/service/managed_service.py" "$target/service/model_probe.py" "$target/service/model_transport.py"
chmod 644 "$target/postgres/Dockerfile" "$target/postgres/extensions.sql"
cd "$target"
docker compose --project-name "$project" config --quiet
if ! docker image inspect POSTGRES_IMAGE_VALUE >/dev/null 2>&1; then
  docker compose --project-name "$project" build memory-db || fail '数据库镜像构建失败，请检查镜像下载和构建日志后重新执行安装命令；原数据保留。'
fi
docker compose --project-name "$project" up -d --no-build --wait --wait-timeout 180 memory-db || fail '数据库启动失败，请检查数据库日志后重新执行安装命令；原数据保留。'
extensions="$(docker compose --project-name "$project" exec -T memory-db psql -U hindsight -d hindsight -v ON_ERROR_STOP=1 -qAt -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pgroonga; SELECT count(*) FROM pg_extension WHERE extname IN ('vector','pgroonga');" < /dev/null)" || fail '数据库扩展初始化失败，请检查数据库日志与镜像后重新执行安装命令；原数据保留。'
[ "$extensions" = 2 ] || fail '数据库扩展不完整，请检查 vector 与 pgroonga 安装结果后重新执行安装命令；原数据保留。'
docker compose --project-name "$project" up -d --no-build --wait --wait-timeout 600 || fail '配套组件启动超时或失败，请检查容器日志、网络和端口后重新执行安装命令；原配置与数据保留。'
if [ "$service_changed" = 1 ]; then
  docker compose --project-name "$project" up -d --no-build --no-deps --force-recreate --wait --wait-timeout 600 memory-api || fail '配套管理组件更新失败，请检查容器日志后重新执行安装命令；原配置与数据保留。'
fi
api_url='http://127.0.0.1:API_PORT_VALUE'
management_url='http://127.0.0.1:MANAGEMENT_PORT_VALUE'
if [ "$connection_mode" = custom ]; then
  api_url='http://ALIAS_VALUE:8888'
  management_url='http://ALIAS_VALUE:8766'
elif [ "$connection_mode" = bridge ]; then
  api_id="$(docker compose --project-name "$project" ps -q memory-api)"
  address="$(docker inspect --format '{{with index .NetworkSettings.Networks "bridge"}}{{.IPAddress}}{{end}}' "$api_id")"
  if [ -z "$address" ]; then
    docker network connect bridge "$api_id"
    address="$(docker inspect --format '{{with index .NetworkSettings.Networks "bridge"}}{{.IPAddress}}{{end}}' "$api_id")"
  fi
  [[ "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail '无法获取配套服务在默认 bridge 中的地址。'
  api_url="http://$address:8888"
  management_url="http://$address:8766"
fi
docker compose --project-name "$project" exec -T memory-api python -c "import json,os,urllib.request; r=urllib.request.Request('http://127.0.0.1:8766/status',headers={'Authorization':'Bearer '+os.environ['ASTRBOT_MEMORY_MANAGEMENT_KEY']}); s=json.load(urllib.request.urlopen(r,timeout=5)); assert s.get('installation_id')==os.environ['HINDSIGHT_SETUP_ID'] and s.get('protocol')==1, '配套管理接口身份不匹配'" < /dev/null || fail '配套管理接口检查失败，请检查安装身份与服务日志后重试。'
if [ -n "$astrbot_id" ] && [ -n "$setup_path" ]; then
  docker exec "$astrbot_id" python -c 'import json,os,sys,tempfile; from pathlib import Path
path=Path(sys.argv[1]); identity=sys.argv[2]
assert json.loads(path.read_text()).get("id")==identity
data=dict(installation_id=identity,api_url=sys.argv[3],management_url=sys.argv[4])
fd,name=tempfile.mkstemp(prefix=".managed-network-",dir=path.parent)
with os.fdopen(fd,"w") as output: json.dump(data,output)
os.replace(name,path.with_name("managed-network.json"))
' "$setup_path" ID_VALUE "$api_url" "$management_url" < /dev/null || fail '连接地址保存失败，请确认 AstrBot 正在运行后重新执行安装命令。'
fi
UPGRADE_AFTER
printf '\n配套组件安装完成。回到插件的「设置」选择模型，点击「保存并连接」完成记忆服务接入。\n'
printf '服务文件位置：%s\n' "$target"
printf '官方面板：http://127.0.0.1:CP_PORT_VALUE（远程电脑需先建立 SSH 隧道，或配置域名反向代理）\n'
printf '在此服务器执行下面的命令，获取面板登录密钥：\n'
printf '%s %q %s\n' "sed -n 's/^HINDSIGHT_CP_ACCESS_KEY=//p'" "$target/.env" "| tr -d '\047\042'"
"""

# Generated commands pipe this script into Bash; exec must not consume that stream.
UPGRADE_BEFORE = r"""[ -f "$target/identity" ] && [ -f "$target/.env" ] || fail '找不到原安装记录；升级不能创建另一套服务。'
cd "$target"
docker compose --project-name "$project" config --quiet
docker compose --project-name "$project" exec -T memory-api python -c "import json,urllib.request; v=json.load(urllib.request.urlopen('http://127.0.0.1:8888/v1/version',timeout=5))['api_version']; assert v=='1.1.0', '仅支持从 1.1.0 升级，实际为 '+str(v)" < /dev/null || fail '无法确认原服务版本；未替换服务，请检查日志与版本。'
docker pull ghcr.io/vectorize-io/hindsight:1.2.0-slim || fail '下载新版镜像失败；原服务尚未停止，请检查网络后重试。'
[ ! -L "${target}-backups" ] || fail '备份目录不能是符号链接。'
mkdir -p "${target}-backups"
chmod 700 "${target}-backups"
backup="$(mktemp -d "${target}-backups/upgrade-1.1.0-XXXXXX")"
printf '升级备份位置：%s\n' "$backup"
trap 'printf "升级未完成；保持保存和查询暂停，备份位置：%s。不要直接改回旧镜像，请按备份与恢复指南恢复。\n" "$backup" >&2' ERR
cp "$target/.env" "$target/identity" "$target/compose.yaml" "$backup/"
[ ! -f "$target/compose.override.yaml" ] || cp "$target/compose.override.yaml" "$backup/"
cp -R "$target/service" "$target/postgres" "$backup/"
docker compose --project-name "$project" exec -T memory-api python -c "from pathlib import Path; import sys; sys.stdout.buffer.write(Path('/settings/models.json').read_bytes())" < /dev/null > "$backup/models.json"
docker compose --project-name "$project" stop --timeout 90 memory-api
docker compose --project-name "$project" exec -T memory-db pg_dump -U hindsight -d hindsight -Fc < /dev/null > "$backup/hindsight.dump"
[ -s "$backup/hindsight.dump" ] || fail '数据库备份为空；保持保存和查询暂停并检查数据库。'
docker compose --project-name "$project" exec -T memory-db pg_restore --list < "$backup/hindsight.dump" > "$backup/dump-inventory.txt"
printf '%s\n' 'from=1.1.0' 'to=1.2.0' > "$backup/upgrade.txt"
chmod -R go-rwx "$backup"
"""

REDEPLOY_BEFORE = r"""if [ -f "$target/identity" ]; then
  cd "$target"
  if ! docker compose --project-name "$project" exec -T memory-api python -c "import json,urllib.request; v=json.load(urllib.request.urlopen('http://127.0.0.1:8888/v1/version',timeout=5))['api_version']; assert v=='1.2.0', '同版本修复仅支持1.2.0，实际为 '+str(v)" < /dev/null; then
    # Only a pinned, unconfigured installation can resume without a running API.
    running="$(docker compose --project-name "$project" ps --status running -q memory-api)"
    if [ -n "$running" ]; then
      docker compose --project-name "$project" exec -T memory-api python -c "import json,os,urllib.request; from hindsight_api import __version__; assert __version__=='1.2.0'; r=urllib.request.Request('http://127.0.0.1:8766/status',headers={'Authorization':'Bearer '+os.environ['ASTRBOT_MEMORY_MANAGEMENT_KEY']}); s=json.load(urllib.request.urlopen(r,timeout=5)); assert s.get('installation_id')==os.environ['HINDSIGHT_SETUP_ID'] and s.get('protocol')==1 and s.get('configured') is False and not s.get('applying',False), '不能续接已配置或正在应用模型的服务'" < /dev/null || fail '无法确认配套服务可续接；已配置的服务须恢复连接，1.1.0 须使用升级入口。'
    else
      docker compose --project-name "$project" run --rm --no-deps --entrypoint python memory-api -c "import os; from pathlib import Path; from hindsight_api import __version__; assert __version__=='1.2.0' and not Path('/settings/models.json').exists() and os.environ['HINDSIGHT_SETUP_ID']=='ID_VALUE', '不能续接旧版本或已配置的服务'" < /dev/null || fail '无法确认首次安装状态；原配置与数据保留，1.1.0 须使用升级入口。'
    fi
  fi
fi
"""

UPGRADE_AFTER = r"""docker compose --project-name "$project" exec -T memory-api python -c "import json,time,urllib.request
deadline=time.monotonic()+180
while True:
    try:
        version=json.load(urllib.request.urlopen('http://127.0.0.1:8888/v1/version',timeout=3))
    except (OSError,ValueError):
        if time.monotonic()>=deadline:
            raise RuntimeError('新版 API 在三分钟内未就绪')
        time.sleep(1)
        continue
    if version.get('api_version')!='1.2.0':
        raise RuntimeError('升级后版本不符')
    break
" < /dev/null || fail '新版版本核对失败；保持保存和查询暂停并检查日志。'
trap - ERR
printf '\n服务已升级到 1.2.0，备份保存在：%s\n回到插件页点击「核对升级结果」，核对旧记忆后再恢复原记忆模式。\n' "$backup"
exit 0
"""

UNINSTALL = r"""#!/bin/bash
set -euo pipefail
fail() { printf '\n卸载未完成：%s\n' "$1" >&2; exit 1; }
project=PROJECT_VALUE
root="${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}"
target="$root/$project"
[ ! -L "$target" ] || fail '安装目录不能是符号链接。'
[ -d "$target" ] || fail '找不到安装目录；自定义安装路径请设置 HINDSIGHT_INSTALL_ROOT。'
root="$(realpath -e -- "$root")"
target="$(realpath -e -- "$target")"
[ "$target" = "$root/$project" ] || fail '安装目录超出预期路径。'
[ "$(cat "$target/identity")" = ID_VALUE ] || fail '服务标识不匹配。'
cd "$target"
containers="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$project")"
networks="$(docker network ls -q --filter "label=com.docker.compose.project=$project")"
for container in $containers; do
  service="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$container")"
  case "$service" in memory-api|memory-db|memory-settings-init|memory-models) ;;
    *) fail '服务列表包含不属于记忆服务的组件。' ;;
  esac
done
for volume in $volumes; do
  case "$volume" in "${project}_memory_postgres"|"${project}_memory_settings") ;;
    *) fail '项目含无法识别的数据卷，请先检查资源归属。' ;;
  esac
done
for network in $networks; do
  name="$(docker network inspect --format '{{.Name}}' "$network")"
  case "$name" in "${project}_memory_database"|"${project}-clients") ;;
    *) fail '项目含无法识别的网络，请先检查资源归属。' ;;
  esac
done
printf '正在卸载配套服务，并删除该服务的全部记忆数据。\n'
for container in $containers; do
  docker stop --time 90 "$container" >/dev/null
  docker rm "$container" >/dev/null
done
for network in $networks; do docker network rm "$network" >/dev/null; done
for volume in $volumes; do docker volume rm "$volume" >/dev/null; done
cd "$root"
rm -rf -- "$target"
printf '配套服务及其记忆数据已删除。插件本体请在 AstrBot 插件面板卸载。\n'
"""


def environment_file(values):
    rows = []
    for key, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid_environment_name")
        text = str(value)
        if any(ord(char) < 32 for char in text):
            raise ValueError("配置含无法保存的字符。")
        if "'" in text:
            rows.append(key + "=" + json.dumps(text, ensure_ascii=False).replace("$", "$$"))
        else:
            rows.append(f"{key}='{text}'")
    return "\n".join(rows) + "\n"


def command(script):
    encoded = base64.b64encode(gzip.compress(script.encode("utf-8"))).decode("ascii")
    return f"printf '%s' '{encoded}' | base64 -d | gzip -d | bash"


def installation(root, state, *, container_hostname="", setup_path="", control_plane_url="", upgrade=False):
    identifier = state["id"]
    if not re.fullmatch(r"[0-9a-f]{12}", identifier):
        raise ValueError("invalid_installation_id")
    project = "astrbot-memory-" + identifier
    alias, network = "memory-" + identifier, project + "-clients"
    ports = {}
    for name, default in (("api_port", 8888), ("management_port", 8889), ("control_plane_port", 9999)):
        value = state.get(name, default)
        if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,5}", str(value)) or not 1 <= int(value) <= 65535:
            raise ValueError("安装端口必须是 1 到 65535 的整数。")
        ports[name] = int(value)
    if len(set(ports.values())) != len(ports):
        raise ValueError("API、模型管理和官方面板必须使用不同的主机端口。")
    browser_url = control_plane_url or state.get("control_plane_url") or f"http://127.0.0.1:{ports['control_plane_port']}"
    address = urlsplit(browser_url)
    if (
        address.scheme not in {"http", "https"} or not address.hostname
        or address.username is not None or address.password is not None
        or address.query or address.fragment
    ):
        raise ValueError("官方面板地址必须是独立的 HTTP/HTTPS 地址，不能包含密钥或登录信息。")
    values = {
        "POSTGRES_PASSWORD": state["database_password"],
        "HINDSIGHT_API_TENANT_API_KEY": state["api_key"],
        "ASTRBOT_MEMORY_MANAGEMENT_KEY": state["management_key"],
        "HINDSIGHT_API_WORKER_ID": project + "-worker",
        "HINDSIGHT_SETUP_ID": identifier,
        "HINDSIGHT_SETUP_FINGERPRINT": state.get("candidate_fingerprint", ""),
        "HINDSIGHT_CP_ACCESS_KEY": secrets.token_hex(32),
        "ASTRBOT_MEMORY_API_PORT": ports["api_port"],
        "ASTRBOT_MEMORY_MANAGEMENT_PORT": ports["management_port"],
        "ASTRBOT_MEMORY_CP_PORT": ports["control_plane_port"],
    }
    compose = COMPOSE.replace("MEMORY_ALIAS", alias).replace("MEMORY_NETWORK", network)
    if container_hostname:
        compose = compose.replace(
            '      - "127.0.0.1:${ASTRBOT_MEMORY_API_PORT:-8888}:8888"\n'
            '      - "127.0.0.1:${ASTRBOT_MEMORY_MANAGEMENT_PORT:-8889}:8766"\n', ""
        )
    files = {".env": environment_file(values), "compose.yaml": compose, "identity": identifier + "\n"}
    for name in ("managed_service.py", "model_probe.py", "model_transport.py"):
        files["service/" + name] = (Path(root) / "deploy" / name).read_text(encoding="utf-8")
    for name in ("Dockerfile", "extensions.sql"):
        files["postgres/" + name] = (Path(root) / "deploy/postgres" / name).read_text(encoding="utf-8")
    files["HINDSIGHT-LICENSE"] = (Path(root) / "deploy/HINDSIGHT-LICENSE").read_text(encoding="utf-8")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        for name, content in files.items():
            data = content.encode("utf-8")
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            entry.mode = 0o600 if name in {".env", "identity"} else 0o644
            output.addfile(entry, io.BytesIO(data))
    script = SCRIPT
    for marker, value in {
        "REDEPLOY_BEFORE": "" if upgrade else REDEPLOY_BEFORE,
        "UPGRADE_BEFORE": UPGRADE_BEFORE if upgrade else "",
        "UPGRADE_AFTER": UPGRADE_AFTER if upgrade else "",
        "PROJECT_VALUE": shlex.quote(project), "HOST_VALUE": shlex.quote(container_hostname),
        "SETUP_PATH_VALUE": shlex.quote(str(setup_path)),
        "NETWORK_VALUE": shlex.quote(network), "ALIAS_VALUE": alias,
        "ID_VALUE": identifier, "POSTGRES_IMAGE_VALUE": POSTGRES_IMAGE,
        "API_PORT_VALUE": str(ports["api_port"]),
        "MANAGEMENT_PORT_VALUE": str(ports["management_port"]),
        "CP_PORT_VALUE": str(ports["control_plane_port"]),
        "ARCHIVE_VALUE": base64.b64encode(archive.getvalue()).decode("ascii"),
    }.items():
        script = script.replace(marker, value)
    return {
        "command": command(script),
        "api_url": f"http://{alias}:8888" if container_hostname else f"http://127.0.0.1:{values['ASTRBOT_MEMORY_API_PORT']}",
        "management_url": f"http://{alias}:8766" if container_hostname else f"http://127.0.0.1:{values['ASTRBOT_MEMORY_MANAGEMENT_PORT']}",
        "browser_url": browser_url,
        "control_plane_url": browser_url,
        "control_plane_key_file": "${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}/" + project + "/.env",
        "control_plane_key_env": "HINDSIGHT_CP_ACCESS_KEY",
        "message": (
            "在原服务器、原安装路径执行。命令先备份配置、模型和数据库，再升级；失败时保持保存和查询暂停。"
            if upgrade else "在运行 AstrBot 的 Linux 主机执行，完成后回到设置中选择模型。"
        ),
    }


def uninstallation(state):
    identifier = state.get("id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{12}", identifier):
        raise ValueError("找不到配套服务安装记录。")
    return {
        "command": command(UNINSTALL.replace("PROJECT_VALUE", "astrbot-memory-" + identifier).replace("ID_VALUE", identifier)),
        "message": "执行后删除这套配套服务及全部记忆数据。插件本体请在 AstrBot 插件面板卸载。",
    }
