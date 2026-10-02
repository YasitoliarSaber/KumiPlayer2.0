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


def test_preparation_requires_session_header_and_does_not_accept_image_query_token(monkeypatch):
    monkeypatch.setenv("KUMIPLAYER_API_TOKEN", "thumbnail-session-token")
    with TestClient(app) as client:
        body = {"items": [{"path": "missing/poster.jpg", "width": 384}]}
        assert client.post("/api/assets/thumbnails/prepare", json=body).status_code == 401
        assert client.post("/api/assets/thumbnails/prepare?api_token=thumbnail-session-token", json=body).status_code == 401
        response = client.post("/api/assets/thumbnails/prepare", json=body, headers={"X-KumiPlayer-Token": "thumbnail-session-token"})
        assert response.status_code == 200
        assert response.json() == {"states": ["unavailable"]}
