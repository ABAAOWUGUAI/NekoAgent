"""Allowlisted root-side executor for the restricted Ops Broker."""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml

from bridge_qq_login_status import _NAPCAT_LOGIN_PROBE
from bridge_aiclient_proxy_consumer import (
    AICLIENT_CONTAINER,
    AICLIENT_MODEL_ID,
    AICLIENT_PROVIDER_ID,
    persist_proxy_config,
    runtime_proxy_consumer_status,
)
from bridge_aiclient_proxy_management import (
    AICLIENT_CONFIG_PATH,
    MIHOMO_SUBSCRIPTION_STATE_PATH,
    apply_controlled_config,
    control_surface_status,
    record_test_receipt,
    rollback_controlled_config,
    save_draft,
)
from bridge_proxy_instances import UserSubscriptionStore, safe_subscription_rollback_error

MAX_OUTPUT = 200_000
MAX_QRCODE_BYTES = 1_000_000
NAPCAT_QRCODE_PATH = "/app/napcat/cache/qrcode.png"
LLBOT_QRCODE_PATH = "/opt/agent-stack/llbot/current/bin/llbot/data/temp/login-qrcode.png"
ADMIN_TOKEN_PATH = Path(os.environ.get(
    "ADMIN_TOKEN_PATH",
    "/etc/agent-bridge/secrets/admin-token",
))
CHANNEL_TOKEN_PATH = Path(os.environ.get(
    "CHANNEL_TOKEN_PATH",
    "/etc/agent-bridge/secrets/qq-channel-token",
))
SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|cookie|secret)\s*[=:]\s*)[^\s,;]+"),
)
MIHOMO_PROVIDER_DIR = Path("/etc/mihomo/proxy-providers")
MIHOMO_CONFIG_PATH = Path("/etc/mihomo/config.yaml")
AICLIENT_CONFIG_DIR = Path("/var/lib/aiclient2api/configs")
PROXY_TEST_ROOT = Path("/run/nekoagent/aiclient-proxy-tests")
MIHOMO_IMAGE = "metacubex/mihomo@sha256:23e7666401c91e2e293e01fb2f842b0a75530821c29b8ff2ed79409cd51fc74f"
AICLIENT_IMAGE = "justlikemaki/aiclient-2-api@sha256:66baa01b33e34e1bcbb59f9fe19f77402a96b0b11517763fc0601f2c5919d5d9"
_PROXY_DOMAIN_WRITE_LOCK = threading.RLock()
_PROXY_DOMAIN_WRITE_ACTIONS = frozenset({
    "proxy_subscription_create",
    "proxy_subscription_update",
    "proxy_subscription_refresh",
    "proxy_subscription_switch",
    "proxy_subscription_enable",
    "proxy_subscription_disable",
    "proxy_subscription_delete",
    "proxy_select",
    "aiclient_proxy_save",
    "aiclient_proxy_test",
    "aiclient_proxy_apply",
    "aiclient_proxy_rollback",
})
_CANDIDATE_MODEL_PROBE = r'''
const fs = require('fs');
const net = require('net');
const tls = require('tls');
const config = JSON.parse(fs.readFileSync('/app/configs/config.json', 'utf8'));
const key = String(config.REQUIRED_API_KEY || '');
const endpoint = 'http://127.0.0.1:3000/v1/chat/completions';
const red = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9ZTAAAAABJRU5ErkJggg==';
function classify(status, body, stage) {
  const text = String(body || '').toLowerCase();
  if ([401, 403].includes(status) || /oauth|invalid_grant|unauthori[sz]ed|credential|authentication/.test(text)) return 'oauth_failed';
  if (/region|location/.test(text) && /unsupported|not supported|not available/.test(text)) return 'gemini_region_unsupported';
  if (status === 400) return 'probe_request_invalid';
  if (!status) return stage === 'text' ? 'gemini_text_failed' : 'gemini_vision_failed';
  return 'unexpected_http_status';
}
async function call(content, stage) {
  try {
    const response = await fetch(endpoint, {method:'POST', headers:{'content-type':'application/json','authorization':`Bearer ${key}`}, body:JSON.stringify({model:'gemini-2.5-flash',messages:[{role:'user',content}],max_tokens:12})});
    const body = (await response.text()).slice(0, 16384);
    return {ok:response.ok,status:response.status,error:response.ok?'':classify(response.status, body, stage)};
  } catch (_) { return {ok:false,status:0,error:stage === 'text' ? 'gemini_text_failed' : 'gemini_vision_failed'}; }
}
function google() {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => { if (!settled) { settled = true; resolve(value); } };
    let proxy;
    try { proxy = new URL(String(config.PROXY_URL || '')); } catch (_) { done({ok:false,status:0,error:'proxy_protocol_error'}); return; }
    const socket = net.connect(Number(proxy.port || 7890), proxy.hostname);
    socket.setTimeout(10000);
    socket.once('error', () => done({ok:false,status:0,error:'proxy_protocol_error'}));
    socket.once('timeout', () => { socket.destroy(); done({ok:false,status:0,error:'node_unreachable'}); });
    socket.once('connect', () => socket.write('CONNECT www.google.com:443 HTTP/1.1\r\nHost: www.google.com:443\r\nConnection: close\r\n\r\n'));
    let head = '';
    socket.on('data', function onHead(chunk) {
      head += chunk.toString('latin1');
      if (!head.includes('\r\n\r\n')) return;
      socket.removeListener('data', onHead);
      const status = Number((head.match(/^HTTP\/\d(?:\.\d)?\s+(\d{3})/) || [])[1] || 0);
      if (status !== 200) { socket.destroy(); done({ok:false,status,error:status >= 500 ? 'node_unreachable' : 'proxy_protocol_error'}); return; }
      const secure = tls.connect({socket, servername:'www.google.com'});
      secure.setTimeout(10000);
      secure.once('error', () => done({ok:false,status:0,error:'google_connectivity_failed'}));
      secure.once('timeout', () => { secure.destroy(); done({ok:false,status:0,error:'google_connectivity_failed'}); });
      secure.once('secureConnect', () => secure.write('GET /generate_204 HTTP/1.1\r\nHost: www.google.com\r\nConnection: close\r\n\r\n'));
      let responseHead = '';
      secure.on('data', (part) => {
        responseHead += part.toString('latin1');
        if (!responseHead.includes('\r\n')) return;
        const googleStatus = Number((responseHead.match(/^HTTP\/\d(?:\.\d)?\s+(\d{3})/) || [])[1] || 0);
        secure.destroy();
        done({ok:googleStatus === 204,status:googleStatus,error:googleStatus === 204 ? '' : 'unexpected_http_status'});
      });
    });
  });
}
(async () => {
  const googleResult = await google();
  const text = await call('Reply with OK.', 'text');
  const vision = await call([{type:'text',text:'Reply with RED.'},{type:'image_url',image_url:{url:red}}], 'vision');
  const oauthFailed = text.error === 'oauth_failed' || vision.error === 'oauth_failed';
  const auth = {ok:Boolean(key) && !oauthFailed,error:Boolean(key) && !oauthFailed ? '' : 'oauth_failed'};
  console.log(JSON.stringify({google:googleResult,text,vision,auth}));
})().catch(() => { console.log(JSON.stringify({google:{ok:false},text:{ok:false,error:'probe_failed'},vision:{ok:false,error:'probe_failed'},auth:{ok:false}})); });
'''

