# -*- coding: utf-8 -*-
"""Source Catalog 生命周期测试：整库删除清理、重叠导入解析与来源根归并。

覆盖（施工方案第六节 A-I）：
- A 删除清理：完整关联链删除后无孤儿，sources 连接记录保留，真实媒体不触碰；
- B 镜像已空但来源根存在：删除仍清理 Source Catalog（用户问题回归）；
- C 镜像删除失败：保留 Source Catalog 事实供重试；
- D durable job 正在运行：协作式取消 + 中止删除（409 语义）；
- E 删除单部作品不删除来源根；
- F 完全相同路径复用 root 增量扫描；
- G 已有父目录覆盖新子目录：不 409、不新建 root、incremental；
- H 新父目录覆盖子目录：归并后单父 root、unit/revision 保留、full 扫描；
- I 归并事务失败：整体回滚，旧子根与关联数据完整；
- J TXT 目录树来源不受整库清理影响。

安全：全部走临时 SQLite 与临时目录，不访问真实网盘、不读写 data/。
"""

import uuid as uuid_mod
from pathlib import Path

import pytest

from app.catalog import lifecycle, store
from app.db.database import close_connection, get_connection, init_db
from app.jobs import store as job_store


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    import app.db.database as db_mod
    from app.catalog import maintenance_guard

    maintenance_guard._gate.reset_for_tests()
    monkeypatch.setattr(db_mod, "_db_path", tmp_path / "lifecycle.db")
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    maintenance_guard._gate.reset_for_tests()
    close_connection()


# ============================================================
# 关联链构造
# ============================================================

def _build_graph(
    *,
    source_id: str = "ol-conn",
    source_type: str = "openlist",
    remote: str = "/夸克网盘/动画/冰菓",
    local: str = "K:\\夸克\\动画\\冰菓",
    with_batch: bool = True,
    with_job: bool = True,
):
    """构造完整关联链：source → root → batch → directory/node → unit → revision
    → media_library → scrape binding → artifact → jobs。"""
    store.create_source(
        source_id=source_id, source_type=source_type, provider_id="quark",
        ingest_method="openlist_api", display_name="OpenList",
    )
    root = store.create_source_root(
        source_id=source_id, remote_locator=remote, local_locator=local,
        import_family="anime",
    )
    root_id = root.root_id

    store.upsert_directory(root_id, remote, parent_path="", depth=0)
    store.update_directory(root_id, remote, state="complete", entry_count=2)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO source_nodes (
            root_id, remote_path, parent_path, name, kind, size, mtime,
            first_seen_generation, last_seen_generation, tombstone
        ) VALUES (?, ?, '', ?, 'file', 100, 1.0, 1, 1, '')
        """,
        (root_id, f"{remote}/S01E01.mkv", "S01E01.mkv"),
    )
    conn.execute(
        """
        INSERT INTO source_nodes (
            root_id, remote_path, parent_path, name, kind, size, mtime,
            first_seen_generation, last_seen_generation, tombstone
        ) VALUES (?, ?, '', ?, 'dir', NULL, 1.0, 1, 1, '')
        """,
        (root_id, f"{remote}/Season 1", "Season 1"),
    )

    unit_id = uuid_mod.uuid4().hex
    revision_id = uuid_mod.uuid4().hex
    conn.execute(
        """
        INSERT INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, closure_generation, current_revision_id, created_at, updated_at
        ) VALUES (?, '', ?, 'seasonal', ?, ?, 'plan_ready', 1, ?, '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (unit_id, root_id, remote, "冰菓", revision_id),
    )
    conn.execute(
        """
        INSERT INTO import_revisions (
            revision_id, unit_id, parent_revision_id, source, provider_id,
            source_generation, status, hash, confirm_method, confirmed_at,
            created_at, updated_at
        ) VALUES (?, ?, '', 'openlist', 'quark', 1, 'executed', 'hash1', 'auto', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (revision_id, unit_id),
    )
    conn.execute(
        """
        INSERT INTO import_revision_items (
            revision_id, item_id, source, relative_path, real_path, logical_locator,
            resource_type, action, work_id, work_title, media_type, group_type,
            season_number, episode_number, target_strm_path, confidence, needs_review
        ) VALUES (?, ?, 'openlist', 'S01E01.mkv', ?, ?, 'video', 'generate_strm', ?, '冰菓', 'tv', 'season', 1, 1, ?, 'high', 0)
        """,
        (
            revision_id, "item-1",
            f"{local}/S01E01.mkv", f"{local}/S01E01.mkv",
            f"raw-{unit_id}", f"{local}/S01E01.strm",
        ),
    )
    conn.execute(
        """
        INSERT INTO media_libraries (
            library_id, name, root_id, remote_locator, import_family,
            current_revision_id, lifecycle_status, created_at, updated_at
        ) VALUES (?, '冰菓', ?, ?, 'anime', ?, 'active', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (f"lib-{unit_id}", root_id, remote, revision_id),
    )
    binding_id = f"binding-{unit_id}"
    conn.execute(
        """
        INSERT INTO scrape_bindings (
            binding_id, revision_id, work_id, source, provider_id, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'openlist', 'quark', 'ready', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (binding_id, revision_id, f"raw-{unit_id}"),
    )
    conn.execute(
        """
        INSERT INTO scrape_reviews (review_id, binding_id, local_title, added_at, updated_at)
        VALUES (?, ?, '冰菓', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (f"review-{unit_id}", binding_id),
    )
    conn.execute(
        """
        INSERT INTO artifact_records (artifact_id, kind, path, revision_id, work_id, created_at)
        VALUES (?, 'strm', ?, ?, ?, '2026-08-01T00:00:00+08:00')
        """,
        (f"artifact-{unit_id}", f"{local}/S01E01.strm", revision_id, f"raw-{unit_id}"),
    )
    conn.commit()

    batch_id = ""
    if with_batch:
        batch = store.create_import_batch(
            source_id=source_id,
            roots=[{"remote_locator": remote, "local_locator": local, "import_family": "anime"}],
        )
        batch_id = batch["batch_id"]

    if with_job:
        job_store.create_job(
            job_type="discovery_scan",
            resource_key=f"scan:conn:{source_id}",
            payload={"root_id": root_id, "generation": 1, "source_id": source_id},
        )
    return {
        "source_id": source_id,
        "root": root,
        "root_id": root_id,
        "unit_id": unit_id,
        "revision_id": revision_id,
        "binding_id": binding_id,
        "batch_id": batch_id,
    }


def _count(table: str, column: str, ids: list[str]) -> int:
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    return int(get_connection().execute(
        f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({marks})", ids
    ).fetchone()[0])


def _set_job_status(job_id: str, status: str) -> None:
    get_connection().execute(
        "UPDATE jobs SET status = ?, lease_owner = 'worker-1' WHERE job_id = ?",
        (status, job_id),
    )
    get_connection().commit()


# ============================================================
# A. 删除清理：完整关联链删除后无孤儿
# ============================================================

def test_openlist_library_clear_removes_catalog_graph(tmp_path, monkeypatch):
    """openlist 整库清理删除完整关联链；sources 连接记录保留；真实媒体不触碰。"""
    real_media = tmp_path / "quark" / "动画" / "冰菓" / "S01E01.mkv"
    real_media.parent.mkdir(parents=True)
    real_media.write_bytes(b"real-media-must-survive")

    graph = _build_graph()
    root_id = graph["root_id"]
    unit_id = graph["unit_id"]
    revision_id = graph["revision_id"]
    binding_id = graph["binding_id"]
    job_id = job_store.list_jobs(job_type="discovery_scan")[0].job_id

    preview = lifecycle.preview_catalog_cleanup("openlist")
    assert preview.root_count == 1
    assert preview.batch_count == 1
    assert preview.directory_count == 1
    assert preview.node_count == 2
    assert preview.unit_count == 1
    assert preview.revision_count == 1
    assert preview.revision_item_count == 1
    assert preview.library_count == 1
    assert preview.job_count == 1
    assert preview.active_job_count == 1

    gate = lifecycle.prepare_catalog_cleanup("openlist")
    assert gate["cancelled_job_count"] == 1
    assert gate["running_job_ids"] == []

    result = lifecycle.delete_catalog_for_clear("openlist")
    assert result.deleted_root_count == 1
    assert result.deleted_batch_count == 1
    assert result.deleted_directory_count == 1
    assert result.deleted_node_count == 2
    assert result.deleted_unit_count == 1
    assert result.deleted_revision_count == 1
    assert result.deleted_library_count == 1
    assert result.deleted_job_count == 1

    # sources 连接配置保留
    conn = get_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM sources WHERE source_id = ?", (graph["source_id"],)
    ).fetchone()[0] == 1
    # 全部关联表无孤儿
    assert _count("source_roots", "root_id", [root_id]) == 0
    assert _count("import_batches", "batch_id", [graph["batch_id"]]) == 0
    assert _count("source_directories", "root_id", [root_id]) == 0
    assert _count("source_nodes", "root_id", [root_id]) == 0
    assert _count("media_units", "root_id", [root_id]) == 0
    assert _count("import_revisions", "unit_id", [unit_id]) == 0
    assert _count("import_revision_items", "revision_id", [revision_id]) == 0
    assert _count("media_libraries", "root_id", [root_id]) == 0
    assert _count("scrape_bindings", "revision_id", [revision_id]) == 0
    assert _count("scrape_reviews", "binding_id", [binding_id]) == 0
    assert _count("artifact_records", "revision_id", [revision_id]) == 0
    assert _count("jobs", "job_id", [job_id]) == 0
    assert _count("job_attempts", "job_id", [job_id]) == 0
    # 真实媒体安然无恙
    assert real_media.read_bytes() == b"real-media-must-survive"


# ============================================================
# B. 镜像已空但来源根仍存在（用户问题回归）
# ============================================================

def test_library_clear_cleans_catalog_when_mirror_is_already_empty(tmp_path, monkeypatch):
    """镜像目录为空/不存在时，整库删除仍必须清理 Source Catalog 残留。"""
    mirror = tmp_path / "mirror"
    mirror.mkdir(parents=True)
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror))
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": "",
            "openlist_mount_root": str(tmp_path / "quark"),
            "mirror_dir": str(mirror),
            "heartbeat_enabled": False,
            "auto_shutdown_on_heartbeat_timeout": False,
        })(),
    )
    graph = _build_graph()

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    # 镜像目录为空也必须预告 Source Catalog 统计
    assert preview.catalog_root_count == 1
    assert preview.catalog_batch_count == 1
    assert preview.catalog_unit_count == 1
    assert preview.catalog_job_count == 1

    result = execute_delete(preview)
    assert result.status == "succeeded"
    assert result.deleted_catalog_root_count == 1
    assert result.deleted_catalog_unit_count == 1
    assert _count("source_roots", "root_id", [graph["root_id"]]) == 0
    # 连接记录仍在
    assert get_connection().execute(
        "SELECT COUNT(*) FROM sources WHERE source_id = ?", (graph["source_id"],)
    ).fetchone()[0] == 1


