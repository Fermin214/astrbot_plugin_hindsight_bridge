"""Self-contained Linux installation, using a frozen provider configuration."""

import base64
import gzip
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import re
import shlex
import tarfile
from urllib.parse import urlsplit


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
  memory-api:
    image: ghcr.io/vectorize-io/hindsight:1.1.0-slim
    restart: unless-stopped
    init: true
    extra_hosts:
      - "host.docker.internal:host-gateway"
    depends_on:
      memory-db:
        condition: service_healthy
    env_file: .env
    environment:
      HINDSIGHT_API_DATABASE_URL: postgresql://hindsight:${POSTGRES_PASSWORD:?}@memory-db:5432/hindsight
      HINDSIGHT_API_TENANT_EXTENSION: hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension
      HINDSIGHT_API_TENANT_API_KEY: ${HINDSIGHT_API_TENANT_API_KEY:?}
      HINDSIGHT_API_WORKER_ID: ${HINDSIGHT_API_WORKER_ID:?}
      HINDSIGHT_API_WORKER_MAX_SLOTS: "2"
      HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS: "1"
      HINDSIGHT_API_DB_POOL_MIN_SIZE: "2"
      HINDSIGHT_API_DB_POOL_MAX_SIZE: "10"
      HINDSIGHT_API_OPERATION_RETENTION_DAYS: "0"
      HINDSIGHT_API_VECTOR_EXTENSION: pgvector
      HINDSIGHT_API_TEXT_SEARCH_EXTENSION: pgroonga
      HINDSIGHT_API_RETAIN_MAX_COMPLETION_TOKENS: "8192"
      HINDSIGHT_API_CONSOLIDATION_MAX_COMPLETION_TOKENS: "8192"
      HINDSIGHT_CP_DATAPLANE_API_URL: http://127.0.0.1:8888
      HINDSIGHT_CP_DATAPLANE_API_KEY: ${HINDSIGHT_API_TENANT_API_KEY:?}
      HINDSIGHT_CP_ACCESS_KEY: ${HINDSIGHT_CP_ACCESS_KEY:?}
      HINDSIGHT_API_LOG_LEVEL: warning
      HINDSIGHT_API_OTEL_ENABLED: "false"
    ports:
      - "127.0.0.1:${HINDSIGHT_API_PORT:-8888}:8888"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/health', timeout=5).close()"]
      interval: 10s
      timeout: 8s
      start_period: 90s
      retries: 20
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
astrbot_id=''
if [ -n "$expected_host" ]; then
  for container in $(docker ps -q); do
    if [ "$(docker inspect --format '{{.Config.Hostname}}' "$container")" = "$expected_host" ]; then
      [ -z "$astrbot_id" ] || fail '找到多个同名 AstrBot 容器，请给 AstrBot 设置唯一 hostname 后重新生成安装命令。'
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
  cmp -s "$stage/identity" "$target/identity" || fail '安装目录属于另一套服务，请沿用原来的 AstrBot 插件配置。'
fi
if [ -f "$target/.env" ]; then
  # Database identity and model dimensions must survive repeated installation.
  for field in POSTGRES_PASSWORD HINDSIGHT_API_TENANT_API_KEY HINDSIGHT_API_WORKER_ID HINDSIGHT_SETUP_FINGERPRINT; do
    old="$(grep -m1 "^${field}=" "$target/.env" || true)"
    new="$(grep -m1 "^${field}=" "$stage/.env" || true)"
    [ "$old" = "$new" ] || fail '服务身份或嵌入模型发生变化，请先完成数据迁移。'
  done
fi
printf '正在安装记忆服务。首次安装需要下载镜像和构建数据库，请保持终端打开。\n'
cp -R "$stage/." "$target/"
cd "$target"
if [ -n "$astrbot_id" ]; then
  existing="$(docker inspect --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$astrbot_id")"
  index=0
  printf 'services:\n  memory-api:\n    networks:\n' > compose.override.yaml
  for shared in $existing; do
    [ "$shared" = NETWORK_VALUE ] && continue
    [[ "$shared" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] || fail 'AstrBot 网络名称无法写入安装配置。'
    [ "$shared" != host ] && [ "$shared" != none ] || fail '配套安装需要 AstrBot 使用 Docker 桥接网络。'
    printf '      astrbot_%s:\n        aliases: [ALIAS_VALUE]\n' "$index" >> compose.override.yaml
    index=$((index + 1))
  done
  [ "$index" -gt 0 ] || fail 'AstrBot 没有可连接的 Docker 网络。'
  if [ HAS_TRANSPORT_VALUE = yes ]; then
    printf '  memory-models:\n    networks:\n' >> compose.override.yaml
    index=0
    for shared in $existing; do
      [ "$shared" = NETWORK_VALUE ] && continue
      printf '      astrbot_%s: {}\n' "$index" >> compose.override.yaml
      index=$((index + 1))
    done
  fi
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
docker compose --project-name "$project" exec -T memory-api python - <<'PY'
import asyncio
import json
import logging
import math
import os
import sys
import urllib.request
from hindsight_api.config import get_config
from hindsight_api.engine.embeddings import create_embeddings_from_env
from hindsight_api.engine.cross_encoder import create_cross_encoder_from_env
from hindsight_api.engine.llm_wrapper import LLMProvider

logging.disable(logging.CRITICAL)

worker = get_config()
if not worker.worker_enabled or worker.worker_max_slots <= sum(worker.worker_slot_reservations.values()):
    sys.exit("安装未完成：后台任务没有可用写入槽位，请更新插件并重新生成安装命令。")

def call(url, key, body, method="POST"):
    data = json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)

