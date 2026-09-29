"""Three dependency-free checks for the current public backend boundary."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


ROOT = Path(os.environ.get("NEKO_PUBLIC_ROOT", Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT))


def test_meme_offer_requires_visual_review_and_reply_anchor() -> None:
    from bridge_meme_expression import _anchored_candidates

    reviewed = {
        "description_method": "manual_visual_review",
        "description": "画面写着赞同",
        "tags": "点赞,赞同",
        "emotion": "happy",
    }
    assert _anchored_candidates([reviewed], "我想点赞", "我先听你说完") == []
    assert _anchored_candidates([reviewed], "随便", "这事值得点赞") == [reviewed]
    assert _anchored_candidates([dict(reviewed, description_method="source_page")], "随便", "这事值得点赞") == []


def test_unknown_role_preserves_available_group_barrier() -> None:
    from bridge_group_state import _merge

    state = {
        "facts": {
            "membership": {"value": "joined"},
            "mute_self": {"value": "clear"},
            "mute_all": {"value": "clear"},
        },
        "barrier": 10,
    }
    assert _merge(state, "role", "unknown", 20, 20, "snapshot", "one")
    assert state["barrier"] == 10
    assert _merge(state, "membership", "absent", 30, 30, "notice", "two")
    assert state["barrier"] == 30


def test_group_policy_gate_uses_identity_scope_without_expanding_authority() -> None:
    import bridge_proactive_messaging_policy as policy

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE proactive_messaging_policies (id INTEGER)")
    conn.execute("CREATE TABLE assistant_feature_flags (name TEXT, enabled INTEGER)")
    conn.execute("INSERT INTO assistant_feature_flags VALUES ('relationship_proactive_v2', 1)")
    calls = []
    original_assistant = policy.current_assistant
    original_gate = policy.proactive_message_gate
    try:
        policy.current_assistant = lambda _conn, *, integrity_scope: (
            calls.append(("assistant", integrity_scope)) or {"id": "public-assistant"}
        )
        policy.proactive_message_gate = lambda _conn, **kwargs: (
            calls.append(("gate", kwargs)) or {"allowed": True}
        )
        result = policy.policy_gate_if_present(conn, "group:example")
    finally:
        policy.current_assistant = original_assistant
        policy.proactive_message_gate = original_gate
        conn.close()
    assert result == {"allowed": True}
    assert calls[0] == ("assistant", "identity")
    assert calls[1][1] == {
        "target_type": "group",
        "target_id": "example",
        "assistant": {"id": "public-assistant"},
    }