# ============================================================
# C. 镜像删除失败：保留 Source Catalog 事实
# ============================================================

def test_library_clear_keeps_catalog_when_mirror_delete_fails(tmp_path, monkeypatch):
    """镜像文件删除失败时不得清除 Source Catalog，保留数据库事实供重试。"""
    mirror = tmp_path / "mirror"
    openlist_dir = mirror / "openlist" / "冰菓" / "Season 1"
    openlist_dir.mkdir(parents=True)
    blocked = openlist_dir / "S01E01.strm"
    blocked.write_text("K:\\夸克\\动画\\冰菓\\S01E01.mkv", encoding="utf-8")
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror))
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": "",
            "openlist_mount_root": str(tmp_path / "quark"),
            "mirror_dir": str(mirror),
            "heartbeat_enabled": False,
            "auto_shutdown_on_heartbeat_timeout": False,
        })(),
    )
    graph = _build_graph()

    real_unlink = Path.unlink

    def fake_unlink(self, *args, **kwargs):
        if str(self).endswith("S01E01.strm"):
            raise PermissionError("simulated locked file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    result = execute_delete(preview)

    # 全部镜像删除失败 → failed；catalog 事实必须保留
    assert result.status in {"failed", "partial_failed"}
    assert any(f.path == "catalog_cleanup" for f in result.failed)
    # Source Catalog 事实保留
    assert _count("source_roots", "root_id", [graph["root_id"]]) == 1
    assert _count("media_units", "root_id", [graph["root_id"]]) == 1


# ============================================================
# D. durable job 正在运行：协作式取消 + 中止删除
# ============================================================

def test_library_clear_requests_job_cancel_before_catalog_cleanup(tmp_path, monkeypatch):
    """running durable job 存在时：置 cancel_requested，删除中止（409 语义），
    root/unit/revision 未删除。"""
    mirror = tmp_path / "mirror"
    mirror.mkdir(parents=True)
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror))
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": "",
            "openlist_mount_root": str(tmp_path / "quark"),
            "mirror_dir": str(mirror),
            "heartbeat_enabled": False,
            "auto_shutdown_on_heartbeat_timeout": False,
        })(),
    )
    graph = _build_graph(with_job=True)
    job_id = job_store.list_jobs(job_type="discovery_scan")[0].job_id
    _set_job_status(job_id, "running")

    from app.library.delete import CatalogCleanupBusyError, build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    with pytest.raises(CatalogCleanupBusyError):
        execute_delete(preview)

    # job 被请求协作式取消
    job = job_store.get_job(job_id)
    assert job is not None and job.cancel_requested is True
    # root/unit/revision 未删除
    assert _count("source_roots", "root_id", [graph["root_id"]]) == 1
    assert _count("media_units", "root_id", [graph["root_id"]]) == 1
    assert _count("import_revisions", "unit_id", [graph["unit_id"]]) == 1


def test_library_clear_api_returns_409_when_job_running(tmp_path, monkeypatch):
    """API 层确认删除时 running 任务 → 409，前端显示“相关后台任务正在停止”。"""
    from fastapi.testclient import TestClient

    from app.library.delete_store import save_delete_preview

    mirror = tmp_path / "mirror"
    mirror.mkdir(parents=True)
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror))
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": "",
            "openlist_mount_root": str(tmp_path / "quark"),
            "mirror_dir": str(mirror),
            "heartbeat_enabled": False,
            "auto_shutdown_on_heartbeat_timeout": False,
        })(),
    )
    graph = _build_graph()
    job_id = job_store.list_jobs(job_type="discovery_scan")[0].job_id
    _set_job_status(job_id, "running")

    from app.library.delete import build_library_clear_preview

    preview = build_library_clear_preview("openlist")
    save_delete_preview(preview)

    from app.main import app

    with TestClient(app) as client:
        resp = client.post(
            "/api/library/delete/library/confirm",
            json={"preview_id": preview.preview_id},
        )
    assert resp.status_code == 409
    assert "相关后台任务正在停止" in resp.json()["detail"]
    assert _count("source_roots", "root_id", [graph["root_id"]]) == 1


# ============================================================
# E. 删除单部作品不删除来源根
# ============================================================

def test_delete_one_work_does_not_remove_source_root(tmp_path, monkeypatch):
    """删除单部作品（scope=work）不得触碰任何来源根。"""
    graph = _build_graph()
    root_id = graph["root_id"]

    from app.library.delete import DeletePreview, execute_delete

    preview = DeletePreview(
        preview_id="work-preview",
        source="openlist",
        scope="work",
        work_id="raw-work",
        files=[],
    )
    execute_delete(preview)
    # 单作品删除（即使索引无此作品导致 failed）绝不触碰来源根与派生事实
    assert _count("source_roots", "root_id", [root_id]) == 1
    assert _count("media_units", "root_id", [root_id]) == 1
    assert _count("import_revisions", "unit_id", [graph["unit_id"]]) == 1


