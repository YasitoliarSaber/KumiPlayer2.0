"""Module 5 Checkpoint 1 验收：SQLite Projection Facts。

规划员必测：
- current pointer：rev1/rev2 均 confirmed，unit.current_revision_id=rev2 →
  list_current_revisions 只能得到 rev2（fail closed，不猜最新、不 fallback）；
- scrape binding：同 target 连续 upsert 2 次 → DB 只有 1 行（binding_id = scrape_target_id）；
- cross revision：rev1 已绑定 TMDB，rev2 相同语义 target → 复用同一 binding，
  revision_id 更新到 rev2；
- artifact：rev1 写 kind=strm,path=X，rev2 重写 X → 仍 1 行，revision_id == rev2；
- migration：旧 v3 scrape_bindings 无 metadata_json → init_db 自动补列，user_version 仍 3。
"""

import json

from app.db.database import get_connection
from app.import_plan import revision_store


def _ensure_unit(unit_id: str) -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, closure_generation, current_revision_id, created_at, updated_at
        ) VALUES (?, '', 'root-x', '', '/动画', 'w', 'discovered', 0, '', ?, ?)
        """,
        (unit_id, revision_store.now_iso(), revision_store.now_iso()),
    )
    conn.commit()


def _items(paths):
    return [
        {
            "id": f"i-{index}",
            "source": "openlist",
            "provider_id": "quark",
            "relative_path": path,
            "real_path": path,
            "resource_type": "video",
            "action": "generate_strm",
            "work_id": "w1",
            "work_title": "作品",
            "series_group": "作品",
            "group_type": "season",
            "season_number": 1,
            "episode_number": index + 1,
            "title": "",
            "target_dir": "",
            "target_strm_path": "",
            "confidence": "high",
            "needs_review": False,
            "availability": "available",
        }
        for index, path in enumerate(paths)
    ]


class TestCurrentRevisionTruth:
    def test_list_current_revisions_only_returns_current_pointer(self):
        """rev1/rev2 均 confirmed 且 current=rev2 → 只投影 rev2。"""
        _ensure_unit("u1")
        rev1 = revision_store.create_revision(
            unit_id="u1", source_generation=1, items=_items(["a.mkv"]), status="confirmed",
        )
        rev2 = revision_store.create_revision(
            unit_id="u1", source_generation=2, items=_items(["a.mkv", "b.mkv"]),
            parent_revision_id=rev1["revision_id"], status="confirmed",
        )
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = 'u1'",
            (rev2["revision_id"],),
        )
        conn.commit()

        current = revision_store.list_current_revisions()
        assert [r["revision_id"] for r in current] == [rev2["revision_id"]]

        # source 过滤
        assert [r["revision_id"] for r in revision_store.list_current_revisions(source="openlist")] == [
            rev2["revision_id"]
        ]
        assert revision_store.list_current_revisions(source="baidu") == []

        # plan 视图
        plans = revision_store.list_current_plans()
        assert [p.plan_id for p in plans] == [rev2["revision_id"]]

    def test_current_pointer_fails_closed(self):
        """current 指针悬空 / 指向 draft / 为空 → 跳过，绝不 fallback 其他 revision。"""
        _ensure_unit("u2")
        rev1 = revision_store.create_revision(
            unit_id="u2", source_generation=1, items=_items(["a.mkv"]), status="confirmed",
        )
        conn = get_connection()
        # 悬空指针
        conn.execute(
            "UPDATE media_units SET current_revision_id = 'ghost-revision' WHERE unit_id = 'u2'"
        )
        conn.commit()
        assert revision_store.list_current_revisions() == []
        # 指向 draft（新建一个 draft，不把 rev1 顶上）
        draft = revision_store.create_revision(
            unit_id="u2", source_generation=2, items=_items(["a.mkv", "c.mkv"]),
            parent_revision_id=rev1["revision_id"], status="draft",
        )
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = 'u2'",
            (draft["revision_id"],),
        )
        conn.commit()
        assert revision_store.list_current_revisions() == []
        # current 为空
        conn.execute("UPDATE media_units SET current_revision_id = '' WHERE unit_id = 'u2'")
        conn.commit()
        assert revision_store.list_current_revisions() == []


class TestScrapeBindingTruth:
    def _make_revisions(self):
        from app.scrape.models import ScrapeMapItem

        _ensure_unit("ub1")
        rev1 = revision_store.create_revision(
            unit_id="ub1", source_generation=1, items=_items(["a.mkv"]), status="confirmed",
        )
        rev2 = revision_store.create_revision(
            unit_id="ub1", source_generation=2, items=_items(["a.mkv", "b.mkv"]),
            parent_revision_id=rev1["revision_id"], status="confirmed",
        )
        item = ScrapeMapItem(
            scrape_target_id="target-A",
            work_id="w1",
            source="openlist",
            import_plan_id=rev1["revision_id"],
            tmdb_id=123,
            tmdb_type="tv",
            scrape_title="作品",
            nfo_path="/mirror/作品/tvshow.nfo",
        )
        return rev1, rev2, item

    def test_same_target_upsert_twice_keeps_single_row(self):
        """binding_id = scrape_target_id：同 target 连续 upsert → 只有 1 行。"""
        from app.scrape.effective_store import upsert_effective_scrape_map_item

        _rev1, _rev2, item = self._make_revisions()
        upsert_effective_scrape_map_item(item)
        upsert_effective_scrape_map_item(item)
        rows = get_connection().execute("SELECT * FROM scrape_bindings").fetchall()
        assert len(rows) == 1
        assert rows[0]["binding_id"] == "target-A"
        assert rows[0]["tmdb_id"] == 123
        # metadata_json 完整还原 ScrapeMapItem
        raw = json.loads(rows[0]["metadata_json"])
        assert raw["scrape_title"] == "作品"
        assert raw["nfo_path"] == "/mirror/作品/tvshow.nfo"

    def test_cross_revision_reuses_same_binding(self):
        """rev2 相同语义 target → 复用同一 binding，revision_id 更新到 rev2。"""
        from dataclasses import replace

        from app.scrape.effective_store import upsert_effective_scrape_map_item

        rev1, rev2, item = self._make_revisions()
        upsert_effective_scrape_map_item(item)
        upsert_effective_scrape_map_item(replace(item, import_plan_id=rev2["revision_id"]))
        rows = get_connection().execute("SELECT * FROM scrape_bindings").fetchall()
        assert len(rows) == 1
        assert rows[0]["binding_id"] == "target-A"
        assert rows[0]["revision_id"] == rev2["revision_id"]
        assert rows[0]["tmdb_id"] == 123

    def test_legacy_plan_still_routes_to_json_scrape_map(self, tmp_path, monkeypatch):
        """legacy（非 V3 revision）仍走 scrape_map.json 兼容路径。"""
        from app.scrape.effective_store import (
            load_effective_scrape_map,
            upsert_effective_scrape_map_item,
        )
        from app.scrape.models import ScrapeMapItem

        item = ScrapeMapItem(
            scrape_target_id="legacy-A", work_id="w1", source="baidu",
            import_plan_id="legacy-plan-1", tmdb_id=1, tmdb_type="tv",
        )
        upsert_effective_scrape_map_item(item)
        loaded = load_effective_scrape_map("legacy-plan-1")
        ids = [i.scrape_target_id for i in loaded.items]
        assert "legacy-A" in ids


class TestArtifactTruth:
    def test_artifact_rewrite_updates_attribution(self):
        """同 (kind, path) 被新 revision 重写 → 仍 1 行且归属切到新 revision。"""
        from app.pipeline.artifacts import upsert_artifact

        upsert_artifact(kind="strm", path="/mirror/X.strm", revision_id="rev1", work_id="w1")
        upsert_artifact(kind="strm", path="/mirror/X.strm", revision_id="rev2", work_id="w1")
        rows = get_connection().execute(
            "SELECT * FROM artifact_records WHERE kind = 'strm' AND path = '/mirror/X.strm'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["revision_id"] == "rev2"

    def test_different_kind_same_path_are_distinct(self):
        from app.pipeline.artifacts import upsert_artifact

        upsert_artifact(kind="strm", path="/mirror/X", revision_id="rev1", work_id="w1")
        upsert_artifact(kind="nfo", path="/mirror/X", revision_id="rev1", work_id="w1")
        rows = get_connection().execute("SELECT * FROM artifact_records WHERE path = '/mirror/X'").fetchall()
        assert len(rows) == 2


class TestScrapeBindingsMigration:
    def test_old_v3_db_gets_metadata_json_column_without_bump(self, tmp_path, monkeypatch):
        """旧 v3 scrape_bindings（无 metadata_json）→ init_db 自动补列，user_version 仍 3。"""
        import sqlite3 as sqlite

        old_db = tmp_path / "old_v3.db"
        conn = sqlite.connect(str(old_db))
        conn.execute("PRAGMA user_version = 3")
        conn.execute(
            """
            CREATE TABLE scrape_bindings (
                binding_id TEXT PRIMARY KEY,
                revision_id TEXT NOT NULL,
                work_id TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                provider_id TEXT NOT NULL DEFAULT '',
                tmdb_id INTEGER,
                tmdb_type TEXT DEFAULT '',
                bangumi_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                nfo_path TEXT DEFAULT '',
                poster_path TEXT DEFAULT '',
                fanart_path TEXT DEFAULT '',
                clearlogo_path TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE TABLE import_revision_items (revision_id TEXT NOT NULL)")
        conn.commit()
        conn.close()

        monkeypatch.setattr("app.db.database._db_path", old_db)
        import app.db.database as db_mod

        if hasattr(db_mod._local, "connection"):
            db_mod._local.connection = None
        db_mod.init_db()

        check = db_mod.get_connection()
        cols = [row[1] for row in check.execute("PRAGMA table_info(scrape_bindings)").fetchall()]
        assert "metadata_json" in cols
        version = check.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3
        db_mod.close_connection()


class TestLegacyDetachment:
    """Checkpoint 2 十七节：把 legacy 直接炸掉的回归。

    V3 durable mirror 绝不写 legacy ImportPlan JSON；V3 binding 不依赖
    JSON ScrapeMap；legacy 默认路径保持不变。
    """

    def _make_confirmed_plan(self, plan_id: str):
        from app.import_plan.models import ImportPlan, ImportPlanItem

        item = ImportPlanItem(
            id="i-0", plan_id=plan_id, raw_file_id="raw-i-0", source="pan115",
            relative_path="动画/作品.2024/i-0.mkv",
            real_path=r"H:\media\动画\作品.2024\i-0.mkv",
            resource_type="video", action="generate_strm",
            work_title="作品", year=2024, media_type="tv", group_type="season",
            card_type="main_series", season_number=1, episode_number=1,
            title="", confidence="high",
        )
        return ImportPlan(
            plan_id=plan_id, source="pan115", source_snapshot_id="snap",
            status="confirmed", items=[item],
        )

    def test_v3_mirror_never_writes_legacy_json(self, tmp_path, monkeypatch):
        """persist_plan=False：save_import_plan 被炸掉，durable mirror 仍成功。"""
        from app.mirror.generator import generate_mirror

        plan = self._make_confirmed_plan("v3-mirror-plan")

        def _bomb(*args, **kwargs):
            raise AssertionError("V3 durable mirror 不得写 legacy ImportPlan JSON")

        monkeypatch.setattr("app.mirror.generator.save_import_plan", _bomb)
        result = generate_mirror(plan, str(tmp_path / "mirror"), persist_plan=False)
        assert result.status == "success"
        assert (tmp_path / "mirror").exists()

    def test_legacy_mirror_still_persists_json_by_default(self, tmp_path, monkeypatch):
        """默认 persist_plan=True：legacy mirror 仍写 JSON（未被 V3 收口误杀）。"""
        from app.mirror.generator import generate_mirror

        plan = self._make_confirmed_plan("legacy-mirror-plan")
        calls = []

        def _spy(*args, **kwargs):
            calls.append(args)

        monkeypatch.setattr("app.mirror.generator.save_import_plan", _spy)
        generate_mirror(plan, str(tmp_path / "mirror"))
        assert len(calls) == 1

    def test_v3_binding_works_without_json_scrape_map(self, monkeypatch):
        """load/save_scrape_map 被炸掉，V3 effective upsert/读取仍走 SQLite。"""
        from app.scrape.effective_store import (
            load_effective_scrape_map,
            upsert_effective_scrape_map_item,
        )
        from app.scrape.models import ScrapeMapItem

        _ensure_unit("u-json-free")
        revision = revision_store.create_revision(
            unit_id="u-json-free", source_generation=1, items=_items(["x.mkv"]),
            status="confirmed",
        )

        def _bomb(*args, **kwargs):
            raise AssertionError("V3 路径不得触碰 JSON ScrapeMap")

        monkeypatch.setattr("app.scrape.store.load_scrape_map", _bomb)
        monkeypatch.setattr("app.scrape.store.save_scrape_map", _bomb)
        item = ScrapeMapItem(
            scrape_target_id="t-json-free", work_id="w1", source="openlist",
            import_plan_id=revision["revision_id"], tmdb_id=1, tmdb_type="tv",
        )
        upsert_effective_scrape_map_item(item)
        loaded = load_effective_scrape_map(revision["revision_id"])
        assert [i.scrape_target_id for i in loaded.items] == ["t-json-free"]


class TestLibraryProjection:
    """Module 5 Checkpoint 3 验收：LibraryIndex 是 SQLite current state 的投影。"""

    def _ensure_source_root(
        self,
        root_id="root-x",
        remote_locator="/动画",
        import_family="anime",
        import_scope="seasonal",
    ):
        from app.catalog import store as catalog_store

        conn = get_connection()
        catalog_store.create_source(source_id="ol", source_type="openlist", provider_id="quark")
        conn.execute(
            """
            INSERT OR IGNORE INTO source_roots (
                root_id, source_id, remote_locator, normalized_locator, local_locator,
                import_family, import_scope, scan_policy, active_generation, created_at, updated_at
            ) VALUES (?, 'ol', ?, ?, '', ?, ?, 'standard', 1, '', '')
            """,
            (root_id, remote_locator, remote_locator, import_family, import_scope),
        )
        conn.commit()

    def _make_current_unit(self, unit_id, root_id, items, gen=1, status="confirmed"):
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, '', ?, '', '/动画', 'w', 'discovered', 0, '', ?, ?)
            """,
            (unit_id, root_id, revision_store.now_iso(), revision_store.now_iso()),
        )
        conn.commit()
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=gen, items=items, status=status,
        )
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (revision["revision_id"], unit_id),
        )
        conn.commit()
        return revision

    def _rebuild(self):
        from app.pipeline.library_handler import handle_library_rebuild

        return handle_library_rebuild({}, progress_callback=lambda *a, **k: None)

    def test_media_libraries_root_id_comes_from_source_root(self, tmp_path):
        """十八.2：media_libraries.root_id == SourceRoot.root_id，字段不再 hard-code。"""
        self._ensure_source_root(root_id="root-x", remote_locator="/动画")
        revision = self._make_current_unit("u-r1", "root-x", _items(["a.mkv"]))
        result = self._rebuild()
        assert result["status"] == "succeeded"
        row = get_connection().execute(
            "SELECT * FROM media_libraries WHERE current_revision_id = ?",
            (revision["revision_id"],),
        ).fetchone()
        assert row is not None
        assert row["root_id"] == "root-x"
        assert row["remote_locator"] == "/动画"
        assert row["import_family"] == "anime"
        assert row["import_scope"] == "seasonal"

    def _make_strm_item(self, item_id, episode_number, strm_path):
        from pathlib import Path

        item = _items(["ep.mkv"])[0]
        item.update(
            id=item_id,
            episode_number=episode_number,
            target_dir=str(Path(strm_path).parent),
            target_strm_path=str(strm_path),
        )
        return item

    def test_superseded_revision_not_projected(self, tmp_path):
        """二十五：current=rev2 时 LibraryIndex 只含 rev2，无 rev1 残留。"""
        from pathlib import Path as P

        from app.library.store import load_library_index
        from app.pipeline.artifacts import upsert_artifact

        self._ensure_source_root()
        strm1 = tmp_path / "mirror" / "ep1.strm"
        strm2 = tmp_path / "mirror" / "ep2.strm"
        strm1.parent.mkdir(parents=True, exist_ok=True)
        strm1.write_text(r"H:\media\ep1.mkv", encoding="utf-8")
        strm2.write_text(r"H:\media\ep2.mkv", encoding="utf-8")

        rev1 = self._make_current_unit(
            "u-sup", "root-x", [self._make_strm_item("i-0", 1, strm1)], gen=1,
        )
        upsert_artifact(kind="strm", path=str(strm1), revision_id=rev1["revision_id"], work_id="w1")
        rev2 = self._make_current_unit(
            "u-sup", "root-x", [self._make_strm_item("i-1", 2, strm2)], gen=2,
        )
        upsert_artifact(kind="strm", path=str(strm2), revision_id=rev2["revision_id"], work_id="w1")

        self._rebuild()
        index = load_library_index()
        assert len(index.works) == 1
        work = index.works[0]
        # 只有 rev2 的 ep2；rev1 的 ep1 是 superseded，不得残留
        assert [ep.episode_number for ep in work.episodes] == [2]

    def test_missing_strm_artifact_not_projected_as_playable(self, tmp_path):
        """二十六：artifact 无成功 STRM（计划路径存在但物化缺失）不得幽灵可播放。"""
        from pathlib import Path as P

        from app.library.store import load_library_index

        self._ensure_source_root()
        ghost = tmp_path / "mirror" / "ghost.strm"
        ghost.parent.mkdir(parents=True, exist_ok=True)
        self._make_current_unit(
            "u-ghost", "root-x", [self._make_strm_item("i-0", 1, ghost)],
        )
        # 不登记 artifact：计划有 target_strm_path，但物化投影不存在
        self._rebuild()
        index = load_library_index()
        if index.works:
            for work in index.works:
                playable = [
                    ep for ep in work.episodes
                    if getattr(ep, "strm_path", "") and P(str(ep.strm_path)).exists()
                ]
                assert playable == []

    def test_rebuild_survives_deleted_library_index_json(self, tmp_path):
        """二十四：删掉 library_index.json 后仍能从 SQLite current state 重建。"""
        from pathlib import Path as P

        from app.library.store import load_library_index

        self._ensure_source_root()
        strm = tmp_path / "mirror" / "ep1.strm"
        strm.parent.mkdir(parents=True, exist_ok=True)
        strm.write_text(r"H:\media\ep1.mkv", encoding="utf-8")
        revision = self._make_current_unit(
            "u-rebuild", "root-x", [self._make_strm_item("i-0", 1, strm)],
        )
        from app.pipeline.artifacts import upsert_artifact

        upsert_artifact(kind="strm", path=str(strm), revision_id=revision["revision_id"], work_id="w1")
        self._rebuild()
        first = load_library_index()
        assert len(first.works) == 1

        # 删除投影 JSON → 再次重建必须恢复（且不依赖 legacy latest/scrape_map JSON）
        index_path = P(get_connection().execute("SELECT 1").fetchone() and "" or "")
        from app.core.paths import get_data_dir

        for candidate in (
            get_data_dir() / "library" / "library_index.json",
            get_data_dir() / "library_index.json",
        ):
            if candidate.exists():
                candidate.unlink()
        self._rebuild()
        second = load_library_index()
        assert len(second.works) == 1


class TestLibraryRebuildCoalescing:
    """Module 5 Checkpoint 4 验收：library:global 合并入队语义。"""

    def _count_queued(self):
        return get_connection().execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = 'library_rebuild' "
            "AND resource_key = 'library:global' AND status = 'queued'"
        ).fetchone()[0]

    def _count_running(self):
        return get_connection().execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = 'library_rebuild' "
            "AND resource_key = 'library:global' AND status = 'running'"
        ).fetchone()[0]

    def test_consecutive_enqueues_share_one_queued_job(self):
        """连续 enqueue ×3、无 running → 同 job_id，queued == 1。"""
        from app.jobs.store import enqueue_coalesced_job

        first, created_first = enqueue_coalesced_job(
            job_type="library_rebuild", resource_key="library:global",
        )
        second, created_second = enqueue_coalesced_job(
            job_type="library_rebuild", resource_key="library:global",
        )
        third, created_third = enqueue_coalesced_job(
            job_type="library_rebuild", resource_key="library:global",
        )
        assert created_first is True
        assert created_second is False
        assert created_third is False
        assert first.job_id == second.job_id == third.job_id
        assert self._count_queued() == 1

    def test_running_job_gets_single_trailing_queued(self):
        """running A + 连续 enqueue ×10 → 新建 trailing B，之后全部复用 B。"""
        from app.jobs.store import enqueue_coalesced_job

        first, _ = enqueue_coalesced_job(
            job_type="library_rebuild", resource_key="library:global",
        )
        conn = get_connection()
        conn.execute("UPDATE jobs SET status = 'running' WHERE job_id = ?", (first.job_id,))
        conn.commit()

        created_flags = []
        last_job = None
        for _ in range(10):
            job, created = enqueue_coalesced_job(
                job_type="library_rebuild", resource_key="library:global",
            )
            created_flags.append(created)
            last_job = job
        assert created_flags[0] is True  # 第一次创建 trailing B
        assert all(flag is False for flag in created_flags[1:])  # 后续全部复用 B
        assert last_job.job_id != first.job_id
        assert self._count_running() == 1
        assert self._count_queued() == 1

    def test_terminal_history_job_yields_new_job(self):
        """A succeeded 之后新变化 → 新 job C（C != A），不复用终态 job。"""
        from app.jobs.store import enqueue_coalesced_job

        first, _ = enqueue_coalesced_job(
            job_type="library_rebuild", resource_key="library:global",
        )
        conn = get_connection()
        conn.execute("UPDATE jobs SET status = 'succeeded' WHERE job_id = ?", (first.job_id,))
        conn.commit()

        second, created = enqueue_coalesced_job(
            job_type="library_rebuild", resource_key="library:global",
        )
        assert created is True
        assert second.job_id != first.job_id
        assert self._count_queued() == 1
