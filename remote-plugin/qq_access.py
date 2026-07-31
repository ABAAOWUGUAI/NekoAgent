"""QQ access-check client kept separate from the AstrBot command adapter."""

from contextvars import ContextVar


_ACTOR_ID = ContextVar("qq_actor_id", default="")
_GROUP_ID = ContextVar("qq_group_id", default="")
_MESSAGE_ID = ContextVar("qq_message_id", default="")
_ACCESS_DECISION = ContextVar("qq_access_decision", default=None)

ACTION_ALIASES = {
    "project_switch": "project",
    "project_create": "project",
    "projects": "projects",
    "memories": "memory",
    "remember": "memory",
    "forget": "memory",
    "persona_set": "persona",
    "relationship_set": "relationship",
    "task_stats": "task_stats",
}


async def event_access_allowed(
    event,
    action: str,
    *,
    call_bridge,
    group_id_of,
    sender_id_of,
    logger,
    message_id_of=lambda _: "",
) -> bool:
    group_id = group_id_of(event)
    sender_id = sender_id_of(event)
    _ACTOR_ID.set(sender_id)
    _GROUP_ID.set(group_id or "")
    _MESSAGE_ID.set(message_id_of(event) or "")
    payload = {
        "sender_id": sender_id,
        "event_type": "group" if group_id else "private",
        "requested_action": ACTION_ALIASES.get(action, action),
    }
    if group_id:
        payload["group_id"] = group_id
    try:
        result = await call_bridge("POST", "/qq/access/check", payload)
    except Exception as exc:
        _ACCESS_DECISION.set({"allowed": False, "reason": "access_check_failed", "role": ""})
        logger.warning("codex_agent access check failed error=%s", type(exc).__name__)
        return False
    _ACCESS_DECISION.set(dict(result))
    if result.get("ok") and result.get("allowed"):
        return True
    logger.info(
        "codex_agent access denied sender=%s group=%s event=%s action=%s reason=%s version=%s",
        sender_id_of(event),
        group_id or "",
        payload["event_type"],
        payload["requested_action"],
        result.get("reason") or result.get("error") or "access_denied",
        result.get("config_version") or "",
    )
    return False


def last_access_decision() -> dict:
    return dict(_ACCESS_DECISION.get() or {})


def actor_headers() -> dict[str, str]:
    actor = _ACTOR_ID.get()
    if not actor:
        return {}
    headers = {"X-QQ-Actor-ID": actor}
    group = _GROUP_ID.get()
    if group:
        headers["X-QQ-Group-ID"] = group
    message_id = _MESSAGE_ID.get()
    if message_id:
        headers["X-QQ-Message-ID"] = message_id
    return headers


__all__ = ["actor_headers", "event_access_allowed", "last_access_decision"]
