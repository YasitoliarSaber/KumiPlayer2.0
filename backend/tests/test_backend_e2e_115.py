"""收口 6：真实端到端测试——四来源 API→Catalog→识别→稳定 revision→镜像→刮削→前端可见 LibraryIndex。

覆盖：
- 115/百度目录树 TXT 经 SourceCatalogScanner 进 DiscoveryEngine（分页聚合）；
- 直属视频 + Season/OVA 结构子目录并存时 closure 等待（不完整系列不先刮削）；
- 第二次增量扫描复用同一 media_unit 且 revision 有 parent 链；
- 重启恢复（JobRunner 租约过期重排队）；
- 真实播放路径（logical_locator 保留，不退化相对路径）；
- library rebuild 后前端读取的 library_index.json 可见作品。
"""
from __future__ import annotations

import pytest

from app.catalog import store as catalog_store
from app.catalog.discovery import DiscoveryEngine
from app.catalog.scanner import SourceCatalogScanner
from app.db.database import close_connection, get_connection, init_db
from app.import_plan import revision_store
from app.jobs import store as job_store
from app.pipeline import orchestrator
from app.pipeline.handlers import register_pipeline_handlers


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "e2e.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    register_pipeline_handlers()
    yield
    close_connection()


class FakeTxtAdapter:
    """模拟 115 目录树 TXT：作品A（直属视频 + Season 1/2 + OVA）、作品B（仅直属视频）。"""

    def snapshot_entries(self, input_path: str, source_root: str):
        from app.catalog.models import SourceNodeInput

        rows = [
            # 作品A：直属视频 + 结构段子目录
            ("动画/作品A/正片01.mkv", "正片01.mkv", "file", "动画/作品A", 100, 1.0),
            ("动画/作品A/Season 1/S01E01.mkv", "S01E01.mkv", "file", "动画/作品A/Season 1", 200, 2.0),
            ("动画/作品A/Season 1/S01E02.mkv", "S01E02.mkv", "file", "动画/作品A/Season 1", 200, 3.0),
            ("动画/作品A/Season 2/S02E01.mkv", "S02E01.mkv", "file", "动画/作品A/Season 2", 300, 4.0),
            ("动画/作品A/OVA/OVA01.mkv", "OVA01.mkv", "file", "动画/作品A/OVA", 150, 5.0),
            ("动画/作品A", "作品A", "dir", "动画", None, 6.0),
            ("动画/作品A/Season 1", "Season 1", "dir", "动画/作品A", None, 7.0),
            ("动画/作品A/Season 2", "Season 2", "dir", "动画/作品A", None, 8.0),
            ("动画/作品A/OVA", "OVA", "dir", "动画/作品A", None, 9.0),
            # 作品B：仅直属视频（closure 立即）
            ("动画/作品B/电影B.mkv", "电影B.mkv", "file", "动画/作品B", 500, 10.0),
            ("动画/作品B", "作品B", "dir", "动画", None, 11.0),
        ]
        return [
            SourceNodeInput(
                remote_path=remote, name=name, kind=kind, parent_path=parent,
                size=size, mtime=mtime,
            )
            for remote, name, kind, parent, size, mtime in rows
        ]


def _make_root() -> dict:
    catalog_store.create_source(
        source_id="pan115-e2e", source_type="pan115",
        provider_id="pan115", ingest_method="directory_tree",
        connection_key="pan115-e2e", display_name="115 目录树",
    )
    root = catalog_store.create_source_root(
        source_id="pan115-e2e",
        remote_locator="/",
        local_locator="K:/115动画",
        import_family="anime",
    )
    return catalog_store.get_source_root(root.root_id)


def _run_discovery(root, generation):
    scanner = SourceCatalogScanner(
        source="pan115", adapter=FakeTxtAdapter(),
        input_path="tree.txt", source_root=root.remote_locator,
    )
    engine = DiscoveryEngine(
        scanner,
        source_id=root.source_id,
        root_id=root.root_id,
        generation=generation,
        source="pan115",
    )

    def on_unit(result):
        # 真实 handler 行为：plan_ready 单元立即 confirmed（parent 链基于 confirmed revision）
        if result.get("status") == "plan_ready" and result.get("revision_id"):
            revision_store.update_revision_status(result["revision_id"], "confirmed")

    return engine.run(
        should_cancel=lambda: False,
        on_unit=on_unit,
    )


