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

    monkeypatch.setattr(db_mod, "_db_path", tmp_path / "lifecycle.db")
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
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
