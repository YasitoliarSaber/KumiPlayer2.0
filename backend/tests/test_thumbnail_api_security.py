"""缩略图端点必须支持桌面图片请求使用查询参数令牌。"""

from fastapi.testclient import TestClient

from app.main import app


def test_thumbnail_endpoint_accepts_desktop_session_query_token(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "thumbnail-session-token")

    with TestClient(app) as client:
        response = client.get(
            "/api/assets/thumbnail",
            params={
                "path": "missing/poster.jpg",
                "width": 384,
                "api_token": "thumbnail-session-token",
            },
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "文件不存在"


def test_thumbnail_endpoint_rejects_wrong_query_token(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "thumbnail-session-token")

    with TestClient(app) as client:
        response = client.get(
            "/api/assets/thumbnail",
            params={
                "path": "missing/poster.jpg",
                "width": 384,
                "api_token": "wrong-token",
            },
        )

    assert response.status_code == 401
