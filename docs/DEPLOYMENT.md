# Deployment guide

This guide deploys a fresh NekoAgent instance. It does not migrate an existing
private installation, copy an Assistant, reuse a model key, or turn on a QQ
channel. Start with the loopback-only configuration and enable integrations one
at a time.

## 1. Choose a deployment shape

| Shape | Use it for | What remains disabled by default |
|---|---|---|
| Local Bridge | Console evaluation and model setup | Channels, group participation, Codex work execution |
| Bridge plus AstrBot | A deployer-owned chat channel | Access to every group until the channel and group policy are explicitly configured |
| Bridge plus Codex | Approved local work execution | Network access, shell access and automatic approvals |

AstrBot and Codex are optional integrations. They are not bundled credentials,
and neither one changes the ownership of Assistant, conversation, memory,
Goal, Run, Delivery or Learning state.

## 2. Prepare the host

- Use Python 3.10 or newer and create a dedicated operating-system account for
  the service in a non-development deployment.
- Keep the repository, runtime data and secrets on storage that is backed up by
  the operator. Do not keep runtime state inside a source checkout that will be
  copied or forked.
- Bind the Bridge to `127.0.0.1` for first start. Do not expose its Admin API,
  SQLite files or artifact directory directly to the Internet.
- Give the runtime and secret directories access only to the service account.
  Secret files should be readable by that account only.

For a local Windows bootstrap:

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

The generated values stay in local files. Do not paste them into an issue,
chat, shell history, repository, screenshot or prompt.

## 3. Configure one local runtime

`deploy/bridge.env.example` is the configuration reference. Copy it to
`deploy/bridge.env.local`, which is ignored by Git, and change paths for the
host. The bootstrap script validates that `TOKEN_PATH` and
`CHANNEL_TOKEN_PATH` point to two existing local files before it starts.

Keep these safe defaults until an operator has a specific reason to change
them:

| Setting | Safe starting value | Reason |
|---|---|---|
| `LISTEN_HOST` | `127.0.0.1` | Prevents direct public Admin exposure |
| `ALLOW_PUBLIC_TOKEN_AUTH` | `0` | Avoids token authentication on a public listener |
| `OPS_BROKER_REQUIRED` | `0` for local bootstrap | Keeps an optional operations broker out of the first start |
| `ADMIN_COOKIE_SECURE` | `1` behind HTTPS | Prevents insecure session cookies in protected deployments |
| database and artifact paths | a private `var/` directory | Keeps mutable state outside source control |

If a reverse proxy is later required, terminate TLS there, restrict upstream
access to loopback or a private network, enable its access logging and rate
limits, and keep the Bridge listener private. A reverse proxy is not a reason
to remove application authentication or approval gates.

## 4. Validate before adding an integration

1. In a separate shell, run `python tools/run_public_tests.py`.
2. Check the local health and admin-version endpoints through the loopback
   listener; a healthy process alone does not prove a channel can deliver.
3. In the console, add one model connection, discover candidate model IDs and
   run isolated validation. A catalog entry is not proof that the deployer's
   account can produce a usable response.
4. Create one Assistant Instance or explicitly install an optional Starter
   Pack. Installing a pack must not import another person's conversations,
   memories, channels, models or credentials.

Do not configure a Provider, channel and executor in one unverified jump.
Validate each boundary separately so failures have an observable cause.

## 5. Optional AstrBot adapter

Install AstrBot under the deployer's own account and plugin lifecycle. Put the
contents of `remote-plugin/` in AstrBot's configured plugin directory, then set
only deployment-local values such as:

```text
ASSISTANT_PLATFORM_BRIDGE_URL=http://bridge-host:18777
ASSISTANT_PLATFORM_CHANNEL_TOKEN_PATH=/run/secrets/assistant-platform-channel-token
```

The Bridge URL must be reachable only from the intended AstrBot network. The
adapter translates events and Delivery acknowledgements; it does not grant
channel, group, approval or model access. Verify an authorized inbound message
through `Delivery confirmed ACK` before describing the channel as operational.

## 6. Optional Codex executor

Install Codex on the deployer's host and complete that deployer's login locally.
Expose the `codex` command through the service account's `PATH`, then create an
explicit `codex_login` executor binding in the console. Never copy a Codex
profile, cookie, token or home directory into NekoAgent.

Web Search and other networked work require the platform's explicit
Network Policy, approval and audit path. A custom model Provider or arbitrary
shell command must not receive network access merely because Codex is present.

## 7. Production hand-off checklist

Before allowing real users or channels:

- Confirm loopback/private-network binding, TLS termination and firewall rules.
- Confirm token files, Provider secret files and runtime directories are not
  readable by other host users or source-control tools.
- Configure a tested backup and restore path for SQLite and artifacts.
- Keep group participation disabled until the exact groups, members, policy and
  frequency limits are explicitly authorized.
- Run one real but low-risk end-to-end request and retain evidence for
  Inbound, policy decision, Delivery ACK and Outcome. `sent` alone is not a
  confirmed delivery or a completed user Goal.

For routine upgrades, incident response and backup/restore rules, continue to
the [Operations Guide](OPERATIONS.md).
