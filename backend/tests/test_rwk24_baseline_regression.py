"""RWK-24 验收：TXT 真正成为 Source Catalog baseline 的决定性回归。

A. 原生路径 TXT 导入（/api/media-presets/import-local-tree）
   → preset.catalog_root_id 非空、Provider root 存在、
     source_nodes > 0、source_directories complete > 0、全过程 0 OpenList 请求；
B. multipart TXT 导入与 A 共用同一共享 bootstrap，同挂载根不产生第二套 root；
C. TXT baseline 完成后再 bind → 成功、root_id/source_id 不变、snapshot 已存在；
D. 大型 fake TXT（多层大量目录）baseline → bind → 第一次 bound incremental
   → 必须真正执行 handle_discovery_scan
   → Fake OpenList 请求量 ≪ 全树目录数（不重新枚举已知 subtree）
   → source_nodes canonical identity 不变、media_units unit_id 不重复。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.media_presets.store import list_presets

# 大型 fake TXT：1 根 × 40 作品 × 2 季 ≈ 122 目录节点（>100 且足够证明
# "首次增量请求量 ≪ 已知目录数"；避免超大树的扫描耗时拖慢回归）
def _big_tree() -> str:
    lines = ["|——根目录", "| |-动画1"]
    for work in range(40):
        work_name = f"作品1-{work:03d}"
        lines.append(f"| | |-{work_name}")
        for season in range(2):
            lines.append(f"| | | |-Season {season + 1}")
            for ep in range(3):
                lines.append(
                    f"| | | | |-{work_name}.S{season + 1:02d}E{ep + 1:02d}.mkv"
                )
    return "\n".join(lines) + "\n"


class FakeOpenListClient:
    instances = []
    list_calls = 0
    requested_paths: list[str] = []
    tree = {}

    def __init__(self, server_url, username, password, **kwargs):
        FakeOpenListClient.instances.append(self)
        self.server_url = server_url
        self.username = username
        self.password = password

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        from app.integrations.openlist.client import join_remote_path, normalize_remote_path
        from app.integrations.openlist.models import OpenListEntry

        normalized = normalize_remote_path(path)
        FakeOpenListClient.list_calls += 1
        FakeOpenListClient.requested_paths.append(normalized)
        items = FakeOpenListClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        return type("Page", (), {"entries": entries, "total": len(entries)})()


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "rwk24.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    import app.media_presets.store as mstore
    import app.media_presets.service as mservice
    import app.core.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mstore, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mservice, "get_data_dir", lambda: data_dir)
    # api 层模块级绑定的 get_data_dir 也要 patch（否则归档路径指向真实 data）
    import app.api.media_presets as api_mp

    monkeypatch.setattr(api_mp, "get_data_dir", lambda: data_dir)
    yield
    close_connection()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.list_calls = 0
    FakeOpenListClient.requested_paths = []
    FakeOpenListClient.tree = {}
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.client.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _run_snapshot_baseline(client, root_id):
    """执行该 root 的 snapshot baseline job（durable handler 同步跑）。"""
    from app.jobs import store as job_store
    from app.pipeline.discovery_handler import handle_discovery_scan

    jobs = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=200)
    snapshot_job = next(
        (j for j in jobs
         if j.payload.get("root_id") == root_id
         and j.payload.get("scan_channel", "").startswith("snapshot_")),
        None,
    )
    assert snapshot_job is not None, "必须产生 snapshot baseline job"
    return handle_discovery_scan(snapshot_job.payload)


def _write_tree(tmp_path, text: str, name: str = "115目录树.txt") -> Path:
    tree = tmp_path / name
    tree.write_text(text, encoding="utf-8")
    return tree


class TestNativePathBaseline:
    def test_import_local_tree_creates_full_baseline(self, client, tmp_path):
        """A：原生路径 TXT 导入 → 完整 Source Catalog baseline，0 OpenList 请求。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        tree = _write_tree(tmp_path, _big_tree())
        calls_before = FakeOpenListClient.list_calls
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={
                "tree_path": str(tree),
                "import_family": "anime",
                "import_scope": "",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("baseline", {}).get("status") == "baseline_queued", resp.text
        assert FakeOpenListClient.list_calls == calls_before, "TXT baseline 全程 0 OpenList 请求"

        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        assert root_id, "import-local-tree 后 catalog_root_id 必须非空"
        root = catalog_store.get_source_root(root_id)
        assert root is not None and root.source_id.startswith("pan115-")

        # 执行 baseline job → source_nodes + complete directories
        result = _run_snapshot_baseline(client, root_id)
        assert result["summary"].get("failed_count", 0) == 0

        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        dirs = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ? AND state = 'complete'",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0, "TXT baseline 必须写入 source_nodes"
        assert int(dirs) > 0, "TXT baseline 必须建立 complete source_directories frontier"

        # bindable-providers 显示 ready
        bp = client.get("/api/openlist/bindable-providers").json()
        entry = next(p for p in bp["providers"] if p["root_id"] == root_id)
        assert entry["baseline_ready"] is True
        assert entry["baseline_node_count"] == int(nodes)

    def test_multipart_and_native_share_same_root(self, client, tmp_path):
        """B：multipart 与原生路径同挂载根 → 同一 root（不产生第二套）。"""
        from app.catalog import store as catalog_store
        from app.media_presets.service import ensure_provider_source_root

        # 先用原生路径导入
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id

        # multipart 导入同一挂载根（source_root 指向同 mount 根）
        with open(tree, "rb") as fh:
            resp2 = client.post(
                "/api/media-presets",
                data={
                    "source": "pan115",
                    "source_root": str(tmp_path),
                    "import_family": "anime",
                    "import_scope": "",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp2.status_code == 200, resp2.text

        # 同挂载根的所有 pan115 root 应复用同一 root
        from app.db.database import get_connection

        rows = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE source_id LIKE 'pan115-%'",
        ).fetchone()
        assert int(rows["c"]) == 1, "同一挂载根不得产生第二套 Provider root"
        # preset 关联指向同一 root
        presets = [p for p in list_presets() if p.source == "pan115"]
        roots = {p.catalog_root_id for p in presets if p.catalog_root_id}
        assert len(roots) == 1

    def test_bind_after_baseline_keeps_identity(self, client, tmp_path):
        """C：TXT baseline 完成后 bind → 成功、root/source 不变、snapshot 已存在。"""
        from app.catalog import store as catalog_store

        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        source_id = catalog_store.get_source_root(root_id).source_id
        _run_snapshot_baseline(client, root_id)

        # 配置 OpenList + route → bind
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 200, bind.text
        root = catalog_store.get_source_root(root_id)
        assert root.source_id == source_id
        assert root.openlist_conn_hash != ""


class TestFirstIncrementalNotFullTraversal:
    def test_first_incremental_requests_far_less_than_tree(self, client, tmp_path):
        """D：大 TXT baseline → bind → 第一次 incremental 请求量 ≪ 全树目录数。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan
        from app.db.database import get_connection

        # 1. 大型 TXT baseline（1 根 × 40 作品 × 2 季 ≈ 122 目录节点）
        #    Fake 远端只暴露 root 直接成员（动画1 等），其余内容以 TXT baseline 为准
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        _run_snapshot_baseline(client, root_id)

        # 统计 TXT 已知目录数（source_directories 总数）
        total_dirs = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ?",
            (root_id,),
        ).fetchone()["c"])
        assert total_dirs > 100, f"测试前置：TXT 目录树应 >100 目录（实际 {total_dirs}）"

        # 2. 配置 OpenList + route + bind
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 200, bind.text

        # Fake 远端：root 直接成员与 TXT 一致（动画1），不提供深层内容——
        # incremental 必须依赖 TXT baseline（mtime UNKNOWN→baseline learning），
        # 不得重新枚举已知 subtree。
        FakeOpenListClient.tree = {
            "/115网盘/动画1": [("动画1", True, None, 100.0)],
        }

        # 3. 第一次 bound incremental rescan → 真实执行 handler
        FakeOpenListClient.list_calls = 0
        FakeOpenListClient.requested_paths = []
        rescan = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan.status_code == 200, rescan.text
        job = job_store.get_job(rescan.json()["task_id"])
        result = handle_discovery_scan(job.payload)
        assert result["summary"].get("failed_count", 0) == 0

        # 4. 关键断言：请求量 ≪ 全树目录数（不重新枚举已知 subtree）。
        # 精确上界：preflight(1) + root(1) + BASELINE_VERIFY_BUDGET(50) 未验证目录。
        # 若增量退化为全树遍历，请求会接近 total_dirs（122），此处必须 ≪。
        from app.catalog import store as catalog_store

        requested = FakeOpenListClient.list_calls
        budget = catalog_store.BASELINE_VERIFY_BUDGET
        assert requested > 0, "必须真实请求 OpenList"
        assert requested <= 2 + budget, (
            f"第一次 incremental 请求量 {requested} 必须 ≤ preflight+root+baseline 预算"
            f"（2+{budget}）；全树已知目录 {total_dirs} 不应被重扫"
        )
        assert requested < total_dirs // 2, (
            f"第一次 incremental 请求量 {requested} 应远小于 TXT 已知目录数 {total_dirs}"
        )

        # 5. canonical identity 不变、media unit 总量不因增量翻倍
        units_before = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM media_units WHERE root_id = ?", (root_id,),
        ).fetchone()["c"])
        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0
        assert units_before > 0, "TXT baseline 必须已产生 media_units"
        # 再跑一轮 incremental，unit 总量不得因 namespace 分裂翻倍
        FakeOpenListClient.list_calls = 0
        rescan2 = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan2.status_code == 200, rescan2.text
        job2 = job_store.get_job(rescan2.json()["task_id"])
        result2 = handle_discovery_scan(job2.payload)
        assert result2["summary"].get("failed_count", 0) == 0
        units_after = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM media_units WHERE root_id = ?", (root_id,),
        ).fetchone()["c"])
        assert units_after <= units_before, (
            f"增量后 media_units 不得翻倍（{units_before} → {units_after}）"
        )


class TestBaselineReuseSafety:
    def test_reimport_same_tree_does_not_requeue_stale_archive(self, client, tmp_path):
        """复用/同媒体路径：不重新入队悬空归档（审查 HIGH 修复）。"""
        from app.jobs import store as job_store

        tree = _write_tree(tmp_path, _big_tree())
        resp1 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp1.status_code == 200, resp1.text
        assert resp1.json().get("baseline", {}).get("status") == "baseline_queued"

        # 再次导入同一 TXT（同媒体 → unchanged/reused 路径）
        resp2 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp2.status_code == 200, resp2.text
        baseline2 = resp2.json().get("baseline", {})
        assert baseline2.get("status") == "baseline_reused", (
            f"同媒体重复导入必须走复用路径（不重新入队），实际 {baseline2}"
        )
        # 不得产生悬空 input_path 的 snapshot job
        jobs = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=100)
        snapshot_jobs = [
            j for j in jobs
            if j.payload.get("scan_channel", "").startswith("snapshot_")
        ]
        for j in snapshot_jobs:
            inp = j.payload.get("input_path") or ""
            assert inp and Path(inp).exists(), (
                f"snapshot job 的 input_path 必须指向现存归档: {inp}"
            )


class TestBindBaselineGuard:
    def test_bind_rejected_when_baseline_failed(self, client, tmp_path):
        """RWK-23/30：baseline 未就绪（同步执行失败）时 bind-root 必须拒绝（400）。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        # 构造会导致 baseline 同步失败的导入：TXT 正常，但随后手工清掉
        # baseline completed fact 并把 target 前移（模拟 v2 pending 且未完成）
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        # 同步执行已完成 baseline（ready）——模拟 v2 入队 pending：target 前移但未完成
        conn = get_connection()
        conn.execute(
            "UPDATE source_roots SET baseline_target_generation = baseline_target_generation + 10 WHERE root_id = ?",
            (root_id,),
        )
        conn.commit()
        stats = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats["baseline_ready"] is False, "target 前移但未完成 → 不 ready"

        # 配置 OpenList + route → bind 必须拒绝
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 400, bind.text
        assert "尚未建立 Source Catalog 本地基线" in bind.json()["detail"]
        root = catalog_store.get_source_root(root_id)
        assert root.openlist_conn_hash == ""


class TestPartialBaselineNotReady:
    def test_v2_pending_after_v1_ready_revokes_ready(self, client, tmp_path):
        """事故①（真实事故链）：v1 完成 ready → v2 入队（target 前进但未执行）
        → ready=false → bind 被阻止；v2 完成 → ready=true。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan

        # 1. v1 TXT baseline（导入即同步完成）→ ready
        tree_v1 = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_v1), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        root_v1 = catalog_store.get_source_root(root_id)
        stats1 = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats1["baseline_ready"] is True, "v1 完成后必须 ready"
        assert root_v1.baseline_completed_generation == root_v1.baseline_target_generation

        # 2. 模拟 v2 入队（target 前进但**不执行**）：直接调 bootstrap 入队
        from app.media_presets.service import bootstrap_provider_catalog_from_tree

        tree_v2 = _write_tree(
            tmp_path,
            _big_tree() + "| | | | |-作品1-001.S01E03.extra.mkv\n",
            name="v2目录树.txt",
        )
        info = bootstrap_provider_catalog_from_tree(
            provider="pan115",
            tree_archive=str(tree_v2),
            local_mount_root=str(tmp_path),
            import_family="anime",
            import_scope="",
        )
        assert info["root_id"] == root_id, "v2 必须复用同一 root"
        root_v2 = catalog_store.get_source_root(root_id)
        assert root_v2.baseline_target_generation > root_v1.baseline_completed_generation, (
            "v2 入队必须前进 target"
        )
        assert root_v2.baseline_completed_generation == root_v1.baseline_completed_generation

        # v2 pending（未执行）→ ready 必须为 false
        stats2 = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats2["baseline_ready"] is False, (
            "v2 pending（target 前进但未完成）不得视为 ready"
        )

        # 3. bind 被拒绝（v2 未 ready）
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 400, bind.text
        assert "尚未建立 Source Catalog 本地基线" in bind.json()["detail"]

        # 4. 执行 v2 snapshot job → ready 恢复 true
        jobs2 = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=100)
        v2_job = next(
            (j for j in jobs2
             if j.payload.get("root_id") == root_id
             and j.payload.get("generation") == root_v2.baseline_target_generation),
            None,
        )
        assert v2_job is not None, "v2 snapshot job 必须存在"
        result2 = handle_discovery_scan(v2_job.payload)
        assert result2["summary"].get("failed_count", 0) == 0
        root_after = catalog_store.get_source_root(root_id)
        assert root_after.baseline_completed_generation == root_after.baseline_target_generation
        stats3 = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats3["baseline_ready"] is True, "v2 完成后必须恢复 ready"

        # 5. 此时 bind 成功
        bind2 = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind2.status_code == 200, bind2.text


class TestSingleExecutionAuthority:
    def test_txt_confirm_uses_durable_revision_from_api(self, client, tmp_path):
        """事故②：真实 TXT API 响应 preview.plan_id 已桥接为 durable revision；
        用该 plan_id confirm → execution_mode=durable → 恰一 mirror job。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store
        from app.import_plan import revision_store

        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        preview = body["preview"]
        baseline = body.get("baseline", {})
        assert baseline.get("status") == "baseline_queued", body

        # 关键：preview.plan_id 必须是 SQLite revision（不是 legacy plan）
        plan_id = preview["plan_id"]
        conn = get_connection()
        rev = conn.execute(
            "SELECT revision_id FROM import_revisions WHERE revision_id = ?",
            (plan_id,),
        ).fetchone()
        assert rev is not None, (
            f"TXT API 返回的 preview.plan_id 必须是 SQLite revision（实际 {plan_id}）"
        )
        # load_import_plan 对 revision_id 走 V3 优先（返回 revision 的 plan，
        # 而非 legacy JSON）——确认链唯一指向 durable revision
        from app.import_plan.store import load_import_plan

        bridged_plan = load_import_plan(plan_id=plan_id)
        assert bridged_plan is not None
        assert bridged_plan.plan_id == plan_id

        # 用真实 UI 的 plan_id confirm → durable mirror
        resp_confirm = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": plan_id, "items": []},
        )
        assert resp_confirm.status_code == 200, resp_confirm.text
        cbody = resp_confirm.json()
        assert cbody.get("execution_mode") == "durable", cbody
        assert cbody.get("job_id"), cbody

        # 该 revision 恰好一个 mirror job（幂等 get-or-create）
        mirror_jobs = [
            j for j in job_store.list_jobs(job_type="mirror_revision", status="queued", limit=100)
            if j.payload.get("revision_id") == plan_id
        ]
        assert len(mirror_jobs) == 1, "同一 revision 只有一条 mirror chain"
        # revision 已 confirmed
        rev_after = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = ?",
            (plan_id,),
        ).fetchone()
        assert rev_after["status"] == "confirmed"


