"""Create backend configuration with independent secrets."""

import os
from pathlib import Path
import secrets


def main():
    directory = Path(__file__).resolve().parent
    destination = directory / ".env"
    template = (directory / ".env.example").read_text(encoding="utf-8")
    for name in (
        "POSTGRES_PASSWORD",
        "HINDSIGHT_API_TENANT_API_KEY",
        "HINDSIGHT_CP_ACCESS_KEY",
    ):
        template = template.replace(f"{name}=\n", f"{name}={secrets.token_hex(32)}\n")
    try:
        descriptor = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except FileExistsError:
        print("Using existing .env; credentials, worker identity and ports are preserved.")
        return
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(template)
    print("Created .env. Fill in your LLM and embedding provider credentials.")
    print("Then run: docker compose up -d --build --wait")


if __name__ == "__main__":
    main()
