# Sources and dependencies

The plugin is independently implemented against AstrBot's documented plugin
interfaces and the Hindsight 1.1.0 public HTTP schema. No third-party AstrBot
plugin source is included.

- AstrBot: https://github.com/AstrBotDevs/AstrBot (AGPL-3.0); the host application
  is not bundled in this package.
- Hindsight: https://github.com/vectorize-io/hindsight/tree/v1.1.0 (root LICENSE:
  MIT, Copyright 2025 Vectorize.io); deployed separately. The PGroonga + pgvector
  image recipe follows the upstream PostgreSQL recipe. Upstream MIT notice is
  retained in `deploy/HINDSIGHT-LICENSE`.
- httpx: https://github.com/encode/httpx (BSD-3-Clause), installed as a dependency.
- tiktoken: https://github.com/openai/tiktoken, installed for query token budgets.
- tzdata: https://github.com/python/tzdata, installed on Windows for IANA time zones.
- PostgreSQL, pgvector and PGroonga remain separate server dependencies under
  their respective upstream licenses. No database binaries are shipped here.
