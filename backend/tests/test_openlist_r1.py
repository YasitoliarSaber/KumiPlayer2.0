"""OLIST-02-R1 回归测试。

覆盖验收打回项：provider 贯通到快照/计划/识别、目录树显式持久元数据、
批量部分结果持久化与取消恢复（含 db 持久化）、SWR 有界并发、
预取 generation 取消、prefetch_limit=0、连接变化清空路由、
分页强刷仅第一页 refresh=true、1001+ 项严格裁剪、单目录与批量 provider 一致。
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import OpenListEntry
from app.main import app

REMOTE_ROOT = "/夸克网盘"
TREE = {
    "/夸克网盘": [("动画", True, None, None), ("说明.txt", False, 5, 1)],
    "/夸克网盘/动画": [("冰菓", True, None, None), ("真人", True, None, None)],
    "/夸克网盘/动画/冰菓": [
        ("冰菓 - 01.mkv", False, 100, 1700000000),
        ("冰菓 - 02.mkv", False, 200, 1700000001),
    ],
    "/夸克网盘/动画/真人": [("真人剧 - 01.mkv", False, 300, 1700000002)],
}


class FakeOpenListClient:
    instances = []
    login_user = ""
    permission_path = ""
    tree = TREE

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.calls: list[tuple[str, bool, int]] = []  # (path, refresh, page)
        FakeOpenListClient.instances.append(self)

    def login(self):
        if self.username == FakeOpenListClient.login_user:
            raise PermissionError("auth")
        return "fake-token"

    def _list_dir_default(self, path, page=1, per_page=100, refresh=False):
        normalized = normalize_remote_path(path)
        self.calls.append((normalized, bool(refresh), page))
        if normalized == FakeOpenListClient.permission_path:
            from app.integrations.openlist.models import OpenListPermissionError
            raise OpenListPermissionError()
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

    list_dir = _list_dir_default


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.login_user = ""
    FakeOpenListClient.permission_path = ""
    FakeOpenListClient.tree = TREE
    FakeOpenListClient.list_dir = FakeOpenListClient._list_dir_default
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _save_config(client: TestClient, tmp_path: Path, *, remote_root: str = REMOTE_ROOT, mount_root: str | None = None) -> None:
    resp = client.post(
        "/api/openlist/config",
        json={
            "server_url": "https://ol.example.com:5244",
            "remote_root": remote_root,
            "mount_root": mount_root or str(tmp_path / "quark"),
            "username": "quark-user",
            "password": "p@ssw0rd",
            "cache_ttl_minutes": 1440,
            "prefetch_limit": 12,
        },
    )
    assert resp.status_code == 200, resp.text


def _make_local_mount(tmp_path: Path) -> Path:
    root = tmp_path / "quark"
    (root / "动画" / "冰菓").mkdir(parents=True)
    (root / "动画" / "冰菓" / "冰菓 - 01.mkv").write_bytes(b"1")
    (root / "动画" / "冰菓" / "冰菓 - 02.mkv").write_bytes(b"2")
    (root / "动画" / "真人").mkdir(parents=True)
    (root / "动画" / "真人" / "真人剧 - 01.mkv").write_bytes(b"3")
    return root


def _save_routes(client: TestClient, routes: list[dict] | None = None) -> None:
    if routes is None:
        routes = [
            {"route_id": "r1", "label": "动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True}
        ]
    resp = client.put("/api/openlist/routes", json={"routes": routes})
    assert resp.status_code == 200, resp.text


def _wait_task(client: TestClient, task_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    record = {}
    while time.time() < deadline:
        record = client.get(f"/api/tasks/{task_id}").json()
        if record["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"任务 {task_id} 超时未完成")
    from app.tasks.registry import get_task_manager

    manager = get_task_manager()
    deadline = time.time() + 5
    while time.time() < deadline:
        with manager._lock:
            released = task_id not in manager._running.values()
        if released:
            break
        time.sleep(0.05)
    return record


# ============================================================
# R1-1：provider 贯通到快照 / 计划 / 识别
# ============================================================

class TestProviderPipeline:
    def test_openlist_baidu_provider_reaches_snapshot_plan_and_recognition(self, client, tmp_path):
        """OpenList 导入归类为百度时，provider 贯通到 snapshot/plan/item，识别走百度规则。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(
            client,
            [{"route_id": "r-baidu", "label": "百度动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "baidu", "enabled": True}],
        )
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画/冰菓", "import_family": "anime"},
        )
        assert resp.status_code == 200
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        result = record["result"]

        # 预设显式持久化
        from app.media_presets.store import list_presets
        preset = list_presets()[0]
        assert preset.provider_id == "baidu"
        assert preset.ingest_method == "openlist_api"
        assert preset.source_route_id == "r-baidu"

        # 快照贯通
        from app.raw.store import load_raw_snapshot
        snapshot = load_raw_snapshot(result["snapshot_id"])
        assert snapshot is not None
        assert snapshot.provider_id == "baidu"
        assert snapshot.ingest_method == "openlist_api"
        assert snapshot.source_route_id == "r-baidu"

        # 计划贯通（plan 级 + item 级）
        from app.import_plan.store import load_import_plan
        plan = load_import_plan(plan_id=result["plan_id"])
        assert plan is not None
        assert plan.provider_id == "baidu"
        assert plan.ingest_method == "openlist_api"
        for item in plan.items:
            assert item.provider_id == "baidu"

        # 识别层使用 provider（effective source = baidu）：剧集类目判定不退化
        assert all(item.provider_id == "baidu" for item in plan.items)
        video_items = [item for item in plan.items if item.resource_type == "video"]
        assert video_items
        for item in video_items:
            assert item.show_type == "anime_series"  # baidu 动画根兜底规则

    def test_manifest_carries_provider_fields(self, tmp_path):
        """OpenList 清单（媒体库真相基线）包含 provider 元数据。"""
        from app.integrations.openlist.manifest import read_manifest, write_manifest
        from app.integrations.openlist.models import OpenListEntry

        entries = [OpenListEntry(name="a.mkv", is_dir=False, size=1, modified=1, remote_path="/动画/a.mkv")]
        path, _ = write_manifest(
            "m1", entries, remote_locator="/动画", source_root="K:\\动画",
            provider_id="baidu", ingest_method="openlist_api", source_route_id="r-baidu",
        )
        data = read_manifest(path)
        assert data["provider_id"] == "baidu"
        assert data["ingest_method"] == "openlist_api"
        assert data["source_route_id"] == "r-baidu"


# ============================================================
# R1-8：目录树/本地新记录显式持久元数据
# ============================================================

class TestExplicitMetadataPersistence:
    def test_directory_tree_preset_persists_provider_ingest_on_disk(self, client, tmp_path, monkeypatch):
        """115/百度目录树新预设创建时显式写入 provider/ingest（不只读取回填）。"""
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        media_root = tmp_path / "媒体"
        (media_root / "示例动画").mkdir(parents=True)
        (media_root / "示例动画" / "示例动画 [01].mkv").write_bytes(b"v")
        tree_path = media_root / "动画_目录树.txt"
        tree_path.write_text("├── 示例动画\n│   └── 示例动画 [01].mkv\n", encoding="utf-8")

        with TestClient(app) as test_client:
            resp = test_client.post(
                "/api/media-presets/import-local-tree",
                json={"tree_path": str(tree_path), "expected_source": "baidu", "import_family": "anime", "import_scope": ""},
            )
            assert resp.status_code == 200, resp.text

        index_path = tmp_path / "data" / "media_presets" / "index.json"
        raw = json.loads(index_path.read_text(encoding="utf-8"))
        assert len(raw["presets"]) == 1
        preset = raw["presets"][0]
        assert preset["provider_id"] == "baidu"
        assert preset["ingest_method"] == "directory_tree"
        # 版本显式写入元数据
        assert len(preset["versions"]) == 1
        version = preset["versions"][0]
        assert version["provider_id"] == "baidu"
        assert version["ingest_method"] == "directory_tree"

    def test_media_tree_version_fields_have_write_points(self, client, tmp_path, monkeypatch):
        """MediaTreeVersion 的 provider 三字段在 openlist 版本创建时显式写入。"""
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画/冰菓", "import_family": "anime"},
        )
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record
        from app.media_presets.store import list_presets
        preset = list_presets()[0]
        assert preset.versions
        version = preset.versions[-1]
        assert version.provider_id == "quark"
        assert version.ingest_method == "openlist_api"


