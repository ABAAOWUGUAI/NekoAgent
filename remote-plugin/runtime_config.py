"""Validated in-process cache for Bridge-owned QQ runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import os
import re
import uuid


PREFIX_PATTERN = re.compile(r"^/[A-Za-z0-9_-]{1,31}$")


@dataclass
class ChannelRuntimeState:
    """Fail-closed until the first valid Bridge configuration is applied."""

    ready: bool = False
    version: int = 0
    etag: str = ""
    command_prefixes: tuple[str, ...] = field(default_factory=lambda: ("/codex",))
    auto_private_chat: bool = False
    reply_max_chars: int = 500
    delivery_poll_seconds: int = 12
    notification_interval_seconds: int = 30
    actual_bot_id: str = ""
    last_sync_error: str = "runtime_config_not_loaded"

    @staticmethod
    def _bounded(payload: dict, name: str, minimum: int, maximum: int) -> int:
        try:
            value = int(payload.get(name))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"runtime_{name}_invalid") from exc
        if value < minimum or value > maximum:
            raise ValueError(f"runtime_{name}_invalid")
        return value

    def apply(self, payload: dict) -> bool:
        if not isinstance(payload, dict):
            raise ValueError("runtime_config_invalid")
        try:
            version = int(payload.get("version") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime_config_version_invalid") from exc
        if version < 1:
            raise ValueError("runtime_config_version_invalid")
        values = payload.get("command_prefixes")
        if not isinstance(values, list):
            raise ValueError("runtime_command_prefixes_invalid")
        prefixes: list[str] = []
        for item in values:
            value = str(item or "").strip().lower()
            if not PREFIX_PATTERN.fullmatch(value):
                raise ValueError("runtime_command_prefix_invalid")
            if value not in prefixes:
                prefixes.append(value)
        if "/codex" not in prefixes or len(prefixes) > 8:
            raise ValueError("runtime_command_prefixes_invalid")
        etag = str(payload.get("etag") or "").strip()
        if len(etag) != 64 or any(char not in "0123456789abcdef" for char in etag.lower()):
            raise ValueError("runtime_config_etag_invalid")
        reply_max_chars = self._bounded(payload, "reply_max_chars", 500, 10000)
        delivery_poll_seconds = self._bounded(
            payload, "delivery_poll_seconds", 5, 300,
        )
        notification_interval_seconds = self._bounded(
            payload, "notification_interval_seconds", 10, 3600,
        )
        changed = version != self.version or etag != self.etag
        self.version = version
        self.etag = etag.lower()
        self.command_prefixes = tuple(prefixes)
        self.auto_private_chat = payload.get("auto_private_chat") is True
        self.reply_max_chars = reply_max_chars
        self.delivery_poll_seconds = delivery_poll_seconds
        self.notification_interval_seconds = notification_interval_seconds
        self.ready = True
        self.last_sync_error = ""
        return changed

    def note_error(self, error: object) -> None:
        self.last_sync_error = str(error or "runtime_config_sync_failed").strip()[:240]


class ChannelRuntimeClient:
    """Synchronize Bridge config and report the active AstrBot adapter identity."""

    def __init__(self, context, call_bridge, logger, state: ChannelRuntimeState) -> None:
        self.context = context
        self.call_bridge = call_bridge
        self.logger = logger
        self.state = state
        self.instance_id = f"astrbot-{os.getpid()}-{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _http_status(result: dict) -> int:
        try:
            return int(result.get("status") or 0)
        except (TypeError, ValueError):
            return 0

    async def discover_identity(self) -> tuple[str, str, str]:
        manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(manager, "platform_insts", ()) or ()
        adapter_id = "unavailable"
        for platform in platforms:
            try:
                meta = platform.meta()
                meta_name = str(getattr(meta, "name", "") or "").lower()
                candidate_id = str(getattr(meta, "id", "") or "").strip()
                if candidate_id:
                    adapter_id = candidate_id[:80]
                if meta_name != "aiocqhttp" and not hasattr(platform, "bot"):
                    continue
                bot = getattr(platform, "bot", None)
                if bot is None or not hasattr(bot, "get_login_info"):
                    continue
                info = await bot.get_login_info()
                if isinstance(info, dict):
                    bot_id = str(info.get("user_id") or info.get("self_id") or "").strip()
                    if bot_id.isdigit():
                        return bot_id, adapter_id, ""
            except Exception as exc:
                return "", adapter_id, type(exc).__name__
        return "", adapter_id, "bot_identity_unavailable"

    async def run(self) -> None:
        await asyncio.sleep(1)
        while True:
            sync_error = ""
            try:
                result = await self.call_bridge("GET", "/qq/channel/runtime-config")
                config = result.get("config") if isinstance(result, dict) else None
                if not result.get("ok") or not isinstance(config, dict):
                    status = self._http_status(result if isinstance(result, dict) else {})
                    raise RuntimeError(f"runtime_config_http_{status or 'failed'}")
                changed = self.state.apply(config)
                if changed:
                    self.logger.info("QQ runtime config applied version=%s", self.state.version)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                sync_error = type(exc).__name__
                self.state.note_error(sync_error)

            actual_bot_id, adapter_id, identity_error = await self.discover_identity()
            self.state.actual_bot_id = actual_bot_id
            try:
                heartbeat = await self.call_bridge(
                    "POST",
                    "/qq/channel/heartbeat",
                    {
                        "channel_instance_id": self.instance_id,
                        "actual_bot_id": actual_bot_id,
                        "adapter_id": adapter_id,
                        "applied_version": self.state.version if self.state.ready else 0,
                        "last_sync_error": sync_error or identity_error,
                        "capabilities": {
                            "runtime_config": True,
                            "heartbeat": True,
                            "delivery_claim": True,
                            "actual_bot_discovery": bool(actual_bot_id),
                        },
                    },
                )
                if not heartbeat.get("ok"):
                    self.logger.warning(
                        "QQ runtime heartbeat rejected status=%s",
                        self._http_status(heartbeat),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("QQ runtime heartbeat failed error=%s", type(exc).__name__)
            next_poll_seconds = self.state.notification_interval_seconds
            if sync_error or identity_error or not actual_bot_id:
                next_poll_seconds = min(next_poll_seconds, 10)
            await asyncio.sleep(next_poll_seconds)


__all__ = ["ChannelRuntimeClient", "ChannelRuntimeState"]