# ============================================================
# F. 完全相同路径：复用 root 增量扫描
# ============================================================

def test_exact_root_reimport_reuses_root_and_scans_incrementally():
    """完全相同路径重复导入：复用原 root，不创建重复来源根。"""
    _build_graph(with_batch=True, with_job=False)
    roots_before = store.list_source_roots("ol-conn")
    assert len(roots_before) == 1

    batch = store.create_import_batch(
        source_id="ol-conn",
        roots=[{"remote_locator": "/夸克网盘/动画/冰菓", "local_locator": "K:\\夸克\\动画\\冰菓"}],
    )
    root_item = batch["roots"][0]
    assert root_item["resolution"] == "exact_reused"
    assert root_item["root_id"] == roots_before[0].root_id
    assert len(store.list_source_roots("ol-conn")) == 1


# ============================================================
# G. 已有父目录覆盖新子目录：复用祖先，不 409
# ============================================================

def test_child_import_reuses_existing_ancestor():
    """已有父根时再导入其下子目录：复用父根、不新建 root、不报重叠错误。"""
    store.create_source(
        source_id="ol-conn", source_type="openlist", provider_id="quark",
        ingest_method="openlist_api",
    )
    parent = store.create_source_root(
        source_id="ol-conn", remote_locator="/夸克网盘/动画", local_locator="K:\\夸克\\动画",
    )
    batch = store.create_import_batch(
        source_id="ol-conn",
        roots=[{"remote_locator": "/夸克网盘/动画/冰菓", "local_locator": "K:\\夸克\\动画\\冰菓"}],
    )
    root_item = batch["roots"][0]
    assert root_item["resolution"] == "covered_by_existing_root"
    assert root_item["canonical_locator"] == "/夸克网盘/动画"
    assert root_item["root_id"] == parent.root_id
    roots = store.list_source_roots("ol-conn")
    assert len(roots) == 1
    assert roots[0].remote_locator == "/夸克网盘/动画"


# ============================================================
# H. 新父目录覆盖子目录：归并 + full 扫描语义
# ============================================================

def test_parent_import_promotes_descendant_roots():
    """新父目录覆盖多个子根：事务化归并，最终只有一个父 root，
    unit/revision 保留，子根不存在。"""
    store.create_source(
        source_id="ol-conn", source_type="openlist", provider_id="quark",
        ingest_method="openlist_api",
    )
    child_a = store.create_source_root(
        source_id="ol-conn", remote_locator="/夸克网盘/动画/冰菓",
        local_locator="K:\\夸克\\动画\\冰菓",
    )
    child_b = store.create_source_root(
        source_id="ol-conn", remote_locator="/夸克网盘/动画/辉夜大小姐",
        local_locator="K:\\夸克\\动画\\辉夜大小姐",
    )
    conn = get_connection()
    for child, remote, title in (
        (child_a, "/夸克网盘/动画/冰菓", "冰菓"),
        (child_b, "/夸克网盘/动画/辉夜大小姐", "辉夜大小姐"),
    ):
        unit_id = uuid_mod.uuid4().hex
        revision_id = uuid_mod.uuid4().hex
        conn.execute(
            """
            INSERT INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, '', ?, 'anime', ?, ?, 'plan_ready', 1, ?, '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
            """,
            (unit_id, child.root_id, remote, title, revision_id),
        )
        conn.execute(
            """
            INSERT INTO import_revisions (
                revision_id, unit_id, parent_revision_id, source, provider_id,
                source_generation, status, hash, created_at, updated_at
            ) VALUES (?, ?, '', 'openlist', 'quark', 1, 'executed', 'h', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
            """,
            (revision_id, unit_id),
        )
        conn.execute(
            """
            INSERT INTO import_revision_items (
                revision_id, item_id, source, relative_path, real_path, logical_locator,
                resource_type, action, work_id, work_title, media_type, group_type,
                season_number, episode_number, target_strm_path, confidence, needs_review
            ) VALUES (?, ?, 'openlist', 'S01E01.mkv', ?, ?, 'video', 'generate_strm', ?, ?, 'tv', 'season', 1, 1, ?, 'high', 0)
            """,
            (
                revision_id, f"item-{unit_id[:8]}",
                f"{remote}/S01E01.mkv", f"{remote}/S01E01.mkv",
                f"raw-{unit_id}", title, f"{remote}/S01E01.strm",
            ),
        )
    conn.commit()

    unit_ids = {
        str(row[0]) for row in conn.execute(
            "SELECT unit_id FROM media_units WHERE root_id IN (?, ?)",
            (child_a.root_id, child_b.root_id),
        ).fetchall()
    }
    revision_ids = {
        str(row[0]) for row in conn.execute(
            f"SELECT revision_id FROM import_revisions WHERE unit_id IN ({','.join('?' for _ in unit_ids)})",
            list(unit_ids),
        ).fetchall()
    }

    batch = store.create_import_batch(
        source_id="ol-conn",
        roots=[{"remote_locator": "/夸克网盘/动画", "local_locator": "K:\\夸克\\动画"}],
    )
    root_item = batch["roots"][0]
    assert root_item["resolution"] == "promoted_to_parent"
    assert set(root_item["covered_root_ids"]) == {child_a.root_id, child_b.root_id}

    roots = store.list_source_roots("ol-conn")
    assert len(roots) == 1
    parent = roots[0]
    assert parent.remote_locator == "/夸克网盘/动画"
    assert parent.root_id == root_item["root_id"]
    # unit/revision 保留且归属父根
    assert _count("media_units", "root_id", [parent.root_id]) == 2
    assert _count("media_units", "root_id", [child_a.root_id, child_b.root_id]) == 0
    remaining_units = {
        str(row[0]) for row in conn.execute(
            "SELECT unit_id FROM media_units WHERE root_id = ?", (parent.root_id,)
        ).fetchall()
    }
    assert remaining_units == unit_ids
    assert _count("import_revisions", "unit_id", list(unit_ids)) == len(unit_ids)
    assert _count("import_revision_items", "revision_id", list(revision_ids)) == len(revision_ids)
    # 旧子根不存在
    assert store.get_source_root(child_a.root_id) is None
    assert store.get_source_root(child_b.root_id) is None


# ============================================================
# I. 归并事务失败：整体回滚
# ============================================================