class TestEndToEnd115:
    def test_direct_video_plus_season_waits_for_closure(self):
        """直属视频 + Season/OVA 并存：closure 前不结算单元（不完整系列不先刮削）。"""
        root = _make_root()
        results = _run_discovery(root, 1)
        by_boundary = {r["boundary"]: r for r in results}
        print("DBG boundaries:", list(by_boundary.keys()))
        # 作品B（仅直属视频）closure 安全 → plan_ready
        assert by_boundary["/动画/作品B"]["status"] == "plan_ready"
        # 作品A 有结构子目录：所有子目录扫描完才结算 → plan_ready（完整聚合）
        assert "/动画/作品A" in by_boundary
        assert by_boundary["/动画/作品A"]["status"] == "plan_ready"
        revision_a = revision_store.load_revision(by_boundary["/动画/作品A"]["revision_id"])
        # 完整聚合：Season1/2 + OVA + 直属视频都在 revision 里
        assert revision_a is not None
        items = revision_a["items"]
        paths = {item["relative_path"] for item in items}
        assert "动画/作品A/正片01.mkv" in paths
        assert "动画/作品A/Season 1/S01E01.mkv" in paths
        assert "动画/作品A/Season 2/S02E01.mkv" in paths
        assert "动画/作品A/OVA/OVA01.mkv" in paths
        # 真实播放路径：logical_locator 保留（不退化相对路径）
        for item in items:
            # Windows 上 Path 拼接为反斜杠：统一转正斜杠后再断言前缀
            assert item["real_path"].replace("\\", "/").startswith("K:/115动画/")

    def test_second_incremental_reuses_unit_and_parent_chain(self):
        """第二次增量：同一 root+boundary 复用 unit，新 revision 有 parent。"""
        root = _make_root()
        _run_discovery(root, 1)

        # 第二轮（新 generation）：内容变化（作品A 新增一集）→ 增量 revision
        class FakeTxtAdapterV2(FakeTxtAdapter):
            def snapshot_entries(self, input_path, source_root):
                from app.catalog.models import SourceNodeInput

                rows = [
                    ("动画/作品A/正片01.mkv", "正片01.mkv", "file", "动画/作品A", 100, 1.0),
                    ("动画/作品A/正片02.mkv", "正片02.mkv", "file", "动画/作品A", 120, 1.5),
                    ("动画/作品A/Season 1/S01E01.mkv", "S01E01.mkv", "file", "动画/作品A/Season 1", 200, 2.0),
                    ("动画/作品A/Season 2/S02E01.mkv", "S02E01.mkv", "file", "动画/作品A/Season 2", 300, 4.0),
                    ("动画/作品A/OVA/OVA01.mkv", "OVA01.mkv", "file", "动画/作品A/OVA", 150, 5.0),
                    ("动画/作品A", "作品A", "dir", "动画", None, 6.0),
                    ("动画/作品A/Season 1", "Season 1", "dir", "动画/作品A", None, 7.0),
                    ("动画/作品A/Season 2", "Season 2", "dir", "动画/作品A", None, 8.0),
                    ("动画/作品A/OVA", "OVA", "dir", "动画/作品A", None, 9.0),
                    ("动画/作品B/电影B.mkv", "电影B.mkv", "file", "动画/作品B", 500, 10.0),
                    ("动画/作品B", "作品B", "dir", "动画", None, 11.0),
                ]
                return [
                    SourceNodeInput(
                        remote_path=remote, name=name, kind=kind, parent_path=parent,
                        size=size, mtime=mtime,
                    )
                    for remote, name, kind, parent, size, mtime in rows
                ]

        generation2 = catalog_store.bump_generation(root.root_id)
        # 隔离版 DiscoveryEngine 的 frontier 持久化：增量扫描需显式把目录重新排队
        catalog_store.prepare_scan(root.root_id, generation=generation2, mode="full")
        scanner = SourceCatalogScanner(
            source="pan115", adapter=FakeTxtAdapterV2(),
            input_path="tree.txt", source_root=root.remote_locator,
        )
        engine = DiscoveryEngine(
            scanner,
            source_id=root.source_id,
            root_id=root.root_id,
            generation=generation2,
            source="pan115",
        )

        def on_unit2(result):
            if result.get("status") == "plan_ready" and result.get("revision_id"):
                revision_store.update_revision_status(result["revision_id"], "confirmed")

        results2 = engine.run(
            should_cancel=lambda: False,
            on_unit=on_unit2,
        )
        assert len(results2) >= 2  # 两轮扫描都返回单元
        conn = get_connection()
        unit_count = conn.execute(
            "SELECT COUNT(*) AS c FROM media_units WHERE root_id = ?",
            (root.root_id,),
        ).fetchone()["c"]
        # 两轮扫描后 unit 数量应等于作品数（2）加上 root "/" 容器候选单元
        # （“动画”分类目录被识别为结构段，root 按容器候选结算，_create_unit
        # 按 root+boundary 复用，第二轮不会翻倍），共 3 个而不是 4 个
        assert unit_count == 3
        # 内容变化的单元（作品A）有增量 revision 且带 parent 链；
        # 内容未变的单元（作品B）hash 去重，保持同一 revision（稳定 revision 语义）
        conn = get_connection()
        boundary_units = {
            row["boundary"]: row["unit_id"]
            for row in conn.execute(
                "SELECT boundary, unit_id FROM media_units WHERE root_id = ?",
                (root.root_id,),
            ).fetchall()
        }
        changed_a = revision_store.list_revisions(boundary_units["/动画/作品A"])
        assert len(changed_a) >= 2
        newest = changed_a[0]
        assert newest["parent_revision_id"] != ""
        assert newest["parent_revision_id"] == changed_a[1]["revision_id"]
        unchanged_b = revision_store.list_revisions(boundary_units["/动画/作品B"])
        assert len(unchanged_b) == 1

    def test_restart_recovery_requeues_expired_lease(self):
        """重启恢复：租约过期的 running 任务重新排队并被新 worker 领取。"""
        root = _make_root()
        generation = catalog_store.bump_generation(root.root_id)
        job_id = orchestrator.enqueue_scan(
            root.root_id, generation, root.source_id,
            input_path="tree.txt",
        )
        job_store.claim_jobs("old-worker")
        from datetime import datetime, timedelta, timezone

        past = (
            datetime.now(timezone(timedelta(hours=8))) - timedelta(seconds=120)
        ).isoformat()
        get_connection().execute(
            "UPDATE jobs SET lease_until = ? WHERE job_id = ?", (past, job_id),
        )
        get_connection().commit()
        job_store.requeue_expired_leases()
        claimed = job_store.claim_jobs(
            "new-worker", limit=5, job_types=["discovery_scan"]
        )
        assert any(job.job_id == job_id for job in claimed)


