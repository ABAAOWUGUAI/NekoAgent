# NekoAgent

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

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [AstrBot, models and Codex integrations](docs/INTEGRATIONS.md)
- [Privacy boundary](PRIVACY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Changelog](CHANGELOG.md)
- [License record](LICENSE-DECISION.md)
- [Contribution guide](CONTRIBUTING.md)
