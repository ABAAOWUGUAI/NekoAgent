"""Route privileged read commands through the fixed Ops Broker contract."""

from __future__ import annotations

import subprocess
from collections.abc import Callable

from bridge_ops_broker_client import OpsBrokerClientError


def capture_command(
    args: list[str],
    timeout: int,
    *,
    env: dict[str, str] | None,
    broker_required: bool,
    broker_request: Callable,
    command_env: Callable[[], dict[str, str]],
) -> tuple[bool, str]:
    if broker_required and args:
        action = ""
        target = ""
        broker_args: dict[str, int | str] = {
            "timeout_seconds": max(1, min(int(timeout), 30)),
        }
        if args[:3] == ["journalctl", "-u", args[2]] and len(args) >= 6:
            action, target = "service_logs", args[2]
            try:
                broker_args["lines"] = int(args[4])
            except (TypeError, ValueError):
                return False, "broker_args_invalid"
        elif len(args) == 5 and args[:3] == ["docker", "logs", "--tail"]:
            action, target = "container_logs", args[4]
            try:
                broker_args["lines"] = int(args[3])
            except (TypeError, ValueError):
                return False, "broker_args_invalid"
        elif len(args) == 5 and args[:3] == ["docker", "exec", args[2]] and args[3] == "printenv":
            action, target = "container_env", args[2]
            broker_args["name"] = args[4]
        elif len(args) == 6 and args[:3] == ["docker", "exec", args[2]] and args[3:5] == ["test", "-s"]:
            action, target = "container_file_exists", args[2]
            broker_args["path"] = args[5]
        elif len(args) >= 6 and args[:3] == ["docker", "exec", args[2]] and args[3:5] == ["python3", "-c"]:
            if "CheckLoginStatus" in str(args[5]):
                action, target = "qq_login_probe", args[2]
        if action:
            try:
                result = broker_request(action, target, broker_args)
            except OpsBrokerClientError as exc:
                return False, str(exc)
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if action == "container_env":
                return bool(result.get("ok") and data.get("ok")), str(data.get("value") or "")
            if action == "container_file_exists":
                return bool(result.get("ok") and data.get("exists")), ""
            return bool(result.get("ok") and data.get("ok", result.get("ok"))), str(
                data.get("output") or result.get("error") or "",
            )
        if args[0] in {"docker", "systemctl", "journalctl"}:
            return False, "broker_operation_unimplemented"
    try:
        completed = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env or command_env(),
        )
        text = ((completed.stdout or "") + (completed.stderr or "")).strip()
        return completed.returncode == 0, text
    except Exception as exc:
        return False, str(exc)


__all__ = ["capture_command"]
