"""补完 4 验收：mirror 状态检查、scrape 错误传播、SQLite 单写与 library rebuild。"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.db.database import close_connection, get_connection, init_db
from app.import_plan import revision_store
from app.mirror.result import MirrorGenerateResult, MirrorItemResult
from app.pipeline.handlers import handle_mirror_revision, handle_scrape_revision, register_pipeline_handlers


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "pipeline.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    register_pipeline_handlers()
    yield
    close_connection()


def _ensure_unit(unit_id: str, boundary: str = "/动画/作品") -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, closure_generation, current_revision_id, created_at, updated_at
        ) VALUES (?, '', 'root-x', '', ?, 'w', 'discovered', 0, '', ?, ?)
        """,
        (unit_id, boundary, "2026-08-11T00:00:00+08:00", "2026-08-11T00:00:00+08:00"),
    )
    conn.commit()


def _make_items(paths):
    return [
        {
            "id": f"i-{index}", "source": "openlist", "provider_id": "quark",
            "relative_path": path, "real_path": path, "logical_locator": path,
            "resource_type": "video", "action": "generate_strm",
            "work_id": "w1", "work_title": "作品", "series_group": "作品",
            "group_type": "season", "season_number": 1, "episode_number": index + 1,
            "title": "", "target_dir": "", "target_strm_path": f"mirror/作品/S01/{path}",
            "confidence": "high", "needs_review": False, "availability": "available",
        }
        for index, path in enumerate(paths)
    ]




def _set_current(unit_id: str, revision_id: str) -> None:
    """Module 5：rebuild/mirror/scrape 只消费 current revision（execution fence）。"""
    conn = get_connection()
    conn.execute(
        "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
        (revision_id, unit_id),
    )
    conn.commit()

class TestMirrorFailure:
    def test_mirror_failed_does_not_register_artifacts_or_scrape(self, monkeypatch):
        """mirror failed：不登记 artifact、不创建 scrape job。"""
        _ensure_unit("unit-1")
        revision = revision_store.create_revision(
            unit_id="unit-1", source_generation=1, items=_make_items(["a.mkv"]),
            status="confirmed",
        )
        revision_id = revision["revision_id"]
        _set_current("unit-1", revision_id)
        calls = []

        class FakeOrch:
            @staticmethod
            def unit_is_closed(unit_id):
                return True

            @staticmethod
            def enqueue_scrape(revision_id, source):
                calls.append(revision_id)

        monkeypatch.setattr("app.pipeline.orchestrator.unit_is_closed", FakeOrch.unit_is_closed)
        monkeypatch.setattr("app.pipeline.orchestrator.enqueue_scrape", FakeOrch.enqueue_scrape)

        failed_result = MirrorGenerateResult(
            plan_id=revision_id, source="openlist",
            mirror_root="K:/mirror", status="failed",
            generated_count=0, errors=["磁盘不可写"],
        )
        with patch("app.mirror.generator.generate_mirror", return_value=failed_result):
            with pytest.raises(ValueError, match="镜像生成失败"):
                handle_mirror_revision(
                    {"revision_id": revision_id, "unit_id": "unit-1"},
                    progress_callback=lambda *a, **k: None,
                )
        assert calls == []  # 未创建 scrape
        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM artifact_records WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()[0]
        assert count == 0  # 未登记 artifact

    def test_mirror_partial_failed_no_scrape_but_registers_generated(self, monkeypatch, tmp_path):
        """partial_failed：不触发 scrape，但成功项登记 artifact。"""
        _ensure_unit("unit-2")
        revision = revision_store.create_revision(
            unit_id="unit-2", source_generation=1, items=_make_items(["b.mkv"]),
            status="confirmed",
        )
        revision_id = revision["revision_id"]
        _set_current("unit-2", revision_id)
        calls = []
        # Review Fix：artifact 登记要求 strm_path 真实 is_file()
        generated_strm = tmp_path / "a.strm"
        generated_strm.write_text("K:/115动画/作品/01.mkv", encoding="utf-8")

        class FakeOrch:
            @staticmethod
            def unit_is_closed(unit_id):
                return True

            @staticmethod
            def enqueue_scrape(revision_id, source):
                calls.append(revision_id)

        monkeypatch.setattr("app.pipeline.orchestrator.unit_is_closed", FakeOrch.unit_is_closed)
        monkeypatch.setattr("app.pipeline.orchestrator.enqueue_scrape", FakeOrch.enqueue_scrape)
        partial = MirrorGenerateResult(
            plan_id=revision_id, source="openlist",
            mirror_root="K:/mirror", status="partial_failed",
            generated_count=1, failed_count=1,
            items=[
                MirrorItemResult(item_id="i-0", source="openlist", status="generated", strm_path=str(generated_strm)),
                MirrorItemResult(item_id="i-1", source="openlist", status="failed", strm_path="K:/mirror/b.strm"),
            ],
        )
        with patch("app.mirror.generator.generate_mirror", return_value=partial):
            result = handle_mirror_revision(
                {"revision_id": revision_id, "unit_id": "unit-2"},
                progress_callback=lambda *a, **k: None,
            )
        assert result["mirror_status"] == "partial_failed"
        assert calls == []  # partial 不触发 scrape
        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM artifact_records WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()[0]
        assert count == 1  # 成功项已登记