async def check_models():
    try:
        embeddings = create_embeddings_from_env()
        await embeddings.initialize()
        vectors = await embeddings.encode(["连接检查"])
        assert len(vectors) == 1
        assert len(vectors[0]) == int(os.environ["HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS"])
        assert all(math.isfinite(float(value)) for value in vectors[0])
    except Exception:
        sys.exit("安装未完成：记忆服务无法调用嵌入模型，请检查模型地址、密钥和网络后重新执行安装命令。")
    try:
        llm = LLMProvider.from_env()
        try:
            await llm.verify_connection()
        finally:
            await llm.cleanup()
    except Exception:
        sys.exit("安装未完成：记忆服务无法调用对话模型，请检查模型地址、密钥和网络后重新执行安装命令。")
    if os.environ.get("HINDSIGHT_API_RERANKER_PROVIDER") != "rrf":
        try:
            reranker = create_cross_encoder_from_env()
            await reranker.initialize()
            scores = await reranker.predict([("连接检查", "记忆服务连接检查")])
            assert len(scores) == 1 and math.isfinite(float(scores[0]))
        except Exception:
            sys.exit("安装未完成：记忆服务无法调用重排模型，请检查模型地址、密钥和网络后重新执行安装命令。")

asyncio.run(check_models())
try:
    call("http://127.0.0.1:8888/v1/default/banks/astrbot-setup-" + os.environ["HINDSIGHT_SETUP_ID"],
         os.environ["HINDSIGHT_API_TENANT_API_KEY"],
         {"name": "astrbot-setup-v1:" + os.environ["HINDSIGHT_SETUP_FINGERPRINT"]}, "PUT")
except Exception:
    sys.exit("安装未完成：无法保存服务安装标识，请重新执行安装命令。")