_BRIDGE_HEALTH_PROBE = r'''
import urllib.request

# The container-to-host bridge address is fixed by the deployment network.
# Do not accept a caller-provided URL or arbitrary command text here.
with urllib.request.urlopen("/health", timeout=3) as response:
    print(response.read().decode("utf-8"))
'''


def redact_output(value: str) -> str:
    text = str(value or "")[-MAX_OUTPUT:]
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(r"\1[REDACTED]", text)
    return text


def _run(args: list[str], timeout: int) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            args, text=True, capture_output=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        return False, redact_output(str(exc))
    # Status commands must not turn harmless CLI warnings into the state
    # value.  Logs may legitimately use stderr, so stdout remains preferred
    # and stderr is used only when stdout is empty.
    output = redact_output(completed.stdout if (completed.stdout or "").strip() else (completed.stderr or ""))
    return completed.returncode == 0, output


def _run_bytes(args: list[str], timeout: int) -> tuple[bool, bytes, str]:
    try:
        completed = subprocess.run(
            args, capture_output=True, timeout=timeout, check=False,
        )
    except Exception as exc:
        return False, b"", redact_output(str(exc))
    error = redact_output(
        (completed.stderr or b"").decode("utf-8", errors="replace"),
    )
    return completed.returncode == 0, completed.stdout or b"", error


def _managed_active_node(
    subscription_key: str,
    node_name: str,
    *,
    expected_revision: int | None = None,
) -> tuple[dict[str, Any], int]:
    state = json.loads(MIHOMO_SUBSCRIPTION_STATE_PATH.read_text(encoding="utf-8"))
    records = state if isinstance(state, list) else state.get("subscriptions", [])
    revision = 0 if isinstance(state, list) else state.get("management_revision", 0)
    revision = revision if isinstance(revision, int) and not isinstance(revision, bool) else 0
    if expected_revision is not None and expected_revision != revision:
        raise ValueError("subscription_revision_changed")
    record = next((
        item for item in records
        if isinstance(item, dict) and item.get("key") == subscription_key
    ), None)
    if record is None:
        raise ValueError("managed_subscription_not_found")
    if not record.get("active") or not record.get("enabled", True):
        raise ValueError("managed_subscription_not_active")
    provider = str(record.get("provider") or "")
    if not re.fullmatch(r"agent-[a-z0-9_-]{1,48}", provider):
        raise ValueError("managed_provider_invalid")
    document = yaml.safe_load((MIHOMO_PROVIDER_DIR / f"{provider}.yaml").read_text(encoding="utf-8")) or {}
    nodes = document.get("proxies") if isinstance(document, dict) else []
    node = next((
        item for item in nodes
        if isinstance(item, dict) and item.get("name") == node_name
    ), None)
    if node is None:
        raise ValueError("managed_node_not_found")
    return dict(node), revision


def _candidate_node(selection: dict[str, Any]) -> dict[str, Any]:
    node, _ = _managed_active_node(
        str(selection.get("subscription_key") or ""),
        str(selection.get("node") or ""),
    )
    return node


