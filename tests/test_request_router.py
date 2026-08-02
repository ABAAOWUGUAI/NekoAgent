from bridge_request_router import normalize_request_text, resolve_request


def test_router_normalizes_bounded_access_variant():
    assert normalize_request_text("准人 列表") == "准入列表"


def test_router_marks_qq_list_as_evidence_required_read():
    result = resolve_request("现在有多少个 QQ 群已经准人列表了？")
    assert result["status"] == "resolved"
    assert result["operation"] == "read"
    assert result["evidence_required"] is True