class TestScrapeErrorPropagation:
    def test_scrape_error_raises(self, monkeypatch):
        """scrape 返回 failed/error → 抛错（JobRunner 标 failed，不标 succeeded）。"""
        _ensure_unit("unit-3")
        revision = revision_store.create_revision(
            unit_id="unit-3", source_generation=1, items=_make_items(["c.mkv"]),
            status="confirmed",
        )
        _set_current("unit-3", revision["revision_id"])
        with patch(
            "app.scrape.auto.run_auto_scrape",
            return_value={"status": "failed", "error": "TMDB 限流"},
        ):
            with pytest.raises(RuntimeError, match="TMDB 限流"):
                handle_scrape_revision(
                    {"revision_id": revision["revision_id"], "source": "openlist"},
                    progress_callback=lambda *a, **k: None,
                )
        # 失败记录进 scrape_failures
        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM scrape_failures WHERE error LIKE '%TMDB 限流%'"
        ).fetchone()[0]
        assert count >= 1


class TestScrapeBindingAndLibraryRebuild:
    def test_scrape_success_enqueues_rebuild_without_coarse_binding(self, monkeypatch):
        """scrape 成功：只入队 library rebuild；成功事实必须来自真实 scrape target
        的 effective upsert——handler 不再插入「整 revision 粗粒度 binding」
        （Module 5 收口：record_scrape_outcome 只保留失败路径）。"""
        _ensure_unit("unit-4")
        revision = revision_store.create_revision(
            unit_id="unit-4", source_generation=1, items=_make_items(["d.mkv"]),
            status="confirmed",
        )
        _set_current("unit-4", revision["revision_id"])
        rebuild_jobs = []
        scrape_kwargs = {}

        class FakeOrch:
            @staticmethod
            def enqueue_library_rebuild(*, unit_id=""):
                rebuild_jobs.append(unit_id)
                return "lib-job-1"

        def fake_run_auto_scrape(source, plan_id=None, **kwargs):
            scrape_kwargs.update(kwargs)
            return {"status": "success", "scraped": 1}

        monkeypatch.setattr("app.pipeline.orchestrator.enqueue_library_rebuild", FakeOrch.enqueue_library_rebuild)
        with patch(
            "app.scrape.auto.run_auto_scrape",
            side_effect=fake_run_auto_scrape,
        ):
            result = handle_scrape_revision(
                {"revision_id": revision["revision_id"], "source": "openlist"},
                progress_callback=lambda *a, **k: None,
            )
        assert result["library_rebuild_job"] == "lib-job-1"
        # V3 durable：不直接更新 legacy LibraryIndex（由 rebuild 统一重建投影）
        assert scrape_kwargs.get("publish_library") is False
        # 成功路径不得有粗粒度 binding（真实 target binding 由 execute_scrape 写入）
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM scrape_bindings WHERE revision_id = ?",
            (revision["revision_id"],),
        ).fetchone()
        assert row is None

    def test_library_rebuild_handler_upserts_libraries(self):
        """library_rebuild：从 current revision 重建 media_libraries（Module 5 语义）。"""
        from app.pipeline.library_handler import handle_library_rebuild

        _ensure_unit("unit-5")
        revision = revision_store.create_revision(
            unit_id="unit-5", source_generation=1, items=_make_items(["e.mkv"]),
            status="confirmed",
        )
        # Module 5：rebuild 只消费 media_units.current_revision_id 指向的当前版本
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = 'unit-5'",
            (revision["revision_id"],),
        )
        conn.commit()
        result = handle_library_rebuild(
            {"unit_id": "unit-5"},
            progress_callback=lambda *a, **k: None,
        )
        assert result["status"] == "succeeded"
        conn = get_connection()
        row = conn.execute(
            "SELECT * FROM media_libraries WHERE library_id = 'w1'"
        ).fetchone()
        assert row is not None
        assert row["current_revision_id"] == revision["revision_id"]