PY
printf '\n安装完成。回到 AstrBot 的「记忆管理」，点击「连接并启用」。\n'
printf '服务文件位置：%s\n' "$target"
"""


def environment_file(values):
    """Quote Compose dotenv values without changing request parameters."""
    rows = []
    for key, value in sorted(values.items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("invalid_environment_name")
        text = str(value)
        if any(ord(char) < 32 for char in text):
            raise ValueError("模型配置含安装文件无法保存的字符，请改用已有服务连接。")
        if "'" in text:
            rows.append(key + "=" + json.dumps(text, ensure_ascii=False).replace("$", "$$"))
        else:
            rows.append(f"{key}='{text}'")
    return "\n".join(rows) + "\n"


def installation(root, state, model_environment, *, container_hostname=""):
    model_environment = dict(model_environment)
    transports = json.loads(model_environment.pop("ASTRBOT_MEMORY_MODEL_CONFIG", "{}"))
    identity = model_environment.pop("ASTRBOT_MEMORY_EMBEDDING_IDENTITY", None)
    addresses = [
        value for key, value in model_environment.items() if key.endswith("_BASE_URL")
    ] + [
        descriptor[field] for descriptor in transports.values()
        for field in ("base_url", "proxy") if descriptor.get(field)
    ]
    for value in addresses:
        hostname = urlsplit(value).hostname or ""
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = hostname.lower() == "localhost"
        if loopback:
            raise ValueError(
                "模型地址使用 localhost 或回环地址，独立记忆容器无法访问。"
                "请在 AstrBot 中配置容器可访问的模型地址，再生成安装命令。"
            )
    identifier = state["id"]
    if not re.fullmatch(r"[0-9a-f]{12}", identifier):
        raise ValueError("invalid_installation_id")
    project = "astrbot-memory-" + identifier
    network = project + "-clients"
    alias = "memory-" + identifier
    embedding = json.loads(identity) if identity else {
        key: value for key, value in model_environment.items()
        if key.startswith("HINDSIGHT_API_EMBEDDINGS_")
        and not key.endswith(("_API_KEY", "_BATCH_SIZE"))
    }
    fingerprint = hashlib.sha256(
        repr(sorted(embedding.items())).encode("utf-8")
    ).hexdigest()
    values = {
        **model_environment,
        "POSTGRES_PASSWORD": state["database_password"],
        "HINDSIGHT_API_TENANT_API_KEY": state["api_key"],
        "HINDSIGHT_CP_ACCESS_KEY": state["panel_key"],
        "HINDSIGHT_API_WORKER_ID": project + "-worker",
        "HINDSIGHT_API_PORT": state.get("api_port", 8888),
        "HINDSIGHT_SETUP_ID": identifier,
        "HINDSIGHT_SETUP_FINGERPRINT": fingerprint,
    }
    values = {
        key: str(value).replace(
            "__ASTRBOT_MEMORY_TRANSPORT_KEY__", state["api_key"]
        ).replace("http://memory-models:8765", f"http://memory-models-{identifier}:8765")
        for key, value in values.items()
    }
    compose = COMPOSE.replace("MEMORY_ALIAS", alias).replace("MEMORY_NETWORK", network)
    if container_hostname:
        compose = compose.replace(
            '    ports:\n      - "127.0.0.1:${HINDSIGHT_API_PORT:-8888}:8888"\n', ""
        )
    files = {
        ".env": environment_file(values),
        "compose.yaml": compose,
        "identity": identifier + "\n",
    }
    if transports:
        # Both processes use the pinned image; the relay has no published port.
        values["ASTRBOT_MEMORY_MODEL_CONFIG"] = json.dumps(transports, ensure_ascii=False, allow_nan=False)
        files["model_transport.py"] = (Path(root) / "deploy/model_transport.py").read_text(encoding="utf-8")
        values["ASTRBOT_MEMORY_TRANSPORT_CODE"] = base64.b64encode(
            files["model_transport.py"].encode("utf-8")
        ).decode("ascii")
        files[".env"] = environment_file(values)
        compose = compose.replace(
            "  memory-api:\n",
            "  memory-models:\n"
            "    image: ghcr.io/vectorize-io/hindsight:1.1.0-slim\n"
            "    entrypoint: [python, -c, \"import base64,os; exec(compile(base64.b64decode(os.environ['ASTRBOT_MEMORY_TRANSPORT_CODE']), 'model_transport.py', 'exec'))\"]\n"
            "    restart: unless-stopped\n"
            "    init: true\n"
            "    read_only: true\n"
            "    cap_drop: [ALL]\n"
            "    security_opt: [no-new-privileges:true]\n"
            "    extra_hosts: [\"host.docker.internal:host-gateway\"]\n"
            "    env_file: .env\n"
            "    environment:\n"
            "      ASTRBOT_MEMORY_TRANSPORT_KEY: ${HINDSIGHT_API_TENANT_API_KEY:?}\n"
            "    healthcheck:\n"
            "      test: [\"CMD\", \"python\", \"-c\", \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).close()\"]\n"
            "      interval: 5s\n"
            "      timeout: 5s\n"
            "      retries: 10\n"
            "    networks:\n"
            "      memory_clients:\n"
            f"        aliases: [memory-models-{identifier}]\n"
            "  memory-api:\n",
        ).replace(
            "    depends_on:\n      memory-db:",
            "    depends_on:\n      memory-models:\n        condition: service_healthy\n      memory-db:",
        )
        files["compose.yaml"] = compose
    for name in ("Dockerfile", "extensions.sql"):
        path = Path(root) / "deploy" / "postgres" / name
        files["postgres/" + name] = path.read_text(encoding="utf-8")
    files["HINDSIGHT-LICENSE"] = (
        Path(root) / "deploy" / "HINDSIGHT-LICENSE"
    ).read_text(encoding="utf-8")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        for name, content in files.items():
            data = content.encode("utf-8")
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o644 if name == "model_transport.py" else 0o600
            output.addfile(entry, io.BytesIO(data))
    script = SCRIPT
    for marker, value in {
        "PROJECT_VALUE": shlex.quote(project),
        "HOST_VALUE": shlex.quote(container_hostname),
        "NETWORK_VALUE": shlex.quote(network),
        "ALIAS_VALUE": alias,
        "ARCHIVE_VALUE": base64.b64encode(archive.getvalue()).decode("ascii"),
        "HAS_TRANSPORT_VALUE": "yes" if transports else "no",
    }.items():
        script = script.replace(marker, value)
    encoded = base64.b64encode(gzip.compress(script.encode("utf-8"))).decode("ascii")
    command = f"printf '%s' '{encoded}' | base64 -d | gzip -d | bash"
    return {
        "command": command,
        "api_url": (
            f"http://{alias}:8888"
            if container_hostname
            else f"http://127.0.0.1:{values['HINDSIGHT_API_PORT']}"
        ),
        "message": "在运行 AstrBot 的 Linux 主机粘贴一次，完成后回到这里连接并启用。",
        "embedding_fingerprint": fingerprint,
    }