def _proxy_select(args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("subscription_key") or "")
    node = str(args.get("node") or "")
    try:
        _, revision = _managed_active_node(
            key,
            node,
            expected_revision=int(args.get("expected_revision", -1)),
        )
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        error = str(exc)
        safe = error if re.fullmatch(r"[a-z0-9_:-]{1,96}", error) else "managed_proxy_state_invalid"
        return {"ok": False, "changed": False, "error": safe}

    snapshot_ok, snapshot = _get_mihomo_selection("Proxies")
    if not snapshot_ok:
        return {
            "ok": False,
            "changed": False,
            "error": "mihomo_selection_snapshot_failed",
            "rolled_back": False,
            "rollback_error": "",
        }
    if snapshot == node:
        return {
            "ok": True,
            "changed": False,
            "subscription_key": key,
            "group": "Proxies",
            "node": node,
            "revision": revision,
        }

    switched, _ = _set_mihomo_selection("Proxies", node)
    readback_ok, selected = _get_mihomo_selection("Proxies")
    if switched and readback_ok and selected == node:
        return {
            "ok": True,
            "changed": True,
            "subscription_key": key,
            "group": "Proxies",
            "node": node,
            "revision": revision,
        }

    if not switched:
        error = "mihomo_selection_apply_failed"
    elif not readback_ok:
        error = "mihomo_selection_readback_failed"
    else:
        error = "mihomo_selection_readback_mismatch"

    restored, _ = _set_mihomo_selection("Proxies", snapshot)
    restore_readback_ok, restore_selected = _get_mihomo_selection("Proxies")
    rolled_back = restore_readback_ok and restore_selected == snapshot
    if rolled_back:
        rollback_error = ""
    elif not restored:
        rollback_error = "mihomo_selection_restore_failed"
    elif not restore_readback_ok:
        rollback_error = "mihomo_selection_restore_readback_failed"
    else:
        rollback_error = "mihomo_selection_restore_readback_mismatch"
    return {
        "ok": False,
        "changed": False,
        "error": error,
        "rolled_back": rolled_back,
        "rollback_error": rollback_error,
    }


def _candidate_failure(error: str) -> dict[str, Any]:
    safe = re.sub(r"[^a-zA-Z0-9_:-]", "_", str(error or "candidate_test_failed"))[:96]
    return {
        "google": {"ok": False, "status": 0, "error": safe},
        "text": {"ok": False, "status": 0, "error": safe},
        "vision": {"ok": False, "status": 0, "error": safe},
        "auth": {"ok": False, "error": safe},
    }


