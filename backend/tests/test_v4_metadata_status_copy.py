from app.media_v4.revisions.service import metadata_recovery_policy


def test_empty_candidates_are_not_described_as_multiple_matches_or_a_blocker():
    result = metadata_recovery_policy({"metadata_state": "waiting_review", "reason_code": "no_candidates"})
    assert result["reason"] == "暂未找到对应的在线作品，已保留本地信息。可调整名称重新搜索，也可以稍后处理。"
    assert result["action"] == "choose_candidate"


def test_ambiguous_candidates_keep_optional_recovery():
    result = metadata_recovery_policy({"metadata_state": "waiting_review", "reason_code": "ambiguous_candidates"})
    assert "已保留本地信息" in result["reason"]
    assert "后继续" not in result["reason"]
    assert result["action"] == "choose_candidate"


def test_confirmed_identity_is_retained_when_detail_request_fails():
    result = metadata_recovery_policy({
        "metadata_state": "source_unavailable", "identity_status": "confirmed",
        "failure_stage": "work_detail", "reason_code": "provider_unavailable",
        "reason": "SECRET: low-level exception",
    })
    assert result["reason"] == "已识别作品，但在线资料暂时获取失败；可以稍后重试。"
    assert result["action"] == "retry_metadata"


def test_authorization_error_is_not_hidden_by_confirmed_identity():
    result = metadata_recovery_policy({
        "metadata_state": "source_unavailable", "identity_status": "confirmed",
        "failure_stage": "work_detail", "reason_code": "provider_auth_required",
    })
    assert result["action"] == "check_settings"
    assert "授权" in result["reason"]