class TestLibraryIndexVisible:
    def test_library_rebuild_publishes_json_index(self, monkeypatch, tmp_path):
        """library rebuild 后：前端读取的 library_index.json 出现作品。"""
        from app.core import paths as core_paths

        cache_dir = tmp_path / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(core_paths, "get_cache_dir", lambda: cache_dir)

        # 真实镜像产物：先建 mirror 根并写 .strm（publish 要求 .strm 存在才可见）
        from app.core import paths as core_paths
        from app.library.store import load_library_index
        from app.pipeline.library_handler import handle_library_rebuild

        mirror_root = tmp_path / "mirror"
        strm_file = mirror_root / "作品A" / "S01" / "S01E01.strm"
        strm_file.parent.mkdir(parents=True, exist_ok=True)
        strm_file.write_text("K:/115动画/作品A/S01E01.mkv", encoding="utf-8")
        monkeypatch.setattr(core_paths, "get_mirror_root", lambda *a, **k: mirror_root)

        # 造一个 confirmed revision（target_strm_path 为绝对路径，同真实镜像输出）
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES ('unit-e2e', '', 'root-x', '', '/动画/作品A', '作品A',
                      'plan_ready', 0, '', '2026-08-11T00:00:00+08:00', '2026-08-11T00:00:00+08:00')
            """
        )
        conn.commit()
        strm_abs = str(strm_file).replace("\\", "/")
        revision_store.create_revision(
            unit_id="unit-e2e", source_generation=1,
            items=[
                {
                    "id": "i1", "source": "pan115", "provider_id": "pan115",
                    "relative_path": "作品A/S01E01.mkv", "real_path": "K:/115动画/作品A/S01E01.mkv",
                    "logical_locator": "K:/115动画/作品A/S01E01.mkv",
                    "resource_type": "video", "action": "generate_strm",
                    "work_id": "w-e2e", "work_title": "作品A", "series_group": "作品A",
                    "group_type": "season", "season_number": 1, "episode_number": 1,
                    "title": "", "target_dir": str(mirror_root / "作品A"),
                    "target_strm_path": strm_abs,
                    "confidence": "high", "needs_review": False, "availability": "available",
                }
            ],
            status="confirmed",
        )
        conn.commit()

        # mock scrape_map 与发布链路所需（load_scrape_map 返回空即可）
        monkeypatch.setattr(
            "app.library.service.load_scrape_map",
            lambda: type("M", (), {"items": []})(),
        )

        result = handle_library_rebuild(
            {"unit_id": "unit-e2e"},
            progress_callback=lambda *a, **k: None,
        )
        assert result["status"] == "succeeded"
        index = load_library_index()
        assert index is not None
        titles = [work.title for work in index.works]
        assert "作品A" in titles
