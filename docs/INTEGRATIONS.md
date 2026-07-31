# Integration guide

## OpenAI-compatible model connection

Create a Connection Instance with your own endpoint and credential in the
console, then discover candidate models and run the isolated validation lab.
Only bind a model to chat, planning or vision after validation returns usable
content. Discovery only reads the Provider catalog; it does not prove account
access, quota or Chat Completions compatibility.

## AstrBot channel adapter

Install AstrBot separately, then place `remote-plugin/` in its configured
plugin directory. Set these values in AstrBot's deployment environment, never
in this repository:

```text
ASSISTANT_PLATFORM_BRIDGE_URL=http://bridge-host:18777
ASSISTANT_PLATFORM_CHANNEL_TOKEN_PATH=/run/secrets/assistant-platform-channel-token
```

The bridge must remain reachable only from the intended AstrBot network. Create
the matching QQ Channel and authorized groups through the platform controls;
the plugin does not grant access by itself. Confirm real Delivery ACK before
calling a channel integration successful.

## Codex executor

Install Codex CLI so the `codex` command is available on the deployer's PATH,
then complete the deployer's own login locally. Configure a
`codex_login` executor binding in the platform. The platform must not import,
copy or display Codex cookies, tokens, profile files or home directories.

Codex is optional. Users who only need chat can bind a validated model Provider
without installing Codex. Users who need Web Search must use the explicit
`codex_login` executor and the time-limited Network Policy; a custom Provider
or raw shell is not silently granted network access.