# ============================================================
# R1-2：批量部分结果持久化与取消恢复
# ============================================================

class TestBatchPersistence:
    def test_batch_cancel_keeps_completed_directory(self, client, tmp_path):
        """第一目录成功、第二目录扫描时取消：result 保留第一目录的确认计划。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        original_list = FakeOpenListClient._list_dir_default

        def slow_second_dir(self, path, page=1, per_page=100, refresh=False):
            if normalize_remote_path(path) == REMOTE_ROOT + "/动画/真人":
                time.sleep(0.5)
            return original_list(self, path, page=page, per_page=per_page, refresh=refresh)

        FakeOpenListClient.list_dir = slow_second_dir
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"], "import_family": "anime"},
        ).json()
        # 等第一个目录完成（冰菓 3 次 list_dir 无延迟），第二个目录扫描中时取消
        time.sleep(0.5)
        client.post(f"/api/tasks/{resp['task_id']}/cancel")
        record = _wait_task(client, resp["task_id"])
        assert record["status"] == "cancelled"
        result = record.get("result") or {}
        items = result.get("batch_items") or []
        assert len(items) == 2
        by_path = {item["remote_path"]: item for item in items}
        assert by_path[REMOTE_ROOT + "/动画/冰菓"]["status"] == "success"
        assert by_path[REMOTE_ROOT + "/动画/冰菓"]["plan_id"]
        assert by_path[REMOTE_ROOT + "/动画/冰菓"]["preset_id"]
        # 未开始目录保持未处理标记
        assert by_path[REMOTE_ROOT + "/动画/真人"]["status"] in ("pending", "unstarted")

    def test_batch_result_persisted_to_db_survives_manager_restart(self, client, tmp_path):
        """批量完成结果写入 SQLite，模拟后端重启后仍可读取已完成目录。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人"], "import_family": "anime"},
        )
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded", record

        # 直接读 SQLite（模拟重启后 list_tasks 从 db 合并）
        from app.db.database import init_db
        from app.db.tasks import get_task
        init_db()
        persisted = get_task(record["task_id"])
        assert persisted is not None
        result = persisted.get("result") or {}
        items = result.get("batch_items") or []
        assert len(items) == 2
        assert all(item["status"] == "success" for item in items)
        assert all(item["plan_id"] for item in items)

    def test_batch_error_messages_are_safe(self, client, tmp_path):
        """未知异常返回固定安全提示，不泄露本地路径/服务 URL。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        def exploding_list_dir(self, path, page=1, per_page=100, refresh=False):
            raise RuntimeError(f"内部错误 /secrets/{tmp_path} token=abc")

        FakeOpenListClient.list_dir = exploding_list_dir
        resp = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        record = _wait_task(client, resp.json()["task_id"])
        assert record["status"] == "succeeded"  # 单目录失败不使任务失败
        result = record["result"]
        items = result.get("batch_items") or []
        assert items[0]["status"] == "error"
        serialized = json.dumps(result, ensure_ascii=False)
        assert str(tmp_path) not in serialized
        assert "token=abc" not in serialized
        assert "/secrets/" not in serialized


# ============================================================
# R1-3：SWR 有界 / 预取 generation
# ============================================================

class TestBoundedBackgroundWork:
    def test_swr_refresh_bounded_concurrency(self, tmp_path, monkeypatch):
        """多个 stale 目录的后台刷新并发不超过上限（有界线程池 + 去重）。"""
        monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
        import app.api.openlist as api_module

        active = {"count": 0, "max": 0}
        original_fetch = api_module._fetch_dir_entries

        def slow_fetch(client, path, *, refresh=False):
            active["count"] += 1
            active["max"] = max(active["max"], active["count"])
            try:
                time.sleep(0.1)
                entries, truncated = original_fetch(client, path, refresh=refresh)
                return entries, truncated
            finally:
                active["count"] -= 1

        monkeypatch.setattr(api_module, "_fetch_dir_entries", slow_fetch)
        api_module._refresh_active = 0
        api_module._refresh_inflight.clear()

        for index in range(20):
            api_module._schedule_background_refresh(
                f"conn{index % 3}", f"/dir{index}",
                "https://x", "u", "p", 1440,
            )
        # 等刷新完成
        deadline = time.time() + 10
        while time.time() < deadline:
            with api_module._refresh_guard:
                if api_module._refresh_active == 0 and not api_module._refresh_inflight:
                    break
            time.sleep(0.05)
        assert active["max"] <= api_module._REFRESH_MAX_ACTIVE

    def test_prefetch_generation_cancels_unstarted_paths(self, client, tmp_path):
        """新一代预取（空请求取消）使旧 generation 停止启动未处理路径。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        original_list = FakeOpenListClient._list_dir_default

        def slow_list_dir(self, path, page=1, per_page=100, refresh=False):
            time.sleep(0.3)
            return original_list(self, path, page=page, per_page=per_page, refresh=refresh)

        FakeOpenListClient.list_dir = slow_list_dir
        # 函数级并发验证（TestClient 请求是同步串行的，无法模拟并发）
        import threading
        from app.api.openlist import PrefetchRequest, prefetch as prefetch_endpoint

        result_holder: dict = {}
        paths = [REMOTE_ROOT + "/动画/冰菓", REMOTE_ROOT + "/动画/真人", REMOTE_ROOT + "/动画"]

        def run_first_prefetch():
            result_holder["result"] = prefetch_endpoint(PrefetchRequest(paths=paths))

        thread = threading.Thread(target=run_first_prefetch)
        thread.start()
        time.sleep(0.2)  # 第一个路径处理中（0.3s/调用），此时递增 generation
        # 空请求 = 新一代 generation（取消语义，不占用单并发锁）
        prefetch_endpoint(PrefetchRequest(paths=[]))
        thread.join(timeout=8)
        assert not thread.is_alive()
        assert result_holder["result"]["cancelled"] is False
        calls = FakeOpenListClient.instances[-1].calls
        paths_seen = [call[0] for call in calls]
        # 旧 generation 被取消：不应出现后续未开始路径
        assert REMOTE_ROOT + "/动画/冰菓" in paths_seen
        assert REMOTE_ROOT + "/动画/真人" not in paths_seen
        assert REMOTE_ROOT + "/动画" not in paths_seen

    def test_prefetch_limit_zero_disables_requests(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "",
                "prefetch_limit": 0,
            },
        )
        assert resp.status_code == 200
        resp = client.post(
            "/api/openlist/prefetch",
            json={"paths": [REMOTE_ROOT + "/动画/冰菓"]},
        )
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        # 未创建任何客户端实例 = 未发起任何请求
        assert FakeOpenListClient.instances == []