def _subscription_store() -> UserSubscriptionStore:
    def config_test() -> tuple[bool, str]:
        return _run(["docker", "exec", "mihomo", "/mihomo", "-t", "-d", "/root/.config/mihomo"], 30)

    def reload_config() -> tuple[bool, str]:
        import urllib.request

        request = urllib.request.Request(
            "http://127.0.0.1:9090/configs?force=true",
            data=json.dumps({"path": "/root/.config/mihomo/config.yaml"}).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status in {200, 204}, ""
        except Exception:
            return False, "mihomo_reload_failed"

    return UserSubscriptionStore(
        config_path=MIHOMO_CONFIG_PATH,
        state_path=MIHOMO_SUBSCRIPTION_STATE_PATH,
        provider_dir=MIHOMO_PROVIDER_DIR,
        backup_root=Path(os.environ.get("AGENT_BACKUP_ROOT", "/opt/agent-stack/backups")),
        config_test=config_test,
        reload_config=reload_config,
    )


def _subscription_dependencies(key: str) -> list[str]:
    dependencies: list[str] = []
    try:
        raw = json.loads(MIHOMO_SUBSCRIPTION_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    records = raw if isinstance(raw, list) else raw.get("subscriptions", [])
    records = records if isinstance(records, list) else []
    target = next(
        (item for item in records if isinstance(item, dict) and item.get("key") == key),
        {},
    )
    if target.get("active"):
        dependencies.append("current_selector")
    enabled = [
        item
        for item in records
        if isinstance(item, dict) and bool(item.get("enabled", True))
    ]
    if bool(target.get("enabled", True)) and len(enabled) <= 1:
        dependencies.append("only_enabled_subscription")
    try:
        management = control_surface_status(AICLIENT_CONFIG_PATH, MIHOMO_SUBSCRIPTION_STATE_PATH)
    except Exception:
        management = {}
    for source, code in (("draft", "consumer_draft"), ("applied", "consumer_applied")):
        value = management.get(source)
        value = value if isinstance(value, dict) else {}
        selection = value.get("selection") if isinstance(value.get("selection"), dict) else value
        if str(selection.get("subscription_key") or "") == key:
            dependencies.append(code)
    return list(dict.fromkeys(dependencies))


def _subscription_write(action: str, args: dict[str, Any]) -> dict[str, Any]:
    store = _subscription_store()
    key = str(args.get("subscription_key") or "")
    with _PROXY_DOMAIN_WRITE_LOCK:
        dependencies = _subscription_dependencies(key) if action in {
            "proxy_subscription_disable", "proxy_subscription_delete",
        } else []
        if dependencies:
            return {
                "ok": False,
                "changed": False,
                "subscription_key": key[:128],
                "error": "dependency_in_use",
                "error_kind": "dependency_in_use",
                "dependencies": dependencies,
                "transaction_stage": "dependency_check",
            }
        if action == "proxy_subscription_create":
            result = store.create(
                str(args["name"]),
                str(args["url"]),
                enabled=bool(args["enabled"]),
                expected_revision=int(args["expected_revision"]),
            )
        elif action == "proxy_subscription_update":
            result = store.update(
                key,
                str(args["name"]),
                str(args.get("url") or ""),
                url_update_present=bool(args["url_update_present"]),
                enabled=bool(args["enabled"]),
                expected_revision=int(args["expected_revision"]),
            )
        elif action == "proxy_subscription_refresh":
            result = store.refresh(key, expected_revision=int(args["expected_revision"]))
        elif action == "proxy_subscription_enable":
            result = store.enable(key, expected_revision=int(args["expected_revision"]))
        elif action == "proxy_subscription_disable":
            result = store.disable(key, expected_revision=int(args["expected_revision"]))
        elif action == "proxy_subscription_delete":
            result = store.delete(key, expected_revision=int(args["expected_revision"]))
        else:
            result = store.switch(key, expected_revision=int(args["expected_revision"]))
    error = re.sub(r"[^a-zA-Z0-9_:-]", "_", str(result.get("error") or ""))[:96]
    rollback_error = safe_subscription_rollback_error(result.get("rollback_error"))
    subscription = result.get("subscription") if isinstance(result.get("subscription"), dict) else {}
    safe_subscription = {
        field: subscription.get(field)
        for field in (
            "key", "name", "provider", "group", "format", "node_count",
            "enabled", "active", "created_at", "updated_at", "last_status",
        )
        if field in subscription
    }
    response = {
        "ok": bool(result.get("ok")),
        "changed": bool(result.get("changed", result.get("ok"))),
        "subscription_key": str(result.get("key") or safe_subscription.get("key") or key)[:128],
        "error": error,
        "error_kind": error,
        "revision": int(result.get("revision") or result.get("current_revision") or 0),
        "current_revision": int(result.get("current_revision") or result.get("revision") or 0),
        "transaction_stage": str(result.get("transaction_stage") or "")[:64],
        "rolled_back": bool(result.get("rolled_back")),
        "rollback_error": rollback_error,
        "backup_id": str(result.get("backup_id") or "")[:160],
        "node_count": int(result.get("node_count") or 0),
        "refreshed_at": str(result.get("refreshed_at") or "")[:64],
        "dependencies": [
            str(item)[:64] for item in (result.get("dependencies") or dependencies)
        ][:12],
    }
    if safe_subscription:
        response["subscription"] = safe_subscription
    response["receipt"] = {
        "action": action,
        "stage": response["transaction_stage"],
        "revision": response["revision"],
        "rolled_back": response["rolled_back"],
        "rollback_error": response["rollback_error"],
        "backup_id": response["backup_id"],
    }
    return response


def _run_candidate_test(selection: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    """Run a fixed disposable chain without touching production selectors."""

    test_id = os.urandom(6).hex()
    mihomo_name = f"neko-proxy-test-m-{test_id}"
    aiclient_name = f"neko-proxy-test-a-{test_id}"
    PROXY_TEST_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(tempfile.mkdtemp(prefix=f"{test_id}-", dir=str(PROXY_TEST_ROOT)))
    try:
        node = _candidate_node(selection)
        mihomo_dir = root / "mihomo"
        aiclient_dir = root / "aiclient"
        mihomo_dir.mkdir(mode=0o700)
        shutil.copytree(AICLIENT_CONFIG_DIR, aiclient_dir, symlinks=False)
        config = json.loads((aiclient_dir / "config.json").read_text(encoding="utf-8"))
        config.update({
            "PROXY_URL": f"http://{mihomo_name}:7890",
            "PROXY_ENABLED_PROVIDERS": ["gemini-antigravity"],
            "MODEL_FALLBACK_ENABLED": False,
            "providerFallbackChain": {},
            "modelFallbackMapping": {},
        })
        (aiclient_dir / "config.json").write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        mihomo_config = {
            "mixed-port": 7890,
            "allow-lan": True,
            "mode": "rule",
            "log-level": "warning",
            "proxies": [node],
            "proxy-groups": [{"name": "CANDIDATE", "type": "select", "proxies": [selection["node"]]}],
            "rules": ["MATCH,CANDIDATE"],
        }
        (mihomo_dir / "config.yaml").write_text(
            yaml.safe_dump(mihomo_config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        commands = [
            ["docker", "run", "-d", "--rm", "--name", mihomo_name, "--network", "neko-proxy-egress", "--network-alias", mihomo_name, "-v", f"{mihomo_dir}:/root/.config/mihomo:ro", MIHOMO_IMAGE],
            ["docker", "run", "-d", "--rm", "--name", aiclient_name, "--network", "neko-proxy-egress", "--network-alias", aiclient_name, "-v", f"{aiclient_dir}:/app/configs", "-e", "ARGS=", AICLIENT_IMAGE],
        ]
        for command in commands:
            ok, output = _run(command, min(timeout, 30))
            if not ok:
                return _candidate_failure(output or "candidate_container_start_failed")
        deadline = time.monotonic() + min(timeout, 45)
        healthy = False
        while time.monotonic() < deadline:
            healthy, _ = _run(["docker", "exec", aiclient_name, "node", "healthcheck.js"], 5)
            if healthy:
                break
            time.sleep(1)
        if not healthy:
            return _candidate_failure("candidate_aiclient_unhealthy")
        ok, output = _run(
            ["docker", "exec", aiclient_name, "node", "-e", _CANDIDATE_MODEL_PROBE],
            min(timeout, 75),
        )
        if not ok:
            return _candidate_failure(output or "candidate_model_probe_failed")
        try:
            result = json.loads(output.strip().splitlines()[-1])
        except Exception:
            return _candidate_failure("candidate_probe_result_invalid")
        return result if isinstance(result, dict) else _candidate_failure("candidate_probe_result_invalid")
    except Exception as exc:
        return _candidate_failure(str(exc))
    finally:
        _run(["docker", "rm", "-f", aiclient_name, mihomo_name], 20)
        shutil.rmtree(root, ignore_errors=True)


def _runtime_gate_status() -> dict[str, Any]:
    status = runtime_proxy_consumer_status({
        "model_registry_provider_id": AICLIENT_PROVIDER_ID,
        "model_registry_id": AICLIENT_MODEL_ID,
    })
    return {
        "network_ready": bool(status.get("shared_network_attached")),
        "mihomo_healthy": bool((status.get("mihomo") or {}).get("running") and (status.get("mihomo") or {}).get("upstream_ok")),
        "aiclient_healthy": bool((status.get("aiclient") or {}).get("running") and (status.get("aiclient") or {}).get("health") == "healthy"),
        "direct_allowed": bool(status.get("direct_allowed")),
        "fallback_allowed": bool(status.get("fallback_allowed")),
        "provider_fallback_allowed": bool(status.get("provider_fallback_allowed")),
        "model_fallback_allowed": bool(status.get("model_fallback_allowed")),
        "credential_configured": bool(status.get("credential_configured")),
    }


def _set_mihomo_selection(group: str, node: str) -> tuple[bool, str]:
    import urllib.parse
    import urllib.request

    payload = json.dumps({"name": node}).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:9090/proxies/{urllib.parse.quote(group, safe='')}",
        data=payload,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status in {200, 204}, ""
    except Exception as exc:
        return False, redact_output(str(exc))


def _get_mihomo_selection(group: str = "Proxies") -> tuple[bool, str]:
    import urllib.parse
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:9090/proxies/{urllib.parse.quote(group, safe='')}",
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            document = json.loads(response.read(262_144).decode("utf-8"))
        selected = str(document.get("now") or "").strip() if isinstance(document, dict) else ""
        if not selected:
            return False, "mihomo_selection_snapshot_invalid"
        return True, selected
    except Exception as exc:
        return False, redact_output(str(exc))


def _aiclient_compose_commands() -> list[list[str]]:
    return [
        [
            "/usr/bin/docker-compose", "-p", "mihomo", "-f",
            "/opt/agent-stack/mihomo/compose.yml", "up", "-d",
        ],
        [
            "/usr/bin/docker-compose", "-p", "aiclient2api", "-f",
            "/opt/agent-stack/aiclient2api/compose.yml", "up", "-d",
        ],
    ]


def _restart_aiclient_runtime() -> tuple[bool, str]:
    return _run(
        ["/usr/bin/docker", "restart", "--time", "20", AICLIENT_CONTAINER],
        30,
    )


def _aiclient_proxy_runtime_error(expected_config: bytes, expected_node: str) -> str:
    try:
        if AICLIENT_CONFIG_PATH.read_bytes() != expected_config:
            return "aiclient_config_readback_mismatch"
    except OSError:
        return "aiclient_config_readback_failed"
    selected, current_node = _get_mihomo_selection("Proxies")
    if not selected:
        return current_node or "mihomo_selection_readback_failed"
    if current_node != expected_node:
        return "mihomo_selection_readback_mismatch"
    status = _runtime_gate_status()
    checks = (
        (not status.get("direct_allowed"), "aiclient_direct_forbidden"),
        (not status.get("fallback_allowed"), "aiclient_fallback_forbidden"),
        (not status.get("provider_fallback_allowed"), "aiclient_provider_fallback_forbidden"),
        (not status.get("model_fallback_allowed"), "aiclient_model_fallback_forbidden"),
        (bool(status.get("credential_configured")), "aiclient_credential_missing"),
        (bool(status.get("network_ready")), "aiclient_proxy_network_unready"),
        (bool(status.get("mihomo_healthy")), "mihomo_upstream_unavailable"),
        (bool(status.get("aiclient_healthy")), "aiclient_unhealthy"),
    )
    return next((error for ok, error in checks if not ok), "")


def _wait_aiclient_proxy_runtime(
    expected_config: bytes,
    expected_node: str,
    *,
    timeout_seconds: float = 20,
) -> tuple[bool, str]:
    deadline = time.monotonic() + max(0.0, min(float(timeout_seconds), 30.0))
    last_error = "aiclient_runtime_verification_failed"
    while True:
        last_error = _aiclient_proxy_runtime_error(expected_config, expected_node)
        if not last_error:
            return True, ""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, last_error
        time.sleep(min(0.5, remaining))


def _atomic_restore_bytes(path: Path, payload: bytes) -> None:
    path = Path(path)
    current = path.stat()
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, stat.S_IMODE(current.st_mode))
        if hasattr(os, "fchown") and os.name != "nt":
            os.fchown(descriptor, current.st_uid, current.st_gid)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _compensate_aiclient_proxy_transaction(
    config_before: bytes,
    selector_before: str,
) -> tuple[bool, str]:
    errors: list[str] = []
    try:
        _atomic_restore_bytes(AICLIENT_CONFIG_PATH, config_before)
    except Exception:
        errors.append("aiclient_config_restore_failed")
    for command in _aiclient_compose_commands():
        recovered, output = _run(command, 30)
        if not recovered:
            errors.append(output or "compose_runtime_restore_failed")
    restarted, output = _restart_aiclient_runtime()
    if not restarted:
        errors.append(output or "aiclient_runtime_restart_restore_failed")
    restored, output = _set_mihomo_selection("Proxies", selector_before)
    if not restored:
        errors.append(output or "mihomo_selection_restore_failed")
    verified, output = _wait_aiclient_proxy_runtime(config_before, selector_before)
    if not verified:
        errors.append(output or "aiclient_runtime_restore_verification_failed")
    safe_errors = [redact_output(str(error))[:160] for error in errors]
    return not safe_errors, ";".join(safe_errors)


def _replace_admin_token(token: str) -> dict[str, Any]:
    """Atomically replace the one fixed admin secret without invoking a shell."""

    path = ADMIN_TOKEN_PATH
    try:
        channel_token = CHANNEL_TOKEN_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return {"ok": False, "error": "channel_token_file_missing"}
    if channel_token and hmac.compare_digest(token.encode("utf-8"), channel_token.encode("utf-8")):
        return {"ok": False, "error": "admin_token_matches_channel_token"}
    if path.is_symlink():
        return {"ok": False, "error": "admin_token_symlink_forbidden"}
    try:
        current = path.stat()
    except OSError:
        return {"ok": False, "error": "admin_token_file_missing"}
    mode = stat.S_IMODE(current.st_mode)
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or mode not in {0o600, 0o640}
    ):
        return {"ok": False, "error": "admin_token_file_policy_invalid"}

    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp")
    descriptor: int | None = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temp_path, flags, mode)
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, current.st_uid, current.st_gid)
        payload = (token + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        readback = path.read_text(encoding="utf-8").strip()
        if not hmac.compare_digest(readback.encode("utf-8"), token.encode("utf-8")):
            return {"ok": False, "error": "admin_token_readback_failed"}
        return {"ok": True, "changed": True}
    except OSError:
        return {"ok": False, "error": "admin_token_write_failed"}
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def execute_read(request: dict[str, Any]) -> dict[str, Any]:
    action = request["action"]
    target = request["target"]
    args = request.get("args") or {}
    timeout = int(args.get("timeout_seconds") or 8)
    if action == "aiclient_proxy_status" and target == "aiclient2api":
        return runtime_proxy_consumer_status({
            "model_registry_provider_id": AICLIENT_PROVIDER_ID,
            "model_registry_id": AICLIENT_MODEL_ID,
        })
    if action == "service_status":
        ok, output = _run(["systemctl", "is-active", target], min(timeout, 10))
        return {"ok": ok and output.strip() == "active", "target": target, "status": output.strip() or "unknown"}
    if action == "service_logs":
        lines = int(args.get("lines") or 100)
        ok, output = _run(["journalctl", "-u", target, "-n", str(lines), "--no-pager"], min(timeout, 30))
        return {"ok": ok, "target": target, "lines": lines, "output": output}
    if action == "container_status":
        ok, output = _run(["docker", "inspect", "-f", "{{.State.Status}}", target], min(timeout, 10))
        return {"ok": ok and output.strip() == "running", "target": target, "status": output.strip() or "unknown"}
    if action == "container_list" and target == "docker":
        ok, output = _run(["docker", "ps", "--format", "{{json .}}"], min(timeout, 15))
        containers = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            containers.append({
                "id": item.get("ID", ""),
                "name": item.get("Names", ""),
                "image": item.get("Image", ""),
                "status": item.get("Status", ""),
                "state": item.get("State", ""),
                "ports": item.get("Ports", ""),
                "running_for": item.get("RunningFor", ""),
            })
        return {"ok": ok, "target": target, "containers": containers}
    if action == "container_logs":
        lines = int(args.get("lines") or 100)
        ok, output = _run(["docker", "logs", "--tail", str(lines), target], min(timeout, 30))
        return {"ok": ok, "target": target, "lines": lines, "output": output}
    if action == "container_env":
        name = str(args.get("name") or "")
        ok, output = _run(["docker", "exec", target, "printenv", name], min(timeout, 10))
        return {"ok": ok, "target": target, "name": name, "value": output.strip() if ok else ""}
    if action == "container_file_exists":
        path = str(args.get("path") or "")
        ok, _ = _run(["docker", "exec", target, "test", "-s", path], min(timeout, 10))
        return {"ok": ok, "target": target, "path": path, "exists": ok}
    if action == "qq_qrcode_info":
        if target == "llbot":
            ok, output = _run(
                ["stat", "-Lc", "%s %Y %n", LLBOT_QRCODE_PATH],
                min(timeout, 10),
            )
            qrcode_path = LLBOT_QRCODE_PATH
        elif target == "maim-bot-napcat":
            ok, output = _run(
                [
                    "docker", "exec", target, "sh", "-lc",
                    (
                        "if [ -s /app/napcat/cache/qrcode.png ]; then "
                        "stat -c '%s %Y %n' /app/napcat/cache/qrcode.png; "
                        "else exit 1; fi"
                    ),
                ],
                min(timeout, 10),
            )
            qrcode_path = NAPCAT_QRCODE_PATH
        else:
            return {"ok": False, "error": "qq_qrcode_target_forbidden"}
        if not ok:
            return {"ok": False, "target": target, "path": qrcode_path}
        parts = output.split(maxsplit=2)
        if len(parts) != 3:
            return {"ok": False, "target": target, "path": qrcode_path}
        try:
            size = int(parts[0])
            mtime = int(float(parts[1]))
        except ValueError:
            return {"ok": False, "target": target, "path": qrcode_path}
        return {
            "ok": size > 0,
            "target": target,
            "path": parts[2],
            "size": size,
            "mtime": mtime,
        }
    if action == "qq_qrcode_png":
        if target == "llbot":
            qrcode_path = LLBOT_QRCODE_PATH
            command = ["cat", qrcode_path]
        elif target == "maim-bot-napcat":
            qrcode_path = NAPCAT_QRCODE_PATH
            command = ["docker", "exec", target, "cat", qrcode_path]
        else:
            return {"ok": False, "error": "qq_qrcode_target_forbidden"}
        ok, payload, error = _run_bytes(
            command,
            min(timeout, 10),
        )
        if not ok:
            return {"ok": False, "target": target, "error": error or "qrcode_read_failed"}
        if not payload or len(payload) > MAX_QRCODE_BYTES:
            return {"ok": False, "target": target, "error": "qrcode_size_invalid"}
        return {
            "ok": True,
            "target": target,
            "path": qrcode_path,
            "content_base64": base64.b64encode(payload).decode("ascii"),
        }
    if action == "qq_login_probe":
        ok, output = _run(["docker", "exec", target, "python3", "-c", _NAPCAT_LOGIN_PROBE], min(timeout, 20))
        return {"ok": ok, "target": target, "output": output}
    if action == "container_bridge_probe":
        ok, output = _run(["docker", "exec", target, "python3", "-c", _BRIDGE_HEALTH_PROBE], min(timeout, 10))
        return {"ok": ok and '"ok": true' in output.lower(), "target": target, "output": output}
    if action == "config_test" and target == "mihomo":
        ok, output = _run(
            ["docker", "exec", "mihomo", "/mihomo", "-t", "-d", "/root/.config/mihomo"],
            min(timeout, 30),
        )
        return {"ok": ok, "target": target, "output": output}
    return {"ok": False, "error": "ops_read_action_unimplemented"}


def _execute_write_unlocked(request: dict[str, Any]) -> dict[str, Any]:
    """Execute only fixed argv or bounded, product-owned plugin operations."""

    action = request["action"]
    target = request["target"]
    args = request.get("args") or {}
    if action == "service_restart":
        ok, output = _run(["systemctl", "restart", f"{target}.service"], 30)
        return {"ok": ok, "target": target, "restarted": ok, "error": "" if ok else output}
    if action == "container_restart":
        ok, output = _run(["docker", "restart", "--time", "20", target], 30)
        return {"ok": ok, "target": target, "restarted": ok, "error": "" if ok else output}
    if action == "proxy_reload":
        ok, output = _run(["docker", "kill", "--signal", "HUP", "mihomo"], 20)
        return {"ok": ok, "target": target, "reloaded": ok, "error": "" if ok else output}
    if action == "astrbot_plugin_set_enabled":
        from bridge_capability_registry import set_plugin_enabled

        return set_plugin_enabled(str(args["plugin_id"]), bool(args["enabled"]))
    if action == "astrbot_plugin_operate":
        from bridge_plugin_marketplace import operate_market_plugin

        database = os.environ.get("ASSISTANT_DB_PATH", "/var/lib/agent-bridge/assistant.sqlite3")
        with sqlite3.connect(database, timeout=20) as conn:
            conn.row_factory = sqlite3.Row
            return operate_market_plugin(conn, {
                "action": str(args["operation"]),
                "plugin_id": str(args["plugin_id"]),
                "confirm_risk": True,
            })
    if action == "admin_token_rotate" and target == "bridge-admin-token":
        return _replace_admin_token(str(args["new_token"]))
    if action.startswith("proxy_subscription_") and target == "mihomo":
        return _subscription_write(action, args)
    if action == "proxy_select" and target == "mihomo":
        return _proxy_select(args)
    if action == "aiclient_proxy_save" and target == "aiclient2api":
        return save_draft(AICLIENT_CONFIG_PATH, MIHOMO_SUBSCRIPTION_STATE_PATH, args)
    if action == "aiclient_proxy_test" and target == "aiclient2api":
        expected_revision = int(args["expected_revision"])
        management = control_surface_status(AICLIENT_CONFIG_PATH, MIHOMO_SUBSCRIPTION_STATE_PATH)
        if expected_revision != int(management.get("revision") or 0):
            return {"ok": False, "error": "consumer_revision_conflict", "revision": management.get("revision", 0)}
        selection = management.get("draft") if isinstance(management.get("draft"), dict) else {}
        if not selection:
            return {"ok": False, "error": "consumer_draft_not_saved", "revision": expected_revision}
        probe = _run_candidate_test(selection, int(args.get("timeout_seconds") or 120))
        return record_test_receipt(
            AICLIENT_CONFIG_PATH,
            MIHOMO_SUBSCRIPTION_STATE_PATH,
            expected_revision=expected_revision,
            result=probe,
        )
    if action == "aiclient_proxy_apply" and target == "aiclient2api":
        config_before = AICLIENT_CONFIG_PATH.read_bytes()
        snapshot_ok, selector_before = _get_mihomo_selection("Proxies")
        if not snapshot_ok:
            return {
                "ok": False,
                "error": selector_before or "mihomo_selection_snapshot_failed",
                "rolled_back": False,
                "rollback_error": "",
            }
        saved = apply_controlled_config(
            AICLIENT_CONFIG_PATH,
            MIHOMO_SUBSCRIPTION_STATE_PATH,
            _runtime_gate_status(),
            expected_revision=int(args["expected_revision"]),
        )
        if not saved.get("ok"):
            return saved
        try:
            candidate_config = AICLIENT_CONFIG_PATH.read_bytes()
        except OSError:
            rolled_back, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "error": "aiclient_config_readback_failed",
                "rolled_back": rolled_back,
                "rollback_error": rollback_error,
            }
        selection = (saved.get("applied") or {}).get("selection") or {}
        switched, switch_error = _set_mihomo_selection(
            "Proxies",
            str(selection.get("node") or ""),
        )
        if not switched:
            rolled_back, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "error": switch_error or "mihomo_selection_apply_failed",
                "rolled_back": rolled_back,
                "rollback_error": rollback_error,
            }
        for command in _aiclient_compose_commands():
            ok, output = _run(command, 30)
            if not ok:
                rolled_back, rollback_error = _compensate_aiclient_proxy_transaction(
                    config_before,
                    selector_before,
                )
                return {
                    "ok": False,
                    "error": output or "aiclient_proxy_compose_apply_failed",
                    "rolled_back": rolled_back,
                    "rollback_error": rollback_error,
                }
        restarted, output = _restart_aiclient_runtime()
        if not restarted:
            rolled_back, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "error": output or "aiclient_proxy_restart_apply_failed",
                "rolled_back": rolled_back,
                "rollback_error": rollback_error,
            }
        verified, verification_error = _wait_aiclient_proxy_runtime(
            candidate_config,
            str(selection.get("node") or ""),
        )
        if not verified:
            rolled_back, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "error": verification_error or "aiclient_runtime_verification_failed",
                "rolled_back": rolled_back,
                "rollback_error": rollback_error,
            }
        return {
            "ok": True,
            "changed": bool(saved.get("changed")),
            "applied": True,
            "runtime_verified": True,
        }
    if action == "aiclient_proxy_rollback" and target == "aiclient2api":
        config_before = AICLIENT_CONFIG_PATH.read_bytes()
        snapshot_ok, selector_before = _get_mihomo_selection("Proxies")
        if not snapshot_ok:
            return {
                "ok": False,
                "error": selector_before or "mihomo_selection_snapshot_failed",
                "rolled_back": False,
                "rollback_error": "",
            }
        result = rollback_controlled_config(
            AICLIENT_CONFIG_PATH,
            expected_revision=int(args["expected_revision"]),
        )
        if not result.get("ok"):
            return result
        try:
            candidate_config = AICLIENT_CONFIG_PATH.read_bytes()
        except OSError:
            restored, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "error": "aiclient_config_readback_failed",
                "rolled_back": restored,
                "rollback_error": rollback_error,
            }
        selection = (result.get("applied") or {}).get("selection") or {}
        switched, error = _set_mihomo_selection(
            "Proxies",
            str(selection.get("node") or ""),
        )
        if not switched:
            restored, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "error": error or "mihomo_selection_rollback_failed",
                "rolled_back": restored,
                "rollback_error": rollback_error,
            }
        ok, output = _run([
            "/usr/bin/docker-compose", "-p", "aiclient2api", "-f",
            "/opt/agent-stack/aiclient2api/compose.yml", "up", "-d",
        ], 30)
        if not ok:
            restored, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "rolled_back": restored,
                "error": output or "aiclient_proxy_compose_rollback_failed",
                "rollback_error": rollback_error,
            }
        restarted, output = _restart_aiclient_runtime()
        if not restarted:
            restored, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "rolled_back": restored,
                "error": output or "aiclient_proxy_restart_rollback_failed",
                "rollback_error": rollback_error,
            }
        verified, verification_error = _wait_aiclient_proxy_runtime(
            candidate_config,
            str(selection.get("node") or ""),
        )
        if not verified:
            restored, rollback_error = _compensate_aiclient_proxy_transaction(
                config_before,
                selector_before,
            )
            return {
                "ok": False,
                "error": verification_error or "aiclient_runtime_verification_failed",
                "rolled_back": restored,
                "rollback_error": rollback_error,
            }
        return {
            "ok": True,
            "rolled_back": True,
            "error": "",
            "rollback_error": "",
            "runtime_verified": True,
        }
    return {"ok": False, "error": "ops_write_action_unimplemented"}


def execute_write(request: dict[str, Any]) -> dict[str, Any]:
    """Serialize every proxy-domain mutation behind one revision barrier."""

    if str(request.get("action") or "") in _PROXY_DOMAIN_WRITE_ACTIONS:
        with _PROXY_DOMAIN_WRITE_LOCK:
            return _execute_write_unlocked(request)
    return _execute_write_unlocked(request)


def execute(request: dict[str, Any]) -> dict[str, Any]:
    """Executor entry point."""

    if request.get("action") in {
        "service_status", "service_logs", "container_status", "container_list",
        "container_logs", "container_env", "container_file_exists", "qq_login_probe",
        "container_bridge_probe", "config_test", "qq_qrcode_info", "qq_qrcode_png",
        "aiclient_proxy_status",
    }:
        return execute_read(request)
    return execute_write(request)


__all__ = ["execute", "execute_read", "execute_write", "redact_output"]
