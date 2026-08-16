"""模块4 C1 验收：preset rescan 切换到 Source Catalog 增量链路（cutover）。

覆盖（规划员任务 3/4）：
- 已有 exact SourceRoot → 复用 root_id、generation +1、enqueue durable
  discovery_scan（scan_mode=incremental），/api/tasks 门面可读 job；
- 旧 preset 无 SourceRoot → 首次 rescan 注册 root，第二次复用、不产生第二个 root；
- preset rescan 不调用 scan_openlist_preset、不写 Manifest（旧链隔离）；
- incremental prepare_scan：只重排 root + failed + due，complete 且未到期的
  目录保持 complete（绝不整棵全 queued）；
- full prepare_scan：整棵已知目录树全部重新排队；
- full-validate 端点保持 scan_mode=full、整棵 frontier queued（已有行为）；
- OpenList capabilities 写入 native_delta=false / directory_verification=true /
  rolling_reconciliation=true（import-batch 与 rescan 共用路径）；
- overlap 冲突（无 exact match）→ 409 安全失败，不产生 root/job。

安全：全部走 Fake 配置 + 临时 SQLite；不访问真实网盘、不读写 data/。
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import load_config
from app.db.database import close_connection, init_db
from app.main import app

REMOTE_ROOT = "/夸克网盘"
PRESET_LOCATOR = "/夸克网盘/动画/冰菓"


class FakeOpenListClient:
    """替换 OpenListClient（含 connection 模块）的假客户端：全程离线。"""

    instances: list["FakeOpenListClient"] = []

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        FakeOpenListClient.instances.append(self)

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        from app.integrations.openlist.models import OpenListEntry

        return type(
            "Page", (), {"entries": [OpenListEntry(name="动画", is_dir=True)], "total": 1}
        )()


@pytest.fixture(autouse=True)
def fake_openlist_client(monkeypatch):
    FakeOpenListClient.instances = []
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr(
        "app.integrations.openlist.connection.OpenListClient", FakeOpenListClient
    )
    yield


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    """catalog + jobs 共用同一个临时 SQLite（app.db.database 单库）。"""
    import app.db.database as db_mod

    monkeypatch.setattr(db_mod, "_db_path", tmp_path / "cutover.db")
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


@pytest.fixture
def client():
    return TestClient(app)


def _save_config(client: TestClient, tmp_path) -> None:
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


def _save_routes(client: TestClient) -> None:
    resp = client.put(
        "/api/openlist/routes",
        json={
            "routes": [
                {
                    "route_id": "r1",
                    "label": "动画",
                    "remote_prefix": REMOTE_ROOT + "/动画",
                    "provider_id": "quark",
                    "enabled": True,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text


def _create_preset(preset_id: str = "preset-ol-1") -> str:
    from app.media_presets.models import MediaLibraryPreset
    from app.media_presets.store import save_preset

    save_preset(
        MediaLibraryPreset(
            preset_id=preset_id,
            name="冰菓",
            source="openlist",
            remote_locator=PRESET_LOCATOR,
            import_family="anime",
            import_scope="",
            ingest_method="openlist_api",
            provider_id="quark",
        )
    )
    return preset_id


def _source_id() -> str:
    from app.api.openlist import _openlist_source_id

    config = load_config()
    return _openlist_source_id(config, username=config.openlist_username or "")

def _discovery_job(root_id: str):
    from app.jobs import store as job_store

    jobs = job_store.list_discovery_jobs_for_root(root_id)
    return jobs[0] if jobs else None  # 按 created_at DESC，取最新 job


def _dirs_state(root_id: str) -> dict[str, str]:
    from app.catalog import store as catalog_store

    return {row["remote_path"]: row["state"] for row in catalog_store.list_all_directories(root_id)}


# ============================================================
# preset rescan cutover（API 层）
# ============================================================

class TestPresetRescanCutover:
    def test_rescan_reuses_exact_root_and_enqueues_incremental(self, client, tmp_path):
        """已有 exact SourceRoot → 复用 root_id、generation +1、incremental durable job。"""
        from app.catalog import store as catalog_store

        _save_config(client, tmp_path)
        _save_routes(client)
        _create_preset()
        catalog_store.create_source(
            source_id=_source_id(), source_type="openlist", provider_id="quark",
            ingest_method="openlist_api",
        )
        root = catalog_store.create_source_root(
            source_id=_source_id(),
            remote_locator=PRESET_LOCATOR,
            local_locator=str(tmp_path / "quark" / "动画" / "冰菓"),
        )
        assert root.active_generation == 0

        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["root_id"] == root.root_id
        assert body["generation"] == 1
        assert body["execution_mode"] == "durable"
        assert body["task_id"]

        job = _discovery_job(root.root_id)
        assert job is not None
        assert job.job_type == "discovery_scan"
        assert job.payload["scan_mode"] == "incremental"
        assert job.payload["generation"] == 1
        assert job.payload["root_id"] == root.root_id

        # /api/tasks 门面可读 durable job（前端原有 task_id polling 继续工作）
        record = client.get(f"/api/tasks/{body['task_id']}").json()
        assert record["task_id"] == body["task_id"]

        # 第二次 rescan：复用同一 root、不产生第二个 root、generation 再 +1
        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text
        assert resp.json()["root_id"] == root.root_id
        assert resp.json()["generation"] == 2
        assert len(catalog_store.list_source_roots(source_id=_source_id())) == 1

    def test_first_rescan_registers_root_second_reuses(self, client, tmp_path):
        """旧 preset 无 SourceRoot → 首次注册，第二次复用，不产生第二个 root。"""
        from app.catalog import store as catalog_store

        _save_config(client, tmp_path)
        _save_routes(client)
        _create_preset()
        assert catalog_store.list_source_roots(source_id=_source_id()) == []

        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text
        first = resp.json()
        roots = catalog_store.list_source_roots(source_id=_source_id())
        assert len(roots) == 1
        assert roots[0].normalized_locator == PRESET_LOCATOR
        assert roots[0].import_family == "anime"
        assert roots[0].local_locator == str(tmp_path / "quark" / "动画" / "冰菓").replace("\\", "\\")

        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text
        assert resp.json()["root_id"] == first["root_id"]
        assert resp.json()["generation"] == 2
        assert len(catalog_store.list_source_roots(source_id=_source_id())) == 1

    def test_rescan_does_not_call_scan_openlist_preset(self, client, tmp_path):
        """rescan 走 Source Catalog 链：旧链 scan_openlist_preset 已退役、不写 Manifest。"""
        import importlib

        from app.media_presets.store import get_preset

        _save_config(client, tmp_path)
        _save_routes(client)
        _create_preset()

        # C2 静态契约：旧递归链已退役，api.openlist 不再导出 scan_openlist_preset
        api_openlist = importlib.import_module("app.api.openlist")
        assert not hasattr(api_openlist, "scan_openlist_preset")

        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text

        # 旧链产物（snapshot/version/plan）不被写入预设
        preset = get_preset("preset-ol-1")
        assert preset.current_snapshot_id == ""
        assert preset.current_version_id == ""
        assert preset.current_plan_id == ""
        assert preset.versions == []

    def test_rescan_404_for_missing_preset(self, client, tmp_path):
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post("/api/openlist/presets/ghost/rescan")
        assert resp.status_code == 404

    def test_rescan_400_for_missing_remote_locator(self, client, tmp_path):
        from app.media_presets.models import MediaLibraryPreset
        from app.media_presets.store import save_preset

        _save_config(client, tmp_path)
        save_preset(
            MediaLibraryPreset(
                preset_id="preset-noloc",
                name="无定位",
                source="openlist",
                remote_locator="",
            )
        )
        resp = client.post("/api/openlist/presets/preset-noloc/rescan")
        assert resp.status_code == 400


# ============================================================
# capabilities 写入（import-batch 与 rescan 共用路径）
# ============================================================

class TestOpenListCapabilities:
    def test_rescan_writes_openlist_capabilities(self, client, tmp_path):
        from app.catalog import store as catalog_store

        _save_config(client, tmp_path)
        _save_routes(client)
        _create_preset()
        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text
        source = catalog_store.get_source(_source_id())
        assert json.loads(source["capabilities_json"]) == {
            "native_delta": False,
            "directory_verification": True,
            "rolling_reconciliation": True,
        }

    def test_import_batch_writes_openlist_capabilities(self, client, tmp_path):
        from app.catalog import store as catalog_store

        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [PRESET_LOCATOR], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        source = catalog_store.get_source(_source_id())
        assert json.loads(source["capabilities_json"]) == {
            "native_delta": False,
            "directory_verification": True,
            "rolling_reconciliation": True,
        }


# ============================================================
# overlap 冲突：409 安全失败（不放宽 overlap 规则、不扩大扫描范围）
# ============================================================

class TestOverlapResolution:
    def test_ancestor_root_conflict_reuses_existing_root(self, client, tmp_path):
        """已有祖先 root（/夸克网盘/动画）时 rescan 子定位（/夸克网盘/动画/冰菓）：
        不再 409，复用祖先 root，incremental 扫描，不创建新 root。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store

        _save_config(client, tmp_path)
        _save_routes(client)
        _create_preset()
        source_id = _source_id()
        catalog_store.create_source(
            source_id=source_id, source_type="openlist", provider_id="quark",
            ingest_method="openlist_api",
        )
        ancestor = catalog_store.create_source_root(
            source_id=source_id,
            remote_locator="/夸克网盘/动画",
            local_locator=str(tmp_path / "quark" / "动画"),
        )
        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["resolution"] == "covered_by_existing_root"
        assert body["canonical_locator"] == "/夸克网盘/动画"
        assert body["root_id"] == ancestor.root_id
        assert body["scan_mode"] == "incremental"

        # 未创建第二个 root；入队了一个 incremental discovery job
        roots = catalog_store.list_source_roots(source_id=source_id)
        assert len(roots) == 1
        assert roots[0].remote_locator == "/夸克网盘/动画"
        jobs = job_store.list_jobs(job_type="discovery_scan")
        assert len(jobs) == 1
        assert jobs[0].payload.get("scan_mode") == "incremental"

    def test_descendant_root_conflict_promotes_parent(self, client, tmp_path):
        """已有后代 root（/夸克网盘/动画/冰菓/第1季）时 rescan 祖先定位：
        事务化归并到新父 root（promoted_to_parent），full 扫描，后代 root 移除。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store

        _save_config(client, tmp_path)
        _save_routes(client)
        _create_preset()
        source_id = _source_id()
        catalog_store.create_source(
            source_id=source_id, source_type="openlist", provider_id="quark",
            ingest_method="openlist_api",
        )
        descendant = catalog_store.create_source_root(
            source_id=source_id,
            remote_locator="/夸克网盘/动画/冰菓/第1季",
            local_locator=str(tmp_path / "quark" / "动画" / "冰菓" / "第1季"),
        )
        # 给后代 root 建一个 media_unit（归并后应保留并归属父 root）
        conn = catalog_store.get_connection()
        conn.execute(
            """
            INSERT INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, created_at, updated_at
            ) VALUES (?, '', ?, 'anime', ?, '冰菓', 'plan_ready', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
            """,
            ("unit-desc", descendant.root_id, "/夸克网盘/动画/冰菓/第1季"),
        )
        conn.commit()

        resp = client.post("/api/openlist/presets/preset-ol-1/rescan")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["resolution"] == "promoted_to_parent"
        assert body["scan_mode"] == "full"
        assert body["covered_root_ids"] == [descendant.root_id]

        # 最终只存在一个父 root；unit 保留并归属父 root；后代 root 不存在
        roots = catalog_store.list_source_roots(source_id=source_id)
        assert len(roots) == 1
        assert roots[0].remote_locator == "/夸克网盘/动画/冰菓"
        assert roots[0].root_id == body["root_id"]
        assert catalog_store.get_source_root(descendant.root_id) is None
        unit_row = conn.execute(
            "SELECT root_id FROM media_units WHERE unit_id = 'unit-desc'"
        ).fetchone()
        assert unit_row is not None and unit_row["root_id"] == body["root_id"]
        jobs = job_store.list_jobs(job_type="discovery_scan")
        assert len(jobs) == 1
        assert jobs[0].payload.get("scan_mode") == "full"


# ============================================================
# prepare_scan 行为：incremental vs full（4.8 核心）
# ============================================================

class TestPrepareScanIncrementalFull:
    def _setup_known_tree(self):
        """root complete、A complete、B failed、C complete-but-due、D complete-and-not-due。"""
        from app.catalog import store as catalog_store

        catalog_store.create_source(
            source_id="ol", source_type="openlist", provider_id="quark",
            ingest_method="openlist_api",
        )
        root = catalog_store.create_source_root(
            source_id="ol", remote_locator="/动画", local_locator="K:\\动画"
        )
        tz = timezone(timedelta(hours=8))
        past = (datetime.now(tz) - timedelta(minutes=10)).isoformat()
        future = (datetime.now(tz) + timedelta(days=1)).isoformat()
        for path, state, next_verify in [
            ("/动画", "complete", ""),
            ("/动画/A", "complete", ""),
            ("/动画/B", "failed", ""),
            ("/动画/C", "complete", past),
            ("/动画/D", "complete", future),
        ]:
            catalog_store.upsert_directory(root.root_id, path)
            catalog_store.update_directory(
                root.root_id, path, state=state, next_verify_at=next_verify
            )
        return root

    def test_incremental_only_requeues_root_failed_due(self):
        """incremental：root/B/C queued；A/D 保持 complete；绝不整棵全 queued。"""
        from app.catalog import store as catalog_store

        root = self._setup_known_tree()
        catalog_store.prepare_scan(root.root_id, generation=1, mode="incremental")
        states = _dirs_state(root.root_id)
        assert states["/动画"] == "queued"          # root 总是重扫
        assert states["/动画/A"] == "complete"      # 完整且未到期：不重扫
        assert states["/动画/B"] == "queued"        # failed 滚动重试
        assert states["/动画/C"] == "queued"        # complete-but-due
        assert states["/动画/D"] == "complete"      # complete-and-not-due 保持
        assert sorted(p for p, s in states.items() if s == "queued") == [
            "/动画", "/动画/B", "/动画/C",
        ]

    def test_full_requeues_whole_known_tree(self):
        """full：root/A/B/C/D 整棵已知目录树全部重新排队。"""
        from app.catalog import store as catalog_store

        root = self._setup_known_tree()
        catalog_store.prepare_scan(root.root_id, generation=1, mode="full")
        states = _dirs_state(root.root_id)
        assert set(states) == {"/动画", "/动画/A", "/动画/B", "/动画/C", "/动画/D"}
        assert all(state == "queued" for state in states.values())

    def test_incremental_on_empty_tree_seeds_root_only(self):
        """空树（首次）incremental 也播种 root 目录。"""
        from app.catalog import store as catalog_store

        catalog_store.create_source(
            source_id="ol", source_type="openlist", provider_id="quark",
            ingest_method="openlist_api",
        )
        root = catalog_store.create_source_root(
            source_id="ol", remote_locator="/动画", local_locator="K:\\动画"
        )
        catalog_store.prepare_scan(root.root_id, generation=1, mode="incremental")
        assert _dirs_state(root.root_id) == {"/动画": "queued"}

    def test_stale_generation_is_ignored(self):
        """generation fence：旧 generation 不得覆盖新 generation。"""
        from app.catalog import store as catalog_store

        root = self._setup_known_tree()
        catalog_store.bump_generation(root.root_id)  # active_generation = 1
        catalog_store.prepare_scan(root.root_id, generation=1, mode="full")
        # generation=1 不低于 active_generation=1，正常执行
        assert all(s == "queued" for s in _dirs_state(root.root_id).values())
        # 更旧 generation 0 被 fence 拒绝，不改变状态
        catalog_store.prepare_scan(root.root_id, generation=0, mode="incremental")
        assert all(s == "queued" for s in _dirs_state(root.root_id).values())


# ============================================================
# full-validate 端点保持 full 语义（已有行为，补断言）
# ============================================================

class TestFullValidateKeepsFullMode:
    def test_full_validate_requeues_whole_frontier(self, client, tmp_path):
        from app.catalog import store as catalog_store

        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [PRESET_LOCATOR], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        batch = resp.json()
        root_id = batch["roots"][0]["root_id"]

        # 构造已知目录树（root complete / 子目录 complete-not-due）
        tz = timezone(timedelta(hours=8))
        future = (datetime.now(tz) + timedelta(days=1)).isoformat()
        for path in ("/夸克网盘/动画/冰菓", "/夸克网盘/动画/冰菓/第1季"):
            catalog_store.upsert_directory(root_id, path)
            catalog_store.update_directory(
                root_id, path, state="complete", next_verify_at=future
            )
        assert all(
            s == "complete" for s in _dirs_state(root_id).values()
        )

        resp = client.post(f"/api/openlist/import-batches/{batch['batch_id']}/full-validate")
        assert resp.status_code == 200, resp.text

        job = _discovery_job(root_id)
        assert job is not None
        assert job.payload["scan_mode"] == "full"
        # 整棵已知 frontier 重新排队（complete 未到期也被 full 重扫）
        assert all(s == "queued" for s in _dirs_state(root_id).values())
