"""Bounded B2 read-surface composition outside the legacy Bridge handler."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bridge_conversation_memory_http import ConversationMemoryHttpApi
from bridge_owner_brief import OwnerBriefService, collect_owner_brief_sources
from bridge_owner_brief_http import OwnerBriefHttpApi
from bridge_qq_conversation_http import QqConversationHttpApi
from bridge_qq_conversation_read import QqConversationReadService
from bridge_qq_quality_receipt import list_recent_quality_receipts


class B2ProductHttpRuntime:
    """Owns only the B2 read projections and their bounded dependencies."""

    def __init__(
        self,
        assistant_connect: Callable[[], Any],
        task_connect: Callable[[], Any],
        delivery_reader: Callable[[int], list[dict]],
        json_response: Callable[..., None],
    ) -> None:
        self._conversation = ConversationMemoryHttpApi(assistant_connect, json_response)
        qq_reader = QqConversationReadService(assistant_connect, delivery_reader=delivery_reader)
        self._qq = QqConversationHttpApi(qq_reader, json_response)

        def recent_quality(limit: int) -> list[dict]:
            conn = assistant_connect()
            try:
                return list_recent_quality_receipts(conn, limit=limit)
            finally:
                conn.close()

        sources = lambda limit: collect_owner_brief_sources(
            assistant_connect=assistant_connect,
            task_connect=task_connect,
            delivery_reader=delivery_reader,
            quality_reader=recent_quality,
            limit=limit,
        )
        self._owner = OwnerBriefHttpApi(OwnerBriefService(sources), json_response)

    def handle_get(self, handler: Any, path: str, query: dict[str, list[str]]) -> bool:
        return bool(
            self._conversation.handle_get(handler, path, query)
            or self._qq.handle_get(handler, path, query)
            or self._owner.handle_get(handler, path, query)
        )


__all__ = ["B2ProductHttpRuntime"]
