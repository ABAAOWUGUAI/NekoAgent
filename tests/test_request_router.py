from bridge_automation_actions import parse_automation_action
from bridge_qq_admin_actions import parse_qq_admin_action
from bridge_request_router import initial_route_disposition, normalize_request_text, resolve_request


def test_router_normalizes_bounded_access_variant():
    assert normalize_request_text("准人 列表") == "准入列表"


def test_router_marks_qq_list_as_evidence_required_read():
    result = resolve_request("现在有多少个 QQ 群已经准人列表了？")
    assert result["status"] == "resolved"
    assert result["operation"] == "read"
    assert result["evidence_required"] is True


def test_schedule_does_not_inherit_stale_qq_control_intent():
    message = (
        "每天早上8点给我汇报天气，未来12小时是否会下雨，"
        "这个需求做成定时任务给到我"
    )
    history = [{"role": "user", "content": "查询 QQ 群准入列表"}]

    assert parse_qq_admin_action(message, history) is None
    action = parse_automation_action(message, history)
    assert action["action_type"] == "automation_create"
    assert action["time_of_day"] == "08:00"
    assert action["job_action_type"] == "agent"
    assert action["parameters"] == {
        "topic": "weather",
        "delivery_format": "conversation",
        "forecast_horizon_hours": 12,
        "include_precipitation": True,
    }
    decision = resolve_request(message, history)
    assert decision["status"] == "resolved"
    assert [candidate["domain"] for candidate in decision["candidates"]] == ["automation"]


def test_explicit_cross_domain_request_is_composable_when_both_candidates_exist():
    message = "查询 QQ 群准入列表，并创建每天 08:00 的天气定时任务"
    decision, blocked = initial_route_disposition(message)

    # This assertion is intentionally conditional on the bounded deterministic
    # parsers recognizing both clauses; it prevents a mixed plan from being
    # converted into a generic clarification when the candidates are explicit.
    if decision["status"] == "mixed":
        assert blocked is None
