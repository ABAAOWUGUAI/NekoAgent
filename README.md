# NekoAgent

> **简体中文：**完整中文说明请看 [README.zh-CN.md](README.zh-CN.md)。部署、运行与仓库保护也有对应的中文文档。

## 中文简介

NekoAgent 是一个 Owner 优先、可自托管的私人 AI 助手平台。Assistant 的身份、关系、对话、记忆、目标、运行、交付和学习状态不绑定某一个模型或渠道；AstrBot 和 Codex 都是可选集成，不是产品本体。首次部署请从[中文部署指南](docs/zh-CN/DEPLOYMENT.md)开始，日常维护看[中文运行指南](docs/zh-CN/OPERATIONS.md)，仓库治理看[中文保护指南](docs/zh-CN/REPOSITORY_PROTECTION.md)。

NekoAgent is an Owner-first, self-hosted personal AI assistant platform. It keeps Assistant
identity, relationship, conversation, memory, goals, runs, delivery and
learning state independent from any single model or channel.

Licensed under [Apache-2.0](LICENSE). You may use, modify and redistribute
NekoAgent, including for commercial purposes, subject to that license and the
separate notices for bundled third-party and Starter Pack assets.

The repository is designed to work in three independent layers:

1. **Platform Core** — Bridge, SQLite state, Admin console, delivery and policy
   gates.
2. **AstrBot Adapter (optional)** — `remote-plugin/` receives chat events and
   reports Delivery acknowledgements; AstrBot remains its own runtime.
3. **Codex Executor (optional)** — a locally installed Codex CLI may execute
   approved work. Its login state remains on the deployer's host. Normal chat
   can instead use a configured OpenAI-compatible model connection.

No production database, conversation, media, Provider key, QQ credential,
server address, user-specific Assistant instance or private PetPack is bundled.
This project ships no default character identity. After bootstrap, create your
own Assistant in the console or explicitly apply an optional Starter Pack.

Version `0.2.0` adds optional Owner-private QQ voice-message input. Audio is
fetched through a bounded channel path, transcribed by a deployer-supplied local
`whisper.cpp` runtime, dispatched into the same Continuity chain as text, and
answered through normal text Delivery/ACK. It also settles terminal Interaction
Plans and empty Skill Plans from final turn evidence.

Version `0.1.1` closes the run-now follow-up path: quoted or recent Automation
context is resolved before execution, unvalidated model-proposed actions fail
closed, and operational replies retain bounded job references for continuity.

Version `0.1.0` adds object-bound Automation conversations: a referenced,
confirmed Delivery can resolve one Automation, clarification preserves the
pending plan, dependent updates run in order, and evidence is presented through
a versioned output contract. Configuration conflicts and ambiguous references
fail closed instead of silently running a different or stale job.

## Product chain

```text
Inbound → Identity/Access → Continuity Turn → Interaction Plan → Skill Plan
→ Capability/Approval/Network Policy → Goal/Run/Task → Evidence/Artifact
→ Delivery/ACK → Outcome → Learning → Assistant Home
```

## Quick start (local, no channel or Provider required)

Use Python 3.10 or newer. The commands below create only local runtime state;
do not add the generated `var/` directory or token files to Git.

```powershell
git clone https://github.com/ABAAOWUGUAI/NekoAgent.git
Set-Location NekoAgent
py -3.10 -m venv .venv
.\.venv\Scripts\python -m pip install --require-hashes -r requirements.lock
New-Item -ItemType Directory -Force .\var\secrets | Out-Null
[guid]::NewGuid().ToString('N') | Set-Content -NoNewline -Encoding ascii .\var\secrets\admin-token
[guid]::NewGuid().ToString('N') | Set-Content -NoNewline -Encoding ascii .\var\secrets\channel-token
Copy-Item .\deploy\bridge.env.example .\deploy\bridge.env.local
.\deploy\start-local.ps1
```

The script imports only the key/value paths in `bridge.env.local` into the
current PowerShell process and then starts the Bridge on `127.0.0.1`. It does
not upload the files, enable a channel, or create a model connection.

After local startup:

1. Configure one model connection and create an Assistant Instance in the
   console. Provider discovery is only a candidate list; validate a selected
   model before binding it to a scene.
2. Optionally integrate AstrBot or Codex by following
   [Integration Guide](docs/INTEGRATIONS.md).
   Owner-private voice messages require the separate, opt-in
   [Voice Input Guide](docs/VOICE_INPUT.md).
3. Run the dependency-free public source Gate at any time:

```powershell
python tools/run_public_tests.py
```

### Optional Starter Packs

Starter Packs are declared resources, never hidden defaults. The included
`starter-packs/xiaofei/` package is an owner-authorized public template for a
name, Persona, PetPack and five expression assets. It is not a platform brand,
model, QQ channel, official account, or real person; it contains no history or
private state. Validate it without writes, then apply it only if you choose:

```powershell
python tools/install_starter_pack.py --pack-dir starter-packs/xiaofei --dry-run
python tools/install_starter_pack.py --pack-dir starter-packs/xiaofei --base-url http://127.0.0.1:18777 --token-file <local-token-file> --apply-to-current
```

The second command creates a Persona Version for the current Assistant and
imports declared visual resources through the existing APIs. It never creates
or activates another Assistant, and never imports relationships, conversations,
memory, knowledge, Goals, Runs, Delivery, Learning, channels, models or
credentials.

The public edition defaults to no QQ channel, no active group participation,
no external Skill download, no raw Shell network access and no automatic
learning of group/global policy. Enable each capability explicitly through the
existing policy and approval controls.

## Security and contribution

Read [SECURITY.md](SECURITY.md) before reporting a vulnerability and
[CONTRIBUTING.md](CONTRIBUTING.md) before submitting a change. Never put a
Provider key, QQ credential, Codex login state, runtime SQLite database or
private conversation into an issue, pull request or repository fork.

Before an Internet-facing deployment, follow the [Deployment Guide](docs/DEPLOYMENT.md)
and [Operations Guide](docs/OPERATIONS.md). Repository administrators should
also complete the [Repository Protection Guide](docs/REPOSITORY_PROTECTION.md);
the presence of a workflow file alone does not protect `main`.

## Documentation

- [中文说明](README.zh-CN.md)
- [中文架构说明](docs/zh-CN/ARCHITECTURE.md)
- [中文集成说明](docs/zh-CN/INTEGRATIONS.md)
- [中文部署指南](docs/zh-CN/DEPLOYMENT.md)
- [中文运行与恢复指南](docs/zh-CN/OPERATIONS.md)
- [中文仓库保护指南](docs/zh-CN/REPOSITORY_PROTECTION.md)
- [Architecture](docs/ARCHITECTURE.md)
- [AstrBot, models and Codex integrations](docs/INTEGRATIONS.md)
- [Deployment guide](docs/DEPLOYMENT.md)
- [Operations and recovery guide](docs/OPERATIONS.md)
- [Repository protection guide](docs/REPOSITORY_PROTECTION.md)
- [Owner-private voice input](docs/VOICE_INPUT.md)
- [Privacy boundary](PRIVACY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Changelog](CHANGELOG.md)
- [License record](LICENSE-DECISION.md)
- [Contribution guide](CONTRIBUTING.md)