def test_parent_promotion_rolls_back_on_merge_failure(monkeypatch):
    """归并事务中途失败：所有旧子根与关联数据仍然完整，不产生半成品父根。"""
    store.create_source(
        source_id="ol-conn", source_type="openlist", provider_id="quark",
        ingest_method="openlist_api",
    )
    child_a = store.create_source_root(
        source_id="ol-conn", remote_locator="/夸克网盘/动画/冰菓",
        local_locator="K:\\夸克\\动画\\冰菓",
    )
    conn = get_connection()
    unit_id = uuid_mod.uuid4().hex
    conn.execute(
        """
        INSERT INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, created_at, updated_at
        ) VALUES (?, '', ?, 'anime', ?, '冰菓', 'plan_ready', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (unit_id, child_a.root_id, "/夸克网盘/动画/冰菓"),
    )
    conn.commit()

    # 归并事务内（创建父 root 时）now_iso 抛错 → 整体回滚
    monkeypatch.setattr(
        "app.catalog.store.now_iso",
        lambda: (_ for _ in ()).throw(RuntimeError("simulated merge failure")),
    )
    from app.catalog.lifecycle import promote_parent_root

    with pytest.raises(RuntimeError, match="simulated merge failure"):
        promote_parent_root(
            "ol-conn", "/夸克网盘/动画",
            local_locator="K:\\夸克\\动画",
            child_root_ids=[child_a.root_id],
        )

    # 旧子根与关联数据完整；无父根半成品
    roots = store.list_source_roots("ol-conn")
    assert [root.remote_locator for root in roots] == ["/夸克网盘/动画/冰菓"]
    assert _count("media_units", "root_id", [child_a.root_id]) == 1


def test_parent_promotion_rejects_active_jobs(monkeypatch):
    """归并前置检查：子根关联 durable job 正在运行时拒绝归并，数据完整。"""
    store.create_source(
        source_id="ol-conn", source_type="openlist", provider_id="quark",
        ingest_method="openlist_api",
    )
    child_a = store.create_source_root(
        source_id="ol-conn", remote_locator="/夸克网盘/动画/冰菓",
        local_locator="K:\\夸克\\动画\\冰菓",
    )
    job_store.create_job(
        job_type="discovery_scan",
        resource_key="scan:conn:ol-conn",
        payload={"root_id": child_a.root_id, "generation": 1, "source_id": "ol-conn"},
    )
    _set_job_status(job_store.list_jobs(job_type="discovery_scan")[0].job_id, "running")

    from app.catalog.lifecycle import promote_parent_root

    with pytest.raises(ValueError, match="相关后台任务正在运行"):
        promote_parent_root(
            "ol-conn", "/夸克网盘/动画",
            local_locator="K:\\夸克\\动画",
            child_root_ids=[child_a.root_id],
        )

    assert len(store.list_source_roots("ol-conn")) == 1
    assert store.get_source_root(child_a.root_id) is not None


# ============================================================
# J. TXT 目录树来源不受整库清理影响
# ============================================================

def test_txt_tree_roots_untouched_by_source_scoped_clear():
    """pan115/baidu TXT 目录树来源：按来源清库不选择其 SourceRoot（若存在）。"""
    store.create_source(
        source_id="txt-pan", source_type="txt", provider_id="pan115",
        ingest_method="directory_tree",
    )
    txt_root = store.create_source_root(
        source_id="txt-pan", remote_locator="/115网盘/动画", local_locator="K:\\115\\动画",
    )

    assert lifecycle.list_roots_for_library_clear("pan115") == []
    assert lifecycle.list_roots_for_library_clear("baidu") == []

    preview = lifecycle.preview_catalog_cleanup("pan115")
    assert preview.root_count == 0

    result = lifecycle.delete_catalog_for_clear("pan115")
    assert result.deleted_root_count == 0
    # TXT 来源根原样保留
    assert store.get_source_root(txt_root.root_id) is not None
    # 而 all 清库会覆盖全部 Source Catalog 管理的来源根（含 txt 场景）
    assert lifecycle.list_roots_for_library_clear("all")[0].root_id == txt_root.root_id


# ============================================================
# 复核新增：父根归并后必须对新父目录全扫，兄弟作品才能被发现
# ============================================================

class _FakeClient:
    """沿用 test_media_discovery 的最小假客户端（OpenList 契约）。"""

    def __init__(self, tree):
        self.tree = tree

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        from app.integrations.openlist.models import OpenListEntry

        items = self.tree.get(path, [])
        start = (page - 1) * per_page
        chunk = items[start:start + per_page]
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=f"{path.rstrip('/')}/{name}",
            )
            for name, is_dir, size, modified in chunk
        ]
        return type("Page", (), {"entries": entries, "total": len(items)})()


def _setup_openlist_root(remote="/动画"):
    store.create_source(source_id="ol", source_type="openlist", provider_id="quark")
    return store.create_source_root(source_id="ol", remote_locator=remote)


def _insert_work_unit(root_id: str, boundary: str, title: str) -> str:
    """为已归并/既有根插入一个 plan_ready unit（识别历史）。"""
    conn = get_connection()
    unit_id = uuid_mod.uuid4().hex
    revision_id = uuid_mod.uuid4().hex
    conn.execute(
        """
        INSERT INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, closure_generation, current_revision_id, created_at, updated_at
        ) VALUES (?, '', ?, 'anime', ?, ?, 'plan_ready', 1, ?, '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (unit_id, root_id, boundary, title, revision_id),
    )
    conn.execute(
        """
        INSERT INTO import_revisions (
            revision_id, unit_id, parent_revision_id, source, provider_id,
            source_generation, status, hash, created_at, updated_at
        ) VALUES (?, ?, '', 'openlist', 'quark', 1, 'executed', 'h', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
        """,
        (revision_id, unit_id),
    )
    conn.commit()
    return unit_id


def test_parent_promotion_frontier_starts_from_parent_and_finds_siblings():
    """归并后父根 frontier 从父目录本身（depth=0）重新开始，兄弟作品可被发现。"""
    from app.catalog import discovery
    from app.integrations.openlist.scanner import OpenListDirectoryScanner

    child = _setup_openlist_root("/动画/冰菓")
    # 识别历史：冰菓 unit 挂在旧子根上
    hyoka_unit = _insert_work_unit(child.root_id, "/动画/冰菓", "冰菓")
    # 构造旧子根物理扫描事实（模拟子根已扫描过一次）
    store.upsert_directory(child.root_id, "/动画/冰菓")
    store.update_directory(child.root_id, "/动画/冰菓", state="complete", depth=0)

    # 归并旧子根到新父根 /动画
    resolution = lifecycle.resolve_root_for_import(child.source_id, "/动画")
    assert resolution.action == "promote_parent"
    promoted = lifecycle.promote_parent_root(
        child.source_id, "/动画", local_locator="K:/动画", child_root_ids=[child.root_id],
    )
    parent_root = store.get_source_root(promoted.canonical_root_id)
    assert parent_root is not None
    assert parent_root.remote_locator == "/动画"
    # 单元保留并归属父根
    assert store.get_connection().execute(
        "SELECT root_id FROM media_units WHERE unit_id = ?", (hyoka_unit,)
    ).fetchone()["root_id"] == parent_root.root_id
    # 旧子根物理扫描事实不迁移
    assert store.list_all_directories(child.root_id) == []
    assert store.list_nodes(child.root_id) == []
    assert store.get_source_root(child.root_id) is None

    # 父根 frontier：从父目录本身重新开始，且 depth=0
    store.prepare_scan(parent_root.root_id, generation=1, mode="full")
    dirs = store.list_all_directories(parent_root.root_id)
    assert {row["remote_path"]: row["depth"] for row in dirs} == {"/动画": 0}

    # 父目录下存在未导入过的兄弟作品 → 跑 discovery 应被发现为新 unit
    tree = {
        "/动画": [("冰菓", True, None, None), ("辉夜大小姐", True, None, None)],
        "/动画/冰菓": [("Season 1", True, None, None)],
        "/动画/冰菓/Season 1": [("冰菓 - S01E01.mkv", False, 100, 1.0)],
        "/动画/辉夜大小姐": [("Season 1", True, None, None)],
        "/动画/辉夜大小姐/Season 1": [("辉夜大小姐 - S01E01.mkv", False, 100, 1.0)],
    }
    scanner = OpenListDirectoryScanner(_FakeClient(tree), rate_per_second=0)
    engine = discovery.DiscoveryEngine(
        scanner, source_id=child.source_id, root_id=parent_root.root_id, generation=2,
    )
    # parent frontier 已有 /动画；把 /动画 置为 queued 供 engine 领扫
    store.update_directory(parent_root.root_id, "/动画", state="queued")
    results = engine.run()
    unit_rows = {
        str(row["boundary"]): str(row["unit_id"])
        for row in store.get_connection().execute(
            "SELECT boundary, unit_id FROM media_units WHERE root_id = ?",
            (parent_root.root_id,),
        ).fetchall()
    }
    # 冰菓旧 unit 复用（boundary 相同）；辉夜是新单元
    assert "/动画/冰菓" in unit_rows
    assert unit_rows["/动画/冰菓"] == hyoka_unit
    assert "/动画/辉夜大小姐" in unit_rows
    assert any(r.get("boundary") == "/动画/辉夜大小姐" for r in results)