class TestTxtUpdateSyncsCatalog:
    def test_txt_v2_update_syncs_same_root(self, client, tmp_path):
        """事故③：TXT v1 baseline → 导入 v2 → 同 root、Catalog 更新为 v2。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan

        # v1 TXT（2 部作品）
        tree_v1 = _write_tree(tmp_path, "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n", name="v1目录树.txt")
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_v1), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        source_id = catalog_store.get_source_root(root_id).source_id
        _run_snapshot_baseline(client, root_id)
        gen_v1 = catalog_store.get_source_root(root_id).active_generation
        assert catalog_store.source_catalog_baseline_stats(root_id)["baseline_ready"] is True

        # v2 TXT（新增作品C）
        tree_v2 = _write_tree(tmp_path, "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n", name="v2目录树.txt")
        resp2 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_v2), "import_family": "anime", "import_scope": ""},
        )
        assert resp2.status_code == 200, resp2.text
        preset2 = next(p for p in list_presets() if p.source == "pan115")
        assert preset2.catalog_root_id == root_id, "TXT v2 必须复用同一 root"
        root2 = catalog_store.get_source_root(root_id)
        assert root2.source_id == source_id

        # 执行 v2 的 snapshot baseline 更新 → generation 前进、目录数增加
        gen_before_run = root2.active_generation
        _run_snapshot_baseline(client, root_id)
        gen_v2 = catalog_store.get_source_root(root_id).active_generation
        assert gen_v2 > gen_v1, "v2 baseline 必须 bump generation"
        dirs = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ? AND state = 'complete'",
            (root_id,),
        ).fetchone()["c"])
        assert dirs > 0
        # Catalog 已含作品C（source_nodes 有 C 的目录）
        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND remote_path LIKE '%作品C%' AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0, "v2 更新后 Catalog 必须包含新增作品C"
        # baseline 完成标记前进（不保留 v1 假 baseline）
        root_after = catalog_store.get_source_root(root_id)
        assert root_after.baseline_completed_generation >= gen_v1