# ============================================================
# R1-5：连接身份变化清空路由
# ============================================================

class TestConnectionRouteIsolation:
    def test_connection_change_clears_routes(self, client, tmp_path):
        _save_config(client, tmp_path)
        _save_routes(client)
        assert client.get("/api/openlist/routes").json()["routes"]
        # 修改连接身份（不同 server_url）
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://another.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "",
            },
        )
        assert resp.status_code == 200
        assert "旧来源目录路由已清空" in resp.json()["message"]
        assert client.get("/api/openlist/routes").json()["routes"] == []

    def test_same_connection_keeps_routes(self, client, tmp_path):
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "https://ol.example.com:5244",
                "remote_root": REMOTE_ROOT,
                "mount_root": str(tmp_path / "quark"),
                "username": "quark-user",
                "password": "",
            },
        )
        assert resp.status_code == 200
        assert "旧来源目录路由已清空" not in resp.json()["message"]
        assert client.get("/api/openlist/routes").json()["routes"]


# ============================================================
# R1-6：分页刷新与截断
# ============================================================

class TestPaginationRefreshTruncation:
    def test_forced_refresh_only_first_page_uses_refresh_true(self, client, tmp_path):
        """200+ 项目录强刷：只有第一页 refresh=true，后续分页 false。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        FakeOpenListClient.tree = {
            REMOTE_ROOT: [
                (f"目录{i:03d}", True, None, None) if i % 2 == 0 else (f"文件{i:03d}.mkv", False, 10, 1)
                for i in range(250)
            ]
        }
        body = client.get(
            "/api/openlist/browse",
            params={"path": REMOTE_ROOT, "refresh": "true"},
        ).json()
        assert body["refresh_requested"] is True
        assert body["total"] == 250
        assert body["truncated"] is False
        refresh_flags = [call[1] for call in FakeOpenListClient.instances[-1].calls if call[0] == REMOTE_ROOT]
        assert refresh_flags == [True, False, False]

    def test_over_1000_entries_strictly_truncated(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        FakeOpenListClient.tree = {
            REMOTE_ROOT: [
                (f"文件{i:04d}.mkv", False, 10, 1)
                for i in range(1050)
            ]
        }
        body = client.get(
            "/api/openlist/browse",
            params={"path": REMOTE_ROOT},
        ).json()
        assert len(body["entries"]) == 1000
        assert body["truncated"] is True
        assert body["total"] == 1000


# ============================================================
# R1-12：单目录与批量目录 provider 标签一致
# ============================================================

class TestProviderLabelConsistency:
    def test_single_and_batch_provider_match(self, client, tmp_path):
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(
            client,
            [{"route_id": "rq", "label": "夸克动画", "remote_prefix": REMOTE_ROOT + "/动画", "provider_id": "quark", "enabled": True}],
        )
        # 单目录导入
        single = client.post(
            "/api/openlist/import",
            json={"remote_path": REMOTE_ROOT + "/动画/冰菓", "import_family": "anime"},
        )
        single_record = _wait_task(client, single.json()["task_id"])
        assert single_record["status"] == "succeeded"
        # 批量导入另一目录
        batch = client.post(
            "/api/openlist/batch-import",
            json={"remote_paths": [REMOTE_ROOT + "/动画/真人"], "import_family": "anime"},
        )
        batch_record = _wait_task(client, batch.json()["task_id"])
        assert batch_record["status"] == "succeeded"
        items = batch_record["result"]["batch_items"]
        assert items[0]["provider_id"] == "quark"

        from app.media_presets.store import list_presets
        presets = {preset.remote_locator: preset for preset in list_presets()}
        assert presets[REMOTE_ROOT + "/动画/冰菓"].provider_id == "quark"
        assert presets[REMOTE_ROOT + "/动画/真人"].provider_id == "quark"
        assert presets[REMOTE_ROOT + "/动画/冰菓"].ingest_method == "openlist_api"
        assert presets[REMOTE_ROOT + "/动画/真人"].ingest_method == "openlist_api"
