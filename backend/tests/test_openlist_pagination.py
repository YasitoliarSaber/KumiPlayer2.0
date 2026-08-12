"""OLIST-03 阶段一验收：真正分页浏览。

验收点：
- 打开 250 条目录只产生一次 page=1 请求，不替前端收齐整层；
- 用户加载下一页后才请求 page 2；
- 响应含 page/per_page/total/has_more；
- 缓存按「连接 + 路径 + 页码 + 每页数量」独立缓存（page1 命中不影响 page2）；
- 不再一次收齐到 1000 条后截断；大目录按页可完整读取。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import OpenListEntry
from app.main import app

REMOTE_ROOT = "/大媒体库"
PER_PAGE = 100


def _make_big_tree(count: int) -> dict:
    return {
        REMOTE_ROOT: [
            (f"作品{i:04d}", True, None, None) if i % 3 == 0 else (f"文件{i:04d}.mkv", False, 10 + i, 1)
            for i in range(count)
        ]
    }


class FakeOpenListClient:
    instances = []
    tree = {}

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.calls: list[tuple[str, bool, int]] = []
        FakeOpenListClient.instances.append(self)

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        normalized = normalize_remote_path(path)
        self.calls.append((normalized, bool(refresh), page))
        items = FakeOpenListClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name,
                is_dir=is_dir,
                size=size,
                modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        start = (page - 1) * per_page
        return type("Page", (), {"entries": entries[start:start + per_page], "total": len(entries)})()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.tree = {}
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _save_config(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/openlist/config",
        json={
            "server_url": "https://ol.example.com:5244",
            "remote_root": REMOTE_ROOT,
            "mount_root": str(tmp_path / "quark"),
            "username": "quark-user",
            "password": "p@ssw0rd",
        },
    )
    assert resp.status_code == 200, resp.text


def _all_calls():
    return [call for inst in FakeOpenListClient.instances for call in inst.calls if call[0] == REMOTE_ROOT]


class TestRealPagination:
    def test_open_directory_only_requests_page_one(self, client, tmp_path):
        """打开 250 条目录只产生一次 page=1 请求。"""
        _save_config(client, tmp_path)
        FakeOpenListClient.tree = _make_big_tree(250)
        body = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT}).json()
        assert len(body["entries"]) == PER_PAGE
        assert body["page"] == 1
        assert body["per_page"] == PER_PAGE
        assert body["total"] == 250
        assert body["has_more"] is True
        assert _all_calls() == [(REMOTE_ROOT, False, 1)]

    def test_next_page_requested_only_on_load_more(self, client, tmp_path):
        """用户加载下一页后才请求 page=2。"""
        _save_config(client, tmp_path)
        FakeOpenListClient.tree = _make_big_tree(250)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        assert _all_calls() == [(REMOTE_ROOT, False, 1)]

        page2 = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "page": "2"}).json()
        assert page2["page"] == 2
        assert len(page2["entries"]) == PER_PAGE
        assert page2["has_more"] is True
        assert _all_calls() == [(REMOTE_ROOT, False, 1), (REMOTE_ROOT, False, 2)]

        page3 = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "page": "3"}).json()
        assert page3["page"] == 3
        assert len(page3["entries"]) == 50
        assert page3["has_more"] is False
        assert _all_calls() == [(REMOTE_ROOT, False, 1), (REMOTE_ROOT, False, 2), (REMOTE_ROOT, False, 3)]

    def test_cache_isolated_per_page(self, client, tmp_path):
        """缓存按页隔离：page1 缓存命中不触碰上游，也不影响 page2。"""
        _save_config(client, tmp_path)
        FakeOpenListClient.tree = _make_big_tree(250)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "page": "2"})
        calls_after_fetch = _all_calls()

        # page1 命中缓存：无新请求
        hit = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT}).json()
        assert hit["cache"]["cached"] is True
        assert hit["cache"]["status"] == "fresh"
        # page2 命中缓存：无新请求
        hit2 = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "page": "2"}).json()
        assert hit2["cache"]["cached"] is True
        assert _all_calls() == calls_after_fetch

    def test_large_directory_fully_readable_without_truncation(self, client, tmp_path):
        """1050 项目录按页完整读取，不出现 1000 截断。"""
        _save_config(client, tmp_path)
        FakeOpenListClient.tree = _make_big_tree(1050)
        collected = 0
        page = 1
        while True:
            body = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "page": str(page)}).json()
            assert len(body["entries"]) <= PER_PAGE
            collected += len(body["entries"])
            if not body["has_more"]:
                break
            page += 1
        assert collected == 1050
        assert page == 11

    def test_forced_refresh_only_refreshes_requested_page(self, client, tmp_path):
        """强制刷新只刷新请求的当前页（page=1），不递归、不刷新其他页。"""
        _save_config(client, tmp_path)
        FakeOpenListClient.tree = _make_big_tree(250)
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT})
        client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "page": "2"})
        calls_before = _all_calls()

        refreshed = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "refresh": "true"}).json()
        assert refreshed["refresh_requested"] is True
        assert refreshed["page"] == 1
        new_calls = _all_calls()
        # 只新增一次 page=1 refresh=true 请求
        assert new_calls == calls_before + [(REMOTE_ROOT, True, 1)]

    def test_has_more_falls_back_to_full_page_when_total_unknown(self, client, tmp_path):
        """服务端未返回 total 时：total 保持 0（不得用本页数量冒充），has_more 按满页推断。"""
        _save_config(client, tmp_path)
        original_list = FakeOpenListClient.list_dir

        def no_total_list_dir(self, path, page=1, per_page=100, refresh=False):
            result = original_list(self, path, page=page, per_page=per_page, refresh=refresh)
            return type("Page", (), {"entries": result.entries, "total": 0})()

        FakeOpenListClient.list_dir = no_total_list_dir
        FakeOpenListClient.tree = _make_big_tree(250)
        body = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT}).json()
        assert body["total"] == 0  # 未知总数：不得用本页数量冒充
        assert body["has_more"] is True  # 满页 → 推断还有更多
        last = client.get("/api/openlist/browse", params={"path": REMOTE_ROOT, "page": "3"}).json()
        assert last["total"] == 0
        assert last["has_more"] is False  # 50 条不满页 → 没有更多
