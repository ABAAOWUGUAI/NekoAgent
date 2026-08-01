#!/usr/bin/env python3
"""Request-scoped trusted provenance shared by conversation persistence paths."""

from contextlib import contextmanager
from contextvars import ContextVar


_CURRENT = ContextVar("current_inbound_exchange", default=None)


@contextmanager
def inbound_exchange_context(context: dict | None):
    token = _CURRENT.set(dict(context or {}))
    try:
        yield
    finally:
        _CURRENT.reset(token)


def current_inbound_exchange_context() -> dict:
    return dict(_CURRENT.get() or {})


__all__ = ["current_inbound_exchange_context", "inbound_exchange_context"]
