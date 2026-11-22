# Hindsight Bridge

[English](README.en.md) | [简体中文](README.md)

Connect AstrBot to self-hosted [Hindsight](https://github.com/vectorize-io/hindsight) so your bot remembers private chats across conversations.

During normal conversations, the plugin extracts private text messages from QQ, WeChat ClawBot, Telegram, and Feishu, sends them to Hindsight for long-term memory extraction, and retrieves relevant memories for future replies. Each user has an isolated memory bank.

## Features

- **Automatic Capture & Recall**: Conversations are saved automatically in the background and recalled semantically in later chats—either via **Auto Recall** or **On-Demand Agent Tools**.
- **Knowledge Pages & Reflect**: Attach up to 8 ordered Knowledge Pages generated in Hindsight's Control Plane, or enable per-user Reflect synthesis for complex questions.
- **Per-User Controls & Inspection**: Switch between Read/Write, Read-Only, and Paused modes per user, customize memory prompts, enable read-only recall on quoted messages or links, and inspect exact delivered context per turn.
- **Multi-Platform Isolation**: Supports OneBot v11, QQ Official C2C (WebSocket & Webhook), WeChat ClawBot, Telegram, and Feishu private chats, isolated by bot account and user.
- **Hassle-Free Deployment & Queuing**: Generate a companion Docker deployment command on Linux that reuses AstrBot's configured models, or connect any existing Hindsight service on Windows/Linux. Unsent messages queue locally and retry once Hindsight comes back online.

## Requirements

| Component | Supported |
| --- | --- |
| AstrBot | **4.29.1+** using the built-in `local` executor |
| Hindsight | Self-hosted **1.1.0** or **1.2.0** |
| Platforms (private chat) | OneBot v11 (`aiocqhttp`), QQ Official (`qq_official` / `qq_official_webhook`), WeChat ClawBot (`weixin_oc`), Telegram (`telegram`), Feishu (`lark`) |
| Host OS | Linux (Docker Engine + Compose v2) for companion setup; Windows or any host when connecting an existing Hindsight instance |

See [Compatibility](docs/SUPPORT.md) for details.

## Installation

1. In AstrBot, go to **Plugins → Install → Install from URL** and enter:

   ```text
   https://github.com/Fermin214/astrbot_plugin_hindsight_bridge
   ```

2. Open **Hindsight Bridge Plugin Details → Memory Management → Plugin Settings**:
   - **Companion Service** (recommended on Linux): Click **Generate Install Command**, run it on the Linux host running AstrBot, then select your chat and embedding models (reranker is optional) and click **Save and Connect**.
   - **Existing Service**: Enter your Hindsight 1.1.0 or 1.2.0 API URL, API Key, and Control Plane URL, click **Test Connection**, then **Save Connection**.
3. Switch to **Users & Memories**, click **+ Add User**, choose the bot instance, and enter the user ID (QQ number for OneBot; UID from `/sid` in private chat for QQ Official, WeChat ClawBot, Telegram, or Feishu). Confirm the bot identity and start chatting.

Companion deployments reuse providers already configured in AstrBot—no duplicate endpoints or keys needed. If no embedding model appears, add one under AstrBot's **Model Providers → Embedding**.

| Capability | Companion Deployment | Existing Service |
| --- | --- | --- |
| Users, modes, knowledge pages, parameters | Plugin panel | Plugin panel |
| Browse, search, and edit memory entries | Official Control Plane | Official Control Plane |
| Model configuration | Selected in plugin panel | Managed by your service |
| Service backup, upgrade, redeploy, uninstall | Plugin panel generates commands | Managed by your service |

See the [Installation Guide](docs/INSTALL.md), [Native Windows Deployment](docs/WINDOWS.md), and [Linux Manual Deployment](deploy/README.md) for full instructions.

## Upgrading

> **Upgrading to 0.4.0**: Back up your plugin data, control directory, and config before upgrading (see [Backup & Restore](docs/DATA-MAINTENANCE.md)). **0.4.0 automatically migrates the control database to schema format 7 while preserving existing banks, settings, and pending records; direct downgrades to older plugin versions are not supported afterward.**

Update and reload the plugin in AstrBot's plugin manager; if prompted, re-confirm the bot identity. Updating the plugin does not touch your Hindsight containers. To upgrade a companion Hindsight service, follow the [Service Upgrade Guide](docs/DATA-MAINTENANCE.md#升级-hindsight-服务) (fresh companion installs default to 1.2.0).

## Usage & Management

Normal chatting requires no commands. Open **Users & Memories** in the plugin panel to:

- **Switch Memory Modes**: Toggle **Read/Write**, **Read-Only**, or **Paused** at the top overview card and monitor saved, pending, and failed counts alongside the latest recall result.
- **Tune Chat Recall**: Switch between **Auto Recall** and **On-Demand Tools**, customize the memory prompt and retrieval budget, allow read-only recall on quoted replies or links, and inspect delivered context snapshots per turn.
- **Manage Knowledge Pages & Reflect**: Pick, order, and toggle up to 8 Knowledge Pages, or enable per-user Reflect synthesis.
- **Edit Content in Control Plane**: Copy the Control Plane URL from **Plugin Settings → Hindsight Service Connection** and the user's **Bank ID** to browse, search, edit, or export memories directly in Hindsight (see [Deployment](deploy/README.md#五打开官方-control-plane) for login key retrieval).
- **Recover or Clear Data**: Resolve upgrade or error pauses under **View Pause Status**, clear stuck Reflect states via **Unblock Reflect** after confirming no active task remains on Hindsight, or permanently erase a user's bank via **Clear Memory** (AstrBot's own chat history stays intact).

Common private-chat commands (see [User Guide](docs/USAGE.md) for the full list):

| Action | Command |
| --- | --- |
| List personal memories | `/hindsight view` |
| Read selected knowledge page | `/hindsight background [index]` |
| Check status & diagnostics | `/hindsight status` |
| Export personal memories as JSON | `/hindsight export` |
| Switch to read-only (keep recalling, stop saving) | `/hindsight readonly` |
| Pause saving and automatic recall | `/hindsight pause` |
| Resume read/write | `/hindsight on` |
| Erase personal memories | `/hindsight forget`, then reply to confirm |

## Scope

- **Recorded**: Private text messages from enabled users and final assistant replies saved to AstrBot's conversation history.
- **Excluded**: Group chats, media (images, audio, files, video), forwards, commands (starting with `/`), and chats from before installation. When read-only recall for quoted replies/links is enabled, those turns read existing memories without saving new ones.
- **Privacy**: Conversation text is sent only to your configured Hindsight instance and model providers.

## Documentation

- [Installation Guide](docs/INSTALL.md): Setup wizard, connecting existing instances, and troubleshooting
- [User Guide](docs/USAGE.md): Panel controls, Knowledge Pages, On-Demand Tools, Reflect, and commands
- [Compatibility](docs/SUPPORT.md): Versions, model providers, platforms, and limits
- [Backup & Restore](docs/DATA-MAINTENANCE.md): Backups, disaster recovery, and companion upgrades
- [Contributing](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/blob/main/CONTRIBUTING.md): Architecture overview, test suites, and contribution rules

Please report issues on [GitHub Issues](https://github.com/Fermin214/astrbot_plugin_hindsight_bridge/issues) with versions, steps to reproduce, and error details.

## License

[MIT License](LICENSE). Third-party dependency notices are listed in [NOTICE.md](NOTICE.md).
