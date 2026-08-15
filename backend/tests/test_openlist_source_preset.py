"""专项大模块 CP3：OpenList 来源媒体库卡片与可恢复处理入口回归。

覆盖：
1. OpenList import root → 创建 1 张来源卡（catalog_root_id 关联）
2. 同 root 再导入 → 仍然 1 张 → incremental scan（不创建重复来源卡）
3. needs_review → 有处理按钮（API：patch revision → confirm → durable mirror）
4. failed mirror → retry exact stage（幂等合并入队）
5. 刷新页面 → 来源卡、attention 状态、unit 状态仍可恢复
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import OpenListEntry
from app.main import app
from app.media_presets.store import list_presets

REMOTE_ROOT = "/夸克网盘"
TREE = {
    "/夸克网盘": [("动画", True, None, None)],
    "/夸克网盘/动画": [("冰菓", True, None, None), ("蜂蜜与四叶草", True, None, None)],
    "/夸克网盘/动画/冰菓": [
        ("冰菓 - 01.mkv", False, 100, 1700000000),
        ("冰菓 - 02.mkv", False, 200, 1700000001),
    ],
    "/夸克网盘/动画/蜂蜜与四叶草": [
        ("蜂蜜与四叶草 - 01.mkv", False, 100, 1700000002),
    ],
}


class FakeOpenListClient:
    """替换 app.api.openlist.OpenListClient 的假客户端（CP3：全程离线）。"""

    instances = []
    login_user = ""
    tree = TREE

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.calls = []
        FakeOpenListClient.instances.append(self)

    def login(self):
        if self.username == FakeOpenListClient.login_user:
            from app.integrations.openlist.models import OpenListAuthError
            raise OpenListAuthError()
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        normalized = normalize_remote_path(path)
        self.calls.append((normalized, bool(refresh), page))
        items = FakeOpenListClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        start = (page - 1) * per_page
        return type(
            "Page", (), {"entries": entries[start:start + per_page], "total": len(entries)}
        )()


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "cp3.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.login_user = ""
    FakeOpenListClient.tree = TREE
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _make_local_mount(tmp_path: Path) -> Path:
    root = tmp_path / "quark" / "动画"
    (root / "冰菓").mkdir(parents=True)
    (root / "冰菓" / "冰菓 - 01.mkv").write_bytes(b"1")
    (root / "冰菓" / "冰菓 - 02.mkv").write_bytes(b"2")
    (root / "蜂蜜与四叶草").mkdir(parents=True)
    (root / "蜂蜜与四叶草" / "蜂蜜与四叶草 - 01.mkv").write_bytes(b"3")
    return root


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


def _save_routes(client: TestClient, prefix: str = REMOTE_ROOT + "/动画", provider: str = "quark") -> None:
    resp = client.put(
        "/api/openlist/routes",
        json={
            "routes": [
                {
                    "route_id": "route-test",
                    "label": "夸克",
                    "remote_prefix": prefix,
                    "provider_id": provider,
                    "enabled": True,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text


class TestOpenlistSourcePreset:
    def test_import_batch_creates_one_source_preset(self, client, tmp_path):
        """OpenList 导入根 → 媒体管理中出现 1 张来源卡（catalog_root_id 关联）。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        resp = client.post(
            "/api/openlist/import-batch",
            json={
                "remote_paths": [REMOTE_ROOT + "/动画/冰菓"],
                "import_family": "anime",
            },
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload.get("presets"), "import-batch 应返回同步的来源卡"
        preset_info = payload["presets"][0]
        assert preset_info["catalog_root_id"]

        presets = list_presets()
        openlist_presets = [item for item in presets if item.source == "openlist"]
        assert len(openlist_presets) == 1
        preset = openlist_presets[0]
        assert preset.catalog_root_id == preset_info["catalog_root_id"]
        assert preset.remote_locator == REMOTE_ROOT + "/动画/冰菓"
        assert preset.ingest_method == "openlist_api"
        assert preset.update_mode == "openlist_scan"
        assert preset.provider_id == "quark"

    def test_same_root_reimport_reuses_single_preset(self, client, tmp_path):
        """同 canonical SourceRoot 再次导入 → 不生成第二张来源卡（复用 + 增量）。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        first = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert first.status_code == 200, first.text
        first_preset = first.json()["presets"][0]["preset_id"]

        second = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert second.status_code == 200, second.text
        second_preset = second.json()["presets"][0]["preset_id"]
        assert second_preset == first_preset

        openlist_presets = [item for item in list_presets() if item.source == "openlist"]
        assert len(openlist_presets) == 1

    def test_preset_rescan_returns_durable_job(self, client, tmp_path):
        """来源卡上的「增量扫描」调用已有 rescan 接口，返回 durable job。"""
        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        preset_id = resp.json()["presets"][0]["preset_id"]

        rescan = client.post(f"/api/openlist/presets/{preset_id}/rescan", json={})
        assert rescan.status_code == 200, rescan.text
        data = rescan.json()
        assert data["task_id"]
        assert data["execution_mode"] == "durable"
        assert data["resolution"] in {"created", "exact_reused"}


class TestRecoverableUnitActions:
    def _make_confirmed_unit(self, client, tmp_path, remote_locator):
        """创建批次并直接构造 confirmed revision 单元（跳过 discovery 不确定性）。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store

        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [remote_locator], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        batch_id = resp.json()["batch_id"]
        root_id = resp.json()["roots"][0]["root_id"]

        conn = get_connection()
        unit_id = "cp3-confirmed-unit"
        conn.execute(
            """
            INSERT OR IGNORE INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, ?, ?, '', '/动画/冰菓', 'w', 'confirmed', 0, '', ?, ?)
            """,
            (unit_id, batch_id, root_id, revision_store.now_iso(), revision_store.now_iso()),
        )
        conn.commit()
        items = [{
            "id": "i1", "source": "openlist", "provider_id": "quark",
            "relative_path": "动画/冰菓/冰菓 - 01.mkv",
            "real_path": str(tmp_path / "quark" / "动画" / "冰菓" / "冰菓 - 01.mkv"),
            "logical_locator": "",
            "resource_type": "video", "action": "generate_strm",
            "work_id": "w1", "canonical_work_id": "unit:cp3-confirmed-unit:main",
            "work_title": "冰菓", "original_title": "", "year": 2012,
            "media_type": "tv", "show_type": "anime_series",
            "series_group": "冰菓", "card_type": "main_series",
            "belongs_to_series": "", "relation_type": "", "group_type": "season",
            "season_number": 1, "episode_number": 1, "special_number": None,
            "title": "", "target_dir": "", "target_strm_path": "",
            "confidence": "high", "needs_review": False, "availability": "available",
            "warnings": [], "reasons": [], "user_override_id": "",
        }]
        rev = revision_store.create_revision(
            unit_id=unit_id, source_generation=1, items=items, status="confirmed",
        )
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (rev["revision_id"], unit_id),
        )
        conn.commit()
        return batch_id, {"unit_id": unit_id, "current_revision_id": rev["revision_id"]}

    def test_retry_unit_returns_batch_and_stages(self, client, tmp_path):
        """failed unit 的 retry：幂等返回同一批次，且不创建重复业务任务。"""
        from app.db.database import get_connection

        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        batch_id, unit = self._make_confirmed_unit(client, tmp_path, REMOTE_ROOT + "/动画/冰菓")
        unit_id = unit["unit_id"]
        revision_id = unit["current_revision_id"]

        # 制造 mirror 失败痕迹：先入队一个 mirror job 并标记终态 failed
        from app.pipeline import orchestrator

        first_job_id = orchestrator.enqueue_mirror(revision_id, unit_id)
        get_connection().execute(
            "UPDATE jobs SET status='failed', error=?, lease_owner='', lease_until='', "
            "cancel_requested=0, version=version+1 WHERE job_id=?",
            ("模拟镜像失败", first_job_id),
        )
        get_connection().commit()

        retry = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/{unit_id}/retry", json={}
        )
        assert retry.status_code == 200, retry.text
        payload = retry.json()
        assert payload["batch_id"] == batch_id
        assert "mirror" in payload["retried_stages"]
        second_job_id = payload["retried_stages"]["mirror"]
        # coalesced：不创建第二个业务任务（同 resource_key 合并）
        mirror_jobs = get_connection().execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE job_type='mirror_revision' AND resource_key=?",
            (f"mirror:{revision_id}",),
        ).fetchone()
        assert mirror_jobs["n"] == 1

        # 重复点击幂等：仍返回同一 job（不新增）
        retry2 = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/{unit_id}/retry", json={}
        )
        assert retry2.status_code == 200, retry2.text
        assert retry2.json()["retried_stages"]["mirror"] == second_job_id

    def test_retry_draft_revision_rejected_with_409(self, client, tmp_path):
        """needs_review（draft revision）不能走 retry —— 必须人工确认。"""
        from app.db.database import get_connection

        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        batch_id = resp.json()["batch_id"]
        root_id = resp.json()["roots"][0]["root_id"]

        # 直接创建 draft revision 单元（模拟 needs_review：不 auto-confirm）
        from app.import_plan import revision_store

        conn = get_connection()
        unit_id = "cp3-draft-unit"
        conn.execute(
            """
            INSERT OR IGNORE INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, ?, ?, '', '/动画/冰菓', 'w', 'needs_review', 0, '', ?, ?)
            """,
            (unit_id, batch_id, root_id, revision_store.now_iso(), revision_store.now_iso()),
        )
        conn.commit()
        items = [{
            "id": "i1", "source": "openlist", "provider_id": "quark",
            "relative_path": "动画/冰菓/冰菓 - 01.mkv",
            "real_path": str(tmp_path / "quark" / "动画" / "冰菓" / "冰菓 - 01.mkv"),
            "logical_locator": "",
            "resource_type": "video", "action": "generate_strm",
            "work_id": "w1", "canonical_work_id": "unit:cp3-draft-unit:main",
            "work_title": "冰菓", "original_title": "", "year": 2012,
            "media_type": "tv", "show_type": "anime_series",
            "series_group": "冰菓", "card_type": "main_series",
            "belongs_to_series": "", "relation_type": "", "group_type": "season",
            "season_number": 1, "episode_number": 1, "special_number": None,
            "title": "", "target_dir": "", "target_strm_path": "",
            "confidence": "high", "needs_review": True, "availability": "available",
            "warnings": [], "reasons": [], "user_override_id": "",
        }]
        rev = revision_store.create_revision(
            unit_id=unit_id, source_generation=1, items=items, status="draft",
        )
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (rev["revision_id"], unit_id),
        )
        conn.commit()

        retry = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/{unit_id}/retry", json={}
        )
        assert retry.status_code == 409
        assert "人工确认" in retry.json()["detail"]

    def test_review_flow_patch_confirm_enqueues_mirror(self, client, tmp_path):
        """needs_review → patch revision → confirm → durable mirror（V3 唯一路径）。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.jobs import store as job_store

        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        batch_id = resp.json()["batch_id"]
        root_id = resp.json()["roots"][0]["root_id"]

        # 直接构造 draft revision（needs_review 状态）
        conn = get_connection()
        unit_id = "cp3-review-unit"
        conn.execute(
            """
            INSERT OR IGNORE INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, ?, ?, '', '/动画/冰菓', 'w', 'needs_review', 0, '', ?, ?)
            """,
            (unit_id, batch_id, root_id, revision_store.now_iso(), revision_store.now_iso()),
        )
        conn.commit()
        items = [{
            "id": "i0", "source": "openlist", "provider_id": "quark",
            "relative_path": "动画/冰菓/冰菓 - 01.mkv",
            "real_path": str(tmp_path / "quark" / "动画" / "冰菓" / "冰菓 - 01.mkv"),
            "logical_locator": "",
            "resource_type": "video", "action": "generate_strm",
            "work_id": "w1", "canonical_work_id": "unit:cp3-review-unit:main",
            "work_title": "冰菓", "original_title": "", "year": 2012,
            "media_type": "tv", "show_type": "anime_series",
            "series_group": "冰菓", "card_type": "main_series",
            "belongs_to_series": "", "relation_type": "", "group_type": "season",
            "season_number": 1, "episode_number": 1, "special_number": None,
            "title": "", "target_dir": "", "target_strm_path": "",
            "confidence": "high", "needs_review": True, "availability": "available",
            "warnings": ["识别结果需要确认"], "reasons": [], "user_override_id": "",
        }]
        rev = revision_store.create_revision(
            unit_id=unit_id, source_generation=1, items=items, status="draft",
        )
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (rev["revision_id"], unit_id),
        )
        conn.commit()
        revision_id = rev["revision_id"]

        # 人工 patch（白名单字段）
        patch = client.patch(
            "/api/imports/openlist/items/i0",
            json={"plan_id": revision_id, "patch": {"needs_review": False, "warnings": []}},
        )
        assert patch.status_code == 200, patch.text

        # 人工确认 → durable mirror job
        confirmed = client.post(
            "/api/imports/openlist/confirm",
            json={"plan_id": revision_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        data = confirmed.json()
        assert data["execution_mode"] == "durable"
        assert data["job_id"]
        assert job_store.get_job(data["job_id"]) is not None