def test_promotion_does_not_keep_stale_child_layer_state():
    """归并后不保留旧子根的错误层级目录状态（parent_path/depth 不污染 frontier）。"""
    child = _setup_openlist_root("/动画/冰菓")
    # 子根有一棵已知目录树（层级 0/1）
    for path, depth in [("/动画/冰菓", 0), ("/动画/冰菓/第1季", 1)]:
        store.upsert_directory(child.root_id, path, parent_path="" if depth == 0 else "/动画/冰菓", depth=depth)
        store.update_directory(child.root_id, path, state="complete", depth=depth)
    promoted = lifecycle.promote_parent_root(
        child.source_id, "/动画", local_locator="K:/动画", child_root_ids=[child.root_id],
    )
    parent_root = store.get_source_root(promoted.canonical_root_id)
    store.prepare_scan(parent_root.root_id, generation=1, mode="full")
    dirs = {row["remote_path"]: (row["depth"], row["parent_path"]) for row in store.list_all_directories(parent_root.root_id)}
    # 只从新父目录开始，旧子根既不会迁移成错误层级，也不会残留
    assert dirs == {"/动画": (0, "")}
    assert "/动画/冰菓" not in dirs


# ============================================================
# 复核新增：扫描暂存孤儿
# ============================================================

def test_single_source_clear_removes_stage_no_orphan():
    """单来源整库删除按其 run→root 归属精确清理扫描暂存，无孤儿 stage。"""
    graph = _build_graph(with_batch=True, with_job=True)
    root_id = graph["root_id"]
    # 登记一次扫描暂存
    run_id = store.new_stage_run()
    store.register_stage_run(run_id, root_id)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO source_stage_entries (run_id, directory_path, remote_path, page, name, kind)
        VALUES (?, '/夸克网盘/动画/冰菓', ?, 1, 'S01E01.mkv', 'file')
        """,
        (run_id, "/夸克网盘/动画/冰菓/S01E01.mkv"),
    )
    conn.commit()

    gate = lifecycle.prepare_catalog_cleanup("openlist")
    assert gate["running_job_ids"] == []
    lifecycle.delete_catalog_for_clear("openlist")

    assert conn.execute("SELECT COUNT(*) FROM source_stage_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_stage_runs").fetchone()[0] == 0


def test_scan_cancel_leaves_no_stage_orphan(tmp_path, monkeypatch):
    """扫描目录被取消后不留扫描暂存与 run→root 归属。"""
    from app.catalog.service import ScanCancelled, scan_directory_paginated

    root = _setup_openlist_root("/动画")
    run_before = len(get_connection().execute("SELECT * FROM source_stage_entries").fetchall())

    class _CancellingScanner:
        def enumerate_directory(self, remote_path, page=1, per_page=100):
            raise ScanCancelled()

    with pytest.raises(ScanCancelled):
        scan_directory_paginated(
            _CancellingScanner(), root.root_id, "/动画", 1,
            per_page=100,
        )
    assert len(get_connection().execute("SELECT * FROM source_stage_entries").fetchall()) == run_before
    assert len(get_connection().execute("SELECT * FROM source_stage_runs").fetchall()) == run_before


# ============================================================
# 复核新增：running 任务返回 409 前不得清追更状态
# ============================================================

def test_library_clear_running_job_keeps_tracking_state(tmp_path, monkeypatch):
    """running durable job 时删除中止（409 语义），追更绑定与扫描记录保持不变。"""
    mirror = tmp_path / "mirror"
    mirror.mkdir(parents=True)
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror))
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": "",
            "openlist_mount_root": str(tmp_path / "quark"),
            "mirror_dir": str(mirror),
            "heartbeat_enabled": False,
            "auto_shutdown_on_heartbeat_timeout": False,
        })(),
    )
    graph = _build_graph(with_job=True)
    job_id = job_store.list_jobs(job_type="discovery_scan")[0].job_id
    _set_job_status(job_id, "running")
    # 造一条追更绑定
    from app.tracking.models import TrackingBinding
    from app.tracking.store import upsert_tracking_binding

    upsert_tracking_binding(TrackingBinding(
        work_id="hyoka-work", display_title="冰菓",
        logical_source="local", root_path="K:\\动画\\冰菓",
    ))

    from app.library.delete import CatalogCleanupBusyError, build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    with pytest.raises(CatalogCleanupBusyError):
        execute_delete(preview)

    # running job 置取消标记；追更绑定/扫描记录保留（门控先于追更清理）
    assert job_store.get_job(job_id).cancel_requested is True
    conn = get_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM tracking_bindings WHERE work_id = 'hyoka-work'"
    ).fetchone()[0] == 1
    assert _count("source_roots", "root_id", [graph["root_id"]]) == 1


# ============================================================
# 复核新增：prepare 与正式删除之间插入新 job 必须回滚
# ============================================================

def test_delete_race_insert_active_job_rolls_back(tmp_path, monkeypatch):
    """prepare 之后、正式删除事务内若出现新 running 任务，删除整体回滚并 409。"""
    graph = _build_graph(with_batch=False, with_job=False)
    root_id = graph["root_id"]

    # 事务外先"通过"门控（无任务）
    gate = lifecycle.prepare_catalog_cleanup("openlist")
    assert gate["running_job_ids"] == []

    running_job = job_store.create_job(
        job_type="discovery_scan", resource_key="scan:conn:ol-conn",
        payload={"root_id": root_id, "generation": 1, "source_id": "ol-conn"},
    )
    _set_job_status(running_job.job_id, "running")

    with pytest.raises(lifecycle.CatalogCleanupBusyError):
        lifecycle.delete_catalog_for_clear("openlist")

    # 全部回滚：root/unit/revision 保持完整
    assert _count("source_roots", "root_id", [root_id]) == 1
    assert _count("media_units", "root_id", [root_id]) == 1
    assert _count("import_revisions", "unit_id", [graph["unit_id"]]) == 1


# ============================================================
# 复核新增：批次两个请求映射同一既有父根 → 去重
# ============================================================

def test_batch_two_requests_map_to_same_ancestor_deduplicated():
    """同一批次两个子目录都被同一既有父根覆盖：批次成功且只含一个规范 root。"""
    store.create_source(source_id="ol-conn", source_type="openlist", provider_id="quark")
    parent = store.create_source_root(
        source_id="ol-conn", remote_locator="/夸克网盘/动画", local_locator="K:\\夸克\\动画",
    )
    batch = store.create_import_batch(
        source_id="ol-conn",
        roots=[
            {"remote_locator": "/夸克网盘/动画/冰菓", "local_locator": "K:\\夸克\\动画\\冰菓"},
            {"remote_locator": "/夸克网盘/动画/辉夜大小姐", "local_locator": "K:\\夸克\\动画\\辉夜大小姐"},
        ],
    )
    # 只有一个规范 root（父根），两条请求都被覆盖
    assert len(batch["roots"]) == 1
    assert batch["roots"][0]["root_id"] == parent.root_id
    assert batch["roots"][0]["resolution"] == "covered_by_existing_root"
    conn = get_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM import_batch_roots WHERE batch_id = ?", (batch["batch_id"],)
    ).fetchone()[0] == 1


# ============================================================
# 复核新增：批次后续步骤失败，父根归并不留半成品
# ============================================================

def test_batch_promote_failure_rolls_back_merge(monkeypatch):
    """批次内归并与批次创建同一事务：归并中途失败则子根/父根/批次全部回滚。"""
    store.create_source(source_id="ol-conn", source_type="openlist", provider_id="quark")
    child = store.create_source_root(
        source_id="ol-conn", remote_locator="/夸克网盘/动画/冰菓",
        local_locator="K:\\夸克\\动画\\冰菓",
    )
    unit_id = _insert_work_unit(child.root_id, "/夸克网盘/动画/冰菓", "冰菓")

    # 归并在批次事务内执行：monkeypatch now_iso 在其首次调用（建父根）抛错
    real_now = store.now_iso
    monkeypatch.setattr(
        "app.catalog.store.now_iso",
        lambda: (_ for _ in ()).throw(RuntimeError("simulated batch failure")),
    )

    with pytest.raises(RuntimeError, match="simulated batch failure"):
        store.create_import_batch(
            source_id="ol-conn",
            roots=[{"remote_locator": "/夸克网盘/动画", "local_locator": "K:\\夸克\\动画"}],
        )
    monkeypatch.setattr("app.catalog.store.now_iso", real_now)

    # 无半成品：旧子根仍在、无父根、无批次
    roots = store.list_source_roots("ol-conn")
    assert [root.remote_locator for root in roots] == ["/夸克网盘/动画/冰菓"]
    assert store.get_source_root(child.root_id) is not None
    assert _count("media_units", "root_id", [child.root_id]) == 1
    assert get_connection().execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    assert unit_id in {
        str(row[0]) for row in get_connection().execute(
            "SELECT unit_id FROM media_units WHERE root_id = ?", (child.root_id,)
        ).fetchall()
    }


# ============================================================
# 复核第三轮：扫描暂存归属生命周期（真实扫描调用链）
# ============================================================

def _first_page_then_cancel_scanner():
    """第一页正常返回（写入暂存），拉第二页时抛取消。"""

    class _Scanner:
        def __init__(self):
            self.page_calls = 0

        def enumerate_directory(self, remote_path, page=1, per_page=100):
            from app.catalog.models import SourceNodeInput
            from app.catalog.service import ScanCancelled

            self.page_calls += 1
            if page > 1:
                raise ScanCancelled()
            # 第一页返回满页（per_page=100,total=101），service 会拉第二页；
            # 第一页已写入暂存，第二页取消 → 验证取消后无 stage 孤儿。
            entries = [
                SourceNodeInput(
                    name=f"f{i}.mkv", remote_path=f"{remote_path}/f{i}.mkv",
                    parent_path=remote_path, kind="file", size=100, mtime=1.0,
                )
                for i in range(100)
            ]
            return type("Page", (), {"entries": entries, "total": 101})()

    return _Scanner()


def test_cancel_after_first_page_writes_stage_leaves_no_orphan():
    """第一页已写入暂存后取消：归属与暂存都被清理，无孤儿。"""
    from app.catalog.service import ScanCancelled, scan_directory_paginated

    root = _setup_openlist_root("/动画")
    scanner = _first_page_then_cancel_scanner()
    with pytest.raises(ScanCancelled):
        scan_directory_paginated(
            scanner, root.root_id, "/动画", 1, per_page=100,
        )
    assert get_connection().execute("SELECT COUNT(*) FROM source_stage_runs").fetchone()[0] == 0
    assert get_connection().execute("SELECT COUNT(*) FROM source_stage_entries").fetchone()[0] == 0


def test_stage_ownership_persists_while_scanning_then_cleared():
    """扫描写入期间 source_stage_runs 归属持续存在；删除来源根时精确清理。"""
    root = _setup_openlist_root("/动画")
    # 直接验证：register 后归属存在
    run_id = store.new_stage_run()
    store.register_stage_run(run_id, root.root_id)
    conn = get_connection()
    conn.execute(
        "INSERT INTO source_stage_entries (run_id, directory_path, remote_path, page, name, kind) "
        "VALUES (?, '/动画', '/动画/a.mkv', 1, 'a.mkv', 'file')",
        (run_id,),
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM source_stage_runs WHERE run_id = ?", (run_id,)).fetchone()[0] == 1

    # 在途删除该来源根：归属与暂存一并精确清理
    store.delete_stage_for_roots([root.root_id])
    assert conn.execute("SELECT COUNT(*) FROM source_stage_runs WHERE run_id = ?", (run_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_stage_entries WHERE run_id = ?", (run_id,)).fetchone()[0] == 0


def test_scan_in_progress_source_root_delete_clears_stage():
    """扫描进行中删除该来源根：按 run→root 归属精确清理其 in-flight 暂存。"""
    root = _setup_openlist_root("/本地根")
    run_id = store.new_stage_run()
    store.register_stage_run(run_id, root.root_id)
    conn = get_connection()
    conn.execute(
        "INSERT INTO source_stage_entries (run_id, directory_path, remote_path, page, name, kind) "
        "VALUES (?, '/本地根', '/本地根/a.mkv', 1, 'a.mkv', 'file')",
        (run_id,),
    )
    conn.commit()

    # 按来源根精确清理（本地来源）
    lifecycle.delete_catalog_for_clear("all")
    assert conn.execute("SELECT COUNT(*) FROM source_stage_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_stage_entries").fetchone()[0] == 0


def test_promotion_clears_child_inflight_stage():
    """父根归并时精确清理子根的 in-flight 扫描暂存。"""
    store.create_source(source_id="ol-prom", source_type="openlist", provider_id="quark")
    child = store.create_source_root(
        source_id="ol-prom", remote_locator="/动画/冰菓", local_locator="K:/动画/冰菓",
    )
    run_id = store.new_stage_run()
    store.register_stage_run(run_id, child.root_id)
    conn = get_connection()
    conn.execute(
        "INSERT INTO source_stage_entries (run_id, directory_path, remote_path, page, name, kind) "
        "VALUES (?, '/动画/冰菓', '/动画/冰菓/a.mkv', 1, 'a.mkv', 'file')",
        (run_id,),
    )
    conn.commit()

    lifecycle.promote_parent_root(
        "ol-prom", "/动画", local_locator="K:/动画", child_root_ids=[child.root_id],
    )
    # 子根归属与暂存被清；父根无旧子根物理事实
    assert conn.execute("SELECT COUNT(*) FROM source_stage_runs WHERE root_id = ?", (child.root_id,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_stage_entries").fetchone()[0] == 0


# ============================================================
# 复核第三轮：维护屏障（整库删除全程无任务竞争）
# ============================================================

def test_maintenance_barrier_blocks_enqueue_and_claim():
    """屏障生效期间：新的任务不能入队、也不能被领取。"""
    from app.catalog import maintenance_guard
    from app.jobs import store as job_store

    assert maintenance_guard.is_active() is False
    with maintenance_guard.hold():
        assert maintenance_guard.is_active() is True
        # 入队被拒
        with pytest.raises(RuntimeError, match="维护进行中"):
            job_store.create_job(job_type="discovery_scan", payload={"root_id": "x"})
        # 领取返回空
        assert job_store.claim_jobs(worker_id="w") == []
    assert maintenance_guard.is_active() is False


def test_library_clear_running_job_preserves_mirror_and_presets(tmp_path, monkeypatch):
    """409（running 任务）时镜像、预设、追更、LibraryIndex、Source Catalog 全部保留。"""
    mirror = tmp_path / "mirror"
    openlist_dir = mirror / "openlist" / "冰菓" / "Season 1"
    openlist_dir.mkdir(parents=True)
    strm = openlist_dir / "S01E01.strm"
    strm.write_text("K:\\夸克\\动画\\冰菓\\S01E01.mkv", encoding="utf-8")
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror))
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": "",
            "openlist_mount_root": str(tmp_path / "quark"),
            "mirror_dir": str(mirror),
            "heartbeat_enabled": False,
            "auto_shutdown_on_heartbeat_timeout": False,
        })(),
    )
    graph = _build_graph(with_job=True)
    job_id = job_store.list_jobs(job_type="discovery_scan")[0].job_id
    _set_job_status(job_id, "running")
    # 媒体库索引 + 预设
    from app.library.models import LibraryIndex
    from app.library.store import save_library_index
    from app.media_presets.models import MediaLibraryPreset
    from app.media_presets.store import save_preset

    save_library_index(LibraryIndex(works=[]))
    save_preset(MediaLibraryPreset(preset_id="ol-preset", source="openlist"))

    from app.library.delete import CatalogCleanupBusyError, build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    with pytest.raises(CatalogCleanupBusyError):
        execute_delete(preview)

    # 全部保留（门控在一切修改之前，屏障阻止新任务覆盖窗口）
    assert strm.exists()
    from app.media_presets.store import get_preset
    assert get_preset("ol-preset") is not None
    assert _count("source_roots", "root_id", [graph["root_id"]]) == 1
    assert _count("media_units", "root_id", [graph["root_id"]]) == 1


def test_execute_library_clear_full_consistency(tmp_path, monkeypatch):
    """屏障下完整 execute_delete：镜像、预设、追更、LibraryIndex、Source Catalog 一致删除。"""
    mirror = tmp_path / "mirror"
    openlist_dir = mirror / "openlist" / "冰菓" / "Season 1"
    openlist_dir.mkdir(parents=True)
    strm = openlist_dir / "S01E01.strm"
    strm.write_text("K:\\夸克\\动画\\冰菓\\S01E01.mkv", encoding="utf-8")
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror))
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": "",
            "openlist_mount_root": str(tmp_path / "quark"),
            "mirror_dir": str(mirror),
            "heartbeat_enabled": False,
            "auto_shutdown_on_heartbeat_timeout": False,
        })(),
    )
    graph = _build_graph(with_batch=True, with_job=True)
    from app.media_presets.models import MediaLibraryPreset
    from app.media_presets.store import save_preset
    save_preset(MediaLibraryPreset(preset_id="ol-preset", source="openlist"))

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    result = execute_delete(preview)
    assert result.status == "succeeded"
    # 镜像、预设、追更、Source Catalog 一致删除
    assert not strm.exists()
    from app.media_presets.store import get_preset
    assert get_preset("ol-preset") is None
    assert _count("source_roots", "root_id", [graph["root_id"]]) == 0
    assert _count("media_units", "root_id", [graph["root_id"]]) == 0
    assert get_connection().execute("SELECT COUNT(*) FROM source_stage_runs").fetchone()[0] == 0


# ============================================================
# 复核第三轮：旧 v3 库 stage 迁移
# ============================================================

def test_legacy_db_migration_clears_orphan_stage_entries(tmp_path, monkeypatch):
    """旧 v3 库：补建 source_stage_runs 后无归属的旧暂存被安全清除。"""
    import app.db.database as db_mod

    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db_mod, "_db_path", db_path)
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    # 先手动构造一个旧库（有 source_stage_entries，无 source_stage_runs）
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "CREATE TABLE source_stage_entries (run_id TEXT, directory_path TEXT, remote_path TEXT, page INTEGER, name TEXT, kind TEXT)"
    )
    conn.execute(
        "INSERT INTO source_stage_entries VALUES ('legacy-run', '/动画', '/动画/a.mkv', 1, 'a.mkv', 'file')"
    )
    conn.commit()
    conn.close()

    # init_db 会补建 stage_runs 表并清除无归属暂存
    db_mod.init_db()
    leftover = db_mod.get_connection().execute(
        "SELECT COUNT(*) FROM source_stage_entries"
    ).fetchone()[0]
    assert leftover == 0
    db_mod.close_connection()


# ============================================================
# 复核第四轮：admission gate 确定性并发（检查与写入之间开启屏障）
# ============================================================

def test_admission_check_to_insert_window_barrier_waits_then_rejects():
    """admission 检查后、INSERT 提交前开启屏障：删除等待在途操作完成，
    完成后新 admission 被可靠拒绝（检查与写入原子）。"""
    import threading
    import time

    from app.catalog import maintenance_guard

    gate = maintenance_guard._gate
    acquired = threading.Event()
    proceed = threading.Event()
    release_barrier = threading.Event()
    barrier_entered = threading.Event()
    outcome: dict[str, bool] = {}

    def task_thread():
        ok = gate.acquire()  # 模拟「检查屏障通过」→ 进入准入
        outcome["task_admitted"] = ok
        acquired.set()
        proceed.wait(10)  # 模拟 INSERT 前暂停
        gate.release()

    def deleter_thread():
        gate.enter_barrier()  # 删除：应等待在途 admission 完成；保持屏障（不退出）
        outcome["barrier_entered"] = True
        barrier_entered.set()
        release_barrier.wait(5)
        gate.exit_barrier()

    t1 = threading.Thread(target=task_thread)
    t2 = threading.Thread(target=deleter_thread)
    t1.start()
    assert acquired.wait(5), "任务线程未进入准入"
    t2.start()
    time.sleep(0.2)
    # 删除在等待任务完成，尚未置位屏障
    assert "barrier_entered" not in outcome
    # 任务此时「提交」：放行 → 释放准入
    proceed.set()
    t1.join(5)
    assert barrier_entered.wait(5)
    assert outcome["task_admitted"] is True
    assert outcome["barrier_entered"] is True
    # 屏障激活期间新 admission 被拒绝（不可进入）
    assert gate.acquire() is False
    # 退出屏障后恢复
    release_barrier.set()
    t2.join(5)
    assert gate.acquire() is True
    gate.release()


def test_admission_claim_window_barrier_waits_then_claim_denied():
    """claim 的 admission 持有整个领取事务：屏障等待其完成；激活后领取返回空。"""
    import threading
    import time

    from app.catalog import maintenance_guard

    gate = maintenance_guard._gate
    claimed = threading.Event()
    proceed = threading.Event()
    release_barrier = threading.Event()
    barrier_entered = threading.Event()
    outcome: dict[str, object] = {}

    def claim_thread():
        # 模拟 worker 领取：admission 内执行 claim（同 gate 语义）
        ok = gate.acquire()
        outcome["claim_admitted"] = ok
        claimed.set()
        proceed.wait(10)
        if ok:
            gate.release()

    def deleter_thread():
        gate.enter_barrier()
        outcome["barrier_entered"] = True
        barrier_entered.set()
        release_barrier.wait(5)
        gate.exit_barrier()

    t1 = threading.Thread(target=claim_thread)
    t2 = threading.Thread(target=deleter_thread)
    t1.start()
    assert claimed.wait(5)
    t2.start()
    time.sleep(0.2)
    assert "barrier_entered" not in outcome
    proceed.set()
    t1.join(5)
    assert barrier_entered.wait(5)
    assert outcome["claim_admitted"] is True
    assert outcome["barrier_entered"] is True
    # 屏障激活：新 claim（admission）被拒 → 返回空（claim_jobs 语义）
    from app.catalog import maintenance_guard as mg
    from app.jobs import store as job_store

    assert job_store.claim_jobs(worker_id="w") == []
    with pytest.raises(mg.MaintenanceAdmissionDenied):
        job_store.create_job(job_type="x", payload={})
    release_barrier.set()
    t2.join(5)


def test_import_batch_admission_covers_create_to_enqueue_window():
    """批次创建后、任务入队前开启屏障：删除等待导入整体完成，
    批次/root/job 全部一致存在（无半成品），随后屏障才进入。"""
    import threading
    import time

    from app.catalog import maintenance_guard

    store.create_source(source_id="ol-adm", source_type="openlist", provider_id="quark")
    batch_created = threading.Event()
    proceed = threading.Event()
    outcome: dict[str, object] = {}

    def import_thread():
        try:
            with maintenance_guard.admission():
                # 创建/复用根 + 批次（事务已提交）
                batch = store.create_import_batch(
                    source_id="ol-adm",
                    roots=[{"remote_locator": "/动画", "local_locator": "K:/动画"}],
                )
                outcome["batch_id"] = batch["batch_id"]
                outcome["root_id"] = batch["roots"][0]["root_id"]
                batch_created.set()
                # 任务入队前暂停：此时删除若尝试进入屏障必须等待
                proceed.wait(10)
                # 入队（admission 内必然成功，屏障无法进入）
                generation = store.bump_generation(batch["roots"][0]["root_id"])
                job = job_store.create_job(
                    job_type="discovery_scan", resource_key="scan:conn:ol-adm",
                    payload={"root_id": batch["roots"][0]["root_id"], "generation": generation},
                )
                outcome["job_id"] = job.job_id
        except Exception as exc:  # pragma: no cover
            outcome["error"] = repr(exc)

    def deleter_thread():
        maintenance_guard._gate.enter_barrier()
        outcome["barrier_entered"] = True
        maintenance_guard._gate.exit_barrier()

    t1 = threading.Thread(target=import_thread)
    t2 = threading.Thread(target=deleter_thread)
    t1.start()
    assert batch_created.wait(5), "导入未完成批次创建"
    t2.start()
    time.sleep(0.2)
    # 批次已提交但入队未完成：删除必须等待
    assert "barrier_entered" not in outcome
    proceed.set()
    t1.join(5)
    t2.join(5)
    assert "error" not in outcome
    assert outcome["barrier_entered"] is True
    # 无半成品：批次/root/job 全部一致存在
    conn = get_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM import_batches WHERE batch_id = ?", (outcome["batch_id"],)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM source_roots WHERE root_id = ?", (outcome["root_id"],)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE job_id = ?", (outcome["job_id"],)
    ).fetchone()[0] == 1


def test_deletion_either_sees_committed_tasks_or_gets_barrier_first():
    """两种结局之一（无第三态）：admission 先 → 任务提交后删除才进入；
    屏障先 → 任务被可靠拒绝，删除完整。"""
    from app.catalog import maintenance_guard

    gate = maintenance_guard._gate
    # 结局 A：任务先获得 admission 并提交（acquire→release）→ 删除随后进入屏障，
    # 屏障激活期间新任务被拒 → 不存在「任务提交中途被删除打断」的部分状态。
    assert gate.acquire() is True
    gate.release()  # 任务完成提交
    gate.enter_barrier()
    assert gate.is_active() is True
    assert gate.acquire() is False  # 删除先到 → 任务被拒
    gate.exit_barrier()
    # 结局 B：删除先进入 → 后续 admission 全部拒绝；退出后恢复
    gate.enter_barrier()
    assert gate.acquire() is False
    gate.exit_barrier()
    assert gate.acquire() is True
    gate.release()


def test_waiting_barrier_stops_new_admission_before_becoming_active():
    """删除已经开始等待时，后续 admission 不能持续插队令删除饥饿。"""
    import threading
    import time

    from app.catalog import maintenance_guard

    gate = maintenance_guard._gate
    assert gate.acquire() is True  # 既有导入/领取正在提交
    entered = threading.Event()
    release_barrier = threading.Event()

    def _enter_barrier():
        gate.enter_barrier()
        entered.set()
        release_barrier.wait(5)
        gate.exit_barrier()

    deleter = threading.Thread(target=_enter_barrier)
    deleter.start()
    for _ in range(50):
        if getattr(gate, "_waiting_barriers", 0) == 1:
            break
        time.sleep(0.01)
    assert getattr(gate, "_waiting_barriers", 0) == 1
    # 屏障尚未真正激活，但另一请求不得越过等待中的删除继续进入。
    late_admission: dict[str, bool] = {}

    def _late_request():
        late_admission["accepted"] = gate.acquire()

    late = threading.Thread(target=_late_request)
    late.start()
    late.join(5)
    assert late_admission["accepted"] is False
    gate.release()
    assert entered.wait(5)
    release_barrier.set()
    deleter.join(5)


def test_barrier_is_exclusive_across_threads_but_reentrant_for_owner():
    """两个删除不能并行；同一删除调用栈仍可安全嵌套 hold。"""
    import threading
    import time

    from app.catalog import maintenance_guard

    gate = maintenance_guard._gate
    gate.enter_barrier()
    gate.enter_barrier()  # 同一线程嵌套
    second_entered = threading.Event()

    def _second_deleter():
        gate.enter_barrier()
        second_entered.set()
        gate.exit_barrier()

    second = threading.Thread(target=_second_deleter)
    second.start()
    time.sleep(0.1)
    assert second_entered.is_set() is False
    gate.exit_barrier()
    gate.exit_barrier()
    assert second_entered.wait(5)
    second.join(5)


def test_import_batch_api_rejected_during_barrier_no_partial(tmp_path, monkeypatch):
    """删除屏障激活时 OpenList 导入批次被拒（409），无批次/root/job 半成品。"""
    from fastapi.testclient import TestClient

    from app.api import openlist as openlist_api
    from app.core import config as core_config
    from app.main import app

    # 显式提供 OpenList 配置（不依赖 load_config 的跨测试状态）
    saved_routes: list = []

    def _fresh_config():
        cfg = core_config.load_config(force_reload=True)
        cfg.openlist_server_url = "https://ol.example.com:5244"
        cfg.openlist_remote_root = "/夸克网盘"
        cfg.openlist_mount_root = str(tmp_path / "quark")
        cfg.openlist_username = "quark-user"
        cfg.openlist_password = "p@ssw0rd"
        cfg.openlist_routes = saved_routes
        return cfg

    monkeypatch.setattr(openlist_api, "load_config", _fresh_config)

    def _save_config(config) -> None:
        saved_routes[:] = list(config.openlist_routes or [])

    monkeypatch.setattr(openlist_api, "save_config", _save_config)

    with TestClient(app) as client:
        resp = client.put(
            "/api/openlist/routes",
            json={"routes": [{
                "route_id": "r1", "label": "动画",
                "remote_prefix": "/夸克网盘/动画", "provider_id": "quark", "enabled": True,
            }]},
        )
        assert resp.status_code == 200, resp.text

        from app.catalog import maintenance_guard

        with maintenance_guard.hold():
            resp = client.post(
                "/api/openlist/import-batch",
                json={"remote_paths": ["/夸克网盘/动画"], "import_family": "anime"},
            )
        assert resp.status_code == 409
        assert "维护进行中" in resp.json()["detail"]

    # 无半成品：无批次、无 source root、无 job
    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_roots").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_full_validate_api_rejected_during_barrier_keeps_existing_batch_state():
    """屏障期完整校验不得先取消旧任务或推进 generation。"""
    from fastapi.testclient import TestClient

    from app.catalog import maintenance_guard
    from app.main import app

    store.create_source(source_id="ol-full-guard", source_type="openlist", provider_id="quark")
    batch = store.create_import_batch(
        source_id="ol-full-guard",
        roots=[{"remote_locator": "/动画", "local_locator": "K:/动画"}],
    )
    root_id = batch["roots"][0]["root_id"]
    existing = job_store.create_job(
        job_type="discovery_scan",
        resource_key="scan:conn:ol-full-guard",
        payload={"root_id": root_id, "generation": 0},
    )
    get_connection().execute(
        "UPDATE jobs SET status = 'succeeded', progress = 100 WHERE job_id = ?",
        (existing.job_id,),
    )
    get_connection().commit()
    with TestClient(app) as client:
        with maintenance_guard.hold():
            response = client.post(f"/api/openlist/import-batches/{batch['batch_id']}/full-validate")
    assert response.status_code == 409
    assert job_store.get_job(existing.job_id).status == "succeeded"
    root = store.get_source_root(root_id)
    assert root is not None and root.active_generation == 0


def test_admitted_endpoint_allows_its_own_nested_job_enqueue_while_delete_waits():
    """同一导入请求获准后，删除开始等待也不能打断其内部入队。"""
    import threading
    import time

    from app.api.openlist import _admitted_import_endpoint
    from app.catalog import maintenance_guard

    entered = threading.Event()
    proceed = threading.Event()
    outcome: dict[str, object] = {}

    @_admitted_import_endpoint
    def _import_endpoint():
        entered.set()
        proceed.wait(5)
        return job_store.create_job(job_type="discovery_scan", payload={"root_id": "nested"})

    def _run_import():
        try:
            outcome["job"] = _import_endpoint()
        except Exception as exc:  # pragma: no cover
            outcome["error"] = repr(exc)

    def _run_delete():
        with maintenance_guard.hold():
            outcome["delete_entered"] = True

    importer = threading.Thread(target=_run_import)
    deleter = threading.Thread(target=_run_delete)
    importer.start()
    assert entered.wait(5)
    deleter.start()
    time.sleep(0.1)
    assert "delete_entered" not in outcome
    proceed.set()
    importer.join(5)
    deleter.join(5)
    assert "error" not in outcome
    assert outcome["job"].job_type == "discovery_scan"
    assert outcome["delete_entered"] is True
