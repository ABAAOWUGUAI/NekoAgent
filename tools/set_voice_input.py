#!/usr/bin/env python3
"""Explicit Owner-private voice input cutover with fail-closed preflight."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge_voice_input import set_voice_input_enabled
from bridge_voice_input_runtime import transcription_policy, validate_runtime
from bridge_voice_message_schema import (
    VOICE_INPUT_FETCH_FEATURE_FLAG,
    require_voice_input_schema,
)
from bridge_voice_transport_probe_schema import VOICE_TRANSPORT_PROBE_FEATURE_FLAG


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assistant-db", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enable", action="store_true")
    mode.add_argument("--disable", action="store_true")
    parser.add_argument(
        "--confirm-owner-private-text-reply-only",
        action="store_true",
    )
    args = parser.parse_args()
    if args.enable and not args.confirm_owner_private_text_reply_only:
        parser.error(
            "--enable requires --confirm-owner-private-text-reply-only"
        )
    database = Path(args.assistant_db).resolve()
    if not database.is_file():
        parser.error("assistant database does not exist")
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        require_voice_input_schema(conn)
        if args.enable:
            rows = dict(conn.execute(
                "SELECT name,enabled FROM assistant_feature_flags WHERE name IN (?,?)",
                (
                    VOICE_TRANSPORT_PROBE_FEATURE_FLAG,
                    VOICE_INPUT_FETCH_FEATURE_FLAG,
                ),
            ).fetchall())
            if any(bool(rows.get(name)) for name in (
                VOICE_TRANSPORT_PROBE_FEATURE_FLAG,
                VOICE_INPUT_FETCH_FEATURE_FLAG,
            )):
                parser.error("probe and controlled-fetch flags must be disabled")
            validate_runtime(transcription_policy(conn))
        result = set_voice_input_enabled(conn, bool(args.enable))
    print(json.dumps({"ok": True, **result}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
