"""Install and remove one owned Linux service without model credentials."""

import base64
import gzip
import io
import json
from pathlib import Path
import re
import shlex
import tarfile


COMPOSE = """\
services:
  memory-db:
    build: ./postgres
    image: astrbot-hindsight-postgres:17-pgroonga4.0.8-pgvector0.8.6
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
    image: ghcr.io/vectorize-io/hindsight:1.1.0-slim
    user: "0:0"
    entrypoint: [python, -c, "import os; os.chown('/settings',1000,1000); os.chmod('/settings',0o700)"]
    volumes: [memory_settings:/settings]
    network_mode: none
    restart: "no"
  memory-api:
    image: ghcr.io/vectorize-io/hindsight:1.1.0-slim
    entrypoint: [python, /opt/astrbot-memory/managed_service.py]
    restart: unless-stopped
    init: true
    stop_grace_period: 50s
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
      HINDSIGHT_ENABLE_CP: "false"
      HINDSIGHT_API_LOG_LEVEL: warning
      HINDSIGHT_API_OTEL_ENABLED: "false"
    ports:
      - "127.0.0.1:${ASTRBOT_MEMORY_API_PORT:-8888}:8888"
      - "127.0.0.1:${ASTRBOT_MEMORY_MANAGEMENT_PORT:-8889}:8766"
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
target="${HINDSIGHT_INSTALL_ROOT:-$HOME/.local/share}/$project"
[ ! -L "$target" ] || fail '安装目录不能是符号链接。'
astrbot_id=''
if [ -n "$expected_host" ]; then
  for container in $(docker ps -q); do
    if [ "$(docker inspect --format '{{.Config.Hostname}}' "$container")" = "$expected_host" ]; then
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
  for field in POSTGRES_PASSWORD HINDSIGHT_API_TENANT_API_KEY HINDSIGHT_API_WORKER_ID; do
    old="$(grep -m1 "^${field}=" "$target/.env" || true)"
    new="$(grep -m1 "^${field}=" "$stage/.env" || true)"
    [ "$old" = "$new" ] || fail '服务身份发生变化，请沿用原来的插件配置。'
  done
fi
printf '正在安装记忆服务。首次安装需要下载镜像和构建数据库，请保持终端打开。\n'
cp -R "$stage/." "$target/"
chmod 755 "$target/service"
cd "$target"
if [ -n "$astrbot_id" ]; then
  existing="$(docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$astrbot_id")"
  index=0
  printf 'services:\n  memory-api:\n    networks:\n' > compose.override.yaml
  for shared in $existing; do
    [ "$shared" = NETWORK_VALUE ] && continue
    [[ "$shared" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail 'AstrBot 网络名称无法写入配置。'
    [ "$shared" != host ] && [ "$shared" != none ] || fail '配套安装需要 AstrBot 使用 Docker 桥接网络。'
    printf '      astrbot_%s:\n        aliases: [ALIAS_VALUE]\n' "$index" >> compose.override.yaml
    index=$((index + 1))
  done
  [ "$index" -gt 0 ] || fail 'AstrBot 没有可连接的 Docker 网络。'
  printf 'networks:\n' >> compose.override.yaml
  index=0
  for shared in $existing; do
    [ "$shared" = NETWORK_VALUE ] && continue
    printf '  astrbot_%s:\n    external: true\n    name: "%s"\n' "$index" "$shared" >> compose.override.yaml
    index=$((index + 1))
  done
fi
docker compose --project-name "$project" config --quiet
if docker image inspect astrbot-hindsight-postgres:17-pgroonga4.0.8-pgvector0.8.6 >/dev/null 2>&1; then
  docker compose --project-name "$project" up -d --no-build
else
  docker compose --project-name "$project" up -d --build
fi
docker compose --project-name "$project" up -d --wait --wait-timeout 600
printf '\n安装完成。回到插件的「设置」检查连接，再选择模型并保存。\n'
printf '服务文件位置：%s\n' "$target"
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
  docker stop --time 50 "$container" >/dev/null
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


def installation(root, state, *, container_hostname=""):
    identifier = state["id"]
    if not re.fullmatch(r"[0-9a-f]{12}", identifier):
        raise ValueError("invalid_installation_id")
    project = "astrbot-memory-" + identifier
    alias, network = "memory-" + identifier, project + "-clients"
    values = {
        "POSTGRES_PASSWORD": state["database_password"],
        "HINDSIGHT_API_TENANT_API_KEY": state["api_key"],
        "ASTRBOT_MEMORY_MANAGEMENT_KEY": state["management_key"],
        "HINDSIGHT_API_WORKER_ID": project + "-worker",
        "HINDSIGHT_SETUP_ID": identifier,
        "HINDSIGHT_SETUP_FINGERPRINT": state.get("candidate_fingerprint", ""),
        "ASTRBOT_MEMORY_API_PORT": state.get("api_port", 8888),
        "ASTRBOT_MEMORY_MANAGEMENT_PORT": state.get("management_port", 8889),
    }
    compose = COMPOSE.replace("MEMORY_ALIAS", alias).replace("MEMORY_NETWORK", network)
    if container_hostname:
        compose = compose.replace(
            '    ports:\n'
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
        "PROJECT_VALUE": shlex.quote(project), "HOST_VALUE": shlex.quote(container_hostname),
        "NETWORK_VALUE": shlex.quote(network), "ALIAS_VALUE": alias,
        "ARCHIVE_VALUE": base64.b64encode(archive.getvalue()).decode("ascii"),
    }.items():
        script = script.replace(marker, value)
    return {
        "command": command(script),
        "api_url": f"http://{alias}:8888" if container_hostname else f"http://127.0.0.1:{values['ASTRBOT_MEMORY_API_PORT']}",
        "management_url": f"http://{alias}:8766" if container_hostname else f"http://127.0.0.1:{values['ASTRBOT_MEMORY_MANAGEMENT_PORT']}",
        "message": "在运行 AstrBot 的 Linux 主机执行，完成后回到设置中选择模型。",
    }


def uninstallation(state):
    identifier = state.get("id", "")
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{12}", identifier):
        raise ValueError("找不到配套服务安装记录。")
    return {
        "command": command(UNINSTALL.replace("PROJECT_VALUE", "astrbot-memory-" + identifier).replace("ID_VALUE", identifier)),
        "message": "执行后删除这套配套服务及全部记忆数据。插件本体请在 AstrBot 插件面板卸载。",
    }
