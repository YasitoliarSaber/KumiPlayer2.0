"""桌面本地 API 的会话身份与浏览器边界。"""

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app


def test_privileged_api_requires_desktop_session_token(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "test-session-token")
    client = TestClient(app)

    unauthorized = client.get("/api/config")
    authorized = client.get(
        "/api/config",
        headers={"X-KumiPlayer-Token": "test-session-token"},
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json() == {"detail": "KumiPlayer 桌面会话验证失败"}
    assert authorized.status_code == 200


def test_health_remains_available_for_desktop_runtime_detection(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "test-session-token")

    response = TestClient(app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["app"] == "KumiPlayer"


def test_asset_get_accepts_token_only_through_restricted_query_parameter(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "test-session-token")
    client = TestClient(app)

    unauthorized = client.get("/api/assets", params={"path": "missing.jpg"})
    authorized = client.get(
        "/api/assets",
        params={"path": "missing.jpg", "api_token": "test-session-token"},
    )
    config_query = client.get(
        "/api/config",
        params={"api_token": "test-session-token"},
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 404
    assert config_query.status_code == 401


def test_websocket_rejects_missing_token_and_accepts_valid_token(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "test-session-token")
    client = TestClient(app)

    try:
        with client.websocket_connect("/ws/heartbeat"):
            raise AssertionError("缺少令牌的 WebSocket 不应连接成功")
    except WebSocketDisconnect as exc:
        assert exc.code == 1008

    with client.websocket_connect(
        "/ws/heartbeat?api_token=test-session-token",
    ) as websocket:
        websocket.send_text("heartbeat")
        assert websocket.receive_json()["type"] == "heartbeat_ack"


def test_cors_does_not_trust_arbitrary_web_pages():
    response = TestClient(app).options(
        "/api/config",
        headers={
            "Origin": "https://malicious.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-KumiPlayer-Token",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None


def test_cors_allows_tauri_preflight_for_privileged_api(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "test-session-token")

    response = TestClient(app).options(
        "/api/config",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-KumiPlayer-Token",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://tauri.localhost"
    assert "x-kumiplayer-token" in response.headers["access-control-allow-headers"].lower()


def test_unknown_host_is_rejected():
    response = TestClient(app).get(
        "/api/health",
        headers={"Host": "malicious.example"},
    )

    assert response.status_code == 400
