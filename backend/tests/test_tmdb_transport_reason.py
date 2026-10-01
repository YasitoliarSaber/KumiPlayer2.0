from unittest.mock import MagicMock

import httpx
import pytest

from app.media_v4.revisions.service import metadata_recovery_policy
from app.media_v4.revisions.transport_reason import transport_failure_reason
from app.scrape.tmdb_client import TMDBClient, TMDBClientError


@pytest.mark.parametrize("error,code", [(httpx.ConnectError("unreachable"), "provider_network_error"), (httpx.ReadTimeout("timeout"), "provider_timeout"), (httpx.ProxyError("proxy"), "provider_proxy_error")])
def test_transport_failure_preserves_actionable_reason(error, code, monkeypatch):
    monkeypatch.setattr("app.scrape.tmdb_client.time.sleep", lambda _: None)
    http = MagicMock()
    http.request.side_effect = error
    client = TMDBClient(bearer_token="test", rate_limit=0, max_retries=1, _http_client=http)
    with pytest.raises(TMDBClientError) as caught:
        client.search_tv("Show")
    assert caught.value.reason_code == code
    view = metadata_recovery_policy({"metadata_state": "source_unavailable", "identity_status": "confirmed", "failure_stage": "work_detail", "reason_code": code})
    assert view["action"] == "retry_metadata"
    assert "网络" in view["reason"] or "代理" in view["reason"]
    assert "服务暂不可用" not in view["reason"]


def test_historical_transport_evidence_is_safe_and_server_failure_is_not_guessed():
    reason = transport_failure_reason("source_unavailable", "TMDB 网络连接失败: https://user:secret@proxy.example")
    assert "网络" in reason
    assert "secret" not in reason
    assert "proxy.example" not in reason
    assert transport_failure_reason("source_unavailable", "TMDB 服务端错误 (503)") == ""
    assert transport_failure_reason("source_unavailable", "") == ""


def test_confirmed_season_transport_failure_can_retry_without_reidentification():
    view = metadata_recovery_policy({
        "metadata_state": "source_unavailable", "identity_status": "confirmed",
        "reason_code": "season_metadata_incomplete",
        "season_results": [{"local_season_number": 2, "reason_code": "provider_timeout"}],
    })
    assert view["action"] == "retry_metadata"
    assert "超时" in view["reason"]
