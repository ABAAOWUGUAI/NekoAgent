#!/usr/bin/env python3
"""Voice-message migrations and their optional drift checks."""

from bridge_migrations import Migration
from bridge_voice_message_schema import (
    VOICE_INPUT_MIGRATION_CHECKSUM,
    VOICE_MESSAGE_MIGRATION_CHECKSUM,
    apply_voice_input_v1,
    apply_voice_message_v1,
    require_voice_input_schema,
    require_voice_message_schema,
)


VOICE_MIGRATIONS = (
    Migration(
        32,
        "qq_voice_message_receipt_v1",
        apply=apply_voice_message_v1,
        checksum=VOICE_MESSAGE_MIGRATION_CHECKSUM,
    ),
    Migration(
        33,
        "qq_voice_input_v1",
        apply=apply_voice_input_v1,
        checksum=VOICE_INPUT_MIGRATION_CHECKSUM,
    ),
)


def require_voice_schemas(conn, versions):
    return (
        require_voice_message_schema(conn) if 32 in versions else None,
        require_voice_input_schema(conn) if 33 in versions else None,
    )


__all__ = ["VOICE_MIGRATIONS", "require_voice_schemas"]
