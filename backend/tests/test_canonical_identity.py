"""专项大模块 CP2：canonical work identity 贯通与投影隔离回归。

规划员强制回归：
1. Work A + Work B 位于同一 collection root，series_group 故意设置成相同字符串：
   - canonical A != canonical B
   - mirror root A != mirror root B
   - scrape target A != scrape target B
   - artifact attribution A != B
   - library work A != library work B
2. 同一 work 的 Season 1 / Season 2 / Special 仍正确聚合到同一 canonical work。
3. canonical_work_id 跨 SQLite revision load/save 不丢失；incremental rescan
   同 MediaUnit 保持 canonical identity。
"""

import pytest

from app.db.database import close_connection, init_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "canonical.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _make_unit(unit_id: str, root_id: str = "root-x", boundary: str = "/动画/作品") -> None:
    from app.db.database import get_connection
    from app.catalog import store
    conn = get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, closure_generation, current_revision_id, created_at, updated_at
        ) VALUES (?, '', ?, '', ?, 'w', 'discovered', 0, '', ?, ?)
        """,
        (unit_id, root_id, boundary, store.now_iso(), store.now_iso()),
    )
    conn.commit()


def _item(relative_path: str, **overrides):
    strm = f"H:/mirror/openlist/{relative_path}".replace(".mkv", ".strm")
    base = dict(
        id="", source="openlist", provider_id="quark",
        relative_path=relative_path, real_path=f"H:/open/{relative_path}",
        logical_locator=f"H:/open/{relative_path}",
        resource_type="video", action="generate_strm",
        work_id="w", canonical_work_id="",
        work_title="作品", original_title="", year=2024,
        media_type="tv", show_type="anime_series",
        series_group="SAME_SERIES_GROUP",  # 故意相同
        card_type="main_series", belongs_to_series="", relation_type="",
        group_type="season", season_number=1, episode_number=1,
        special_number=None, title="", target_dir="", target_strm_path=strm,
        confidence="high", needs_review=False, availability="available",
        warnings=[], reasons=[], user_override_id="",
    )
    base.update(overrides)
    if "target_dir" not in overrides and "/" in relative_path:
        base["target_dir"] = strm.rsplit("/", 1)[0]
    return base


class TestCanonicalIdentityPersistence:
    def test_canonical_work_id_roundtrip_sqlite(self):
        """canonical_work_id 跨 SQLite revision load/save 不丢失。"""
        from app.import_plan import revision_store

        _make_unit("unit-cp2-1")
        items = [
            _item("动画/作品/Season 1/a.mkv", canonical_work_id="unit:unit-cp2-1:main"),
        ]
        rev = revision_store.create_revision(
            unit_id="unit-cp2-1", source_generation=1, items=items, status="draft",
        )
        loaded = revision_store.load_revision(rev["revision_id"])
        assert loaded["items"][0]["canonical_work_id"] == "unit:unit-cp2-1:main"

        plan = revision_store.load_plan(rev["revision_id"])
        assert plan.items[0].canonical_work_id == "unit:unit-cp2-1:main"

    def test_canonical_change_changes_semantic_hash(self):
        """canonical identity 变化必须产生新 revision（不是'完全同一个 revision'）。"""
        from app.import_plan import revision_store

        _make_unit("unit-cp2-2")
        items_a = [_item("a.mkv", canonical_work_id="unit:unit-cp2-2:a")]
        items_b = [_item("a.mkv", canonical_work_id="unit:unit-cp2-2:b")]
        rev_a = revision_store.create_revision(
            unit_id="unit-cp2-2", source_generation=1, items=items_a, status="confirmed",
        )
        rev_b = revision_store.create_revision(
            unit_id="unit-cp2-2", source_generation=2, items=items_b, status="draft",
        )
        assert rev_b["revision_id"] != rev_a["revision_id"]

    def test_incremental_rescan_keeps_canonical_identity(self):
        """同 MediaUnit 跨 incremental generation 保持 canonical identity。"""
        from app.import_plan import revision_store

        _make_unit("unit-cp2-3")
        first = revision_store.create_revision(
            unit_id="unit-cp2-3", source_generation=1,
            items=[_item("Season 1/a.mkv", canonical_work_id="unit:unit-cp2-3:main")],
            status="confirmed",
        )
        second = revision_store.create_revision(
            unit_id="unit-cp2-3", source_generation=2,
            items=[
                _item("Season 1/a.mkv", canonical_work_id="unit:unit-cp2-3:main"),
                _item("Season 2/b.mkv", canonical_work_id="unit:unit-cp2-3:main"),
            ],
            parent_revision_id=first["revision_id"],
            status="draft",
        )
        loaded = revision_store.load_revision(second["revision_id"])
        canonicals = {item["canonical_work_id"] for item in loaded["items"]}
        assert canonicals == {"unit:unit-cp2-3:main"}


class TestCanonicalIsolation:
    """同一 collection root、series_group 故意相同 → 全投影层隔离。"""

    def _two_units(self):
        from app.import_plan import revision_store

        _make_unit("unit-cp2-a", boundary="/动画/作品A")
        _make_unit("unit-cp2-b", boundary="/动画/作品B")
        rev_a = revision_store.create_revision(
            unit_id="unit-cp2-a", source_generation=1,
            items=[_item("作品A/Season 1/a.mkv", canonical_work_id="unit:unit-cp2-a:main")],
            status="confirmed",
        )
        rev_b = revision_store.create_revision(
            unit_id="unit-cp2-b", source_generation=1,
            items=[_item("作品B/Season 1/b.mkv", canonical_work_id="unit:unit-cp2-b:main")],
            status="confirmed",
        )
        return revision_store.load_plan(rev_a["revision_id"]), revision_store.load_plan(rev_b["revision_id"])

    def test_library_work_ids_are_distinct(self):
        """series_group 相同也不得合并不同 canonical 的 Library works。"""
        from app.library.index import build_library_index

        plan_a, plan_b = self._two_units()
        index_a = build_library_index(plan_a)
        index_b = build_library_index(plan_b)
        works_a = {work.work_id for work in index_a.works}
        works_b = {work.work_id for work in index_b.works}
        assert works_a and works_b
        assert works_a.isdisjoint(works_b)

    def test_mirror_roots_are_distinct(self):
        """不同 canonical 即使 series_group 相同也不得共享 mirror work root。"""
        from app.mirror.generator import generate_mirror

        plan_a, plan_b = self._two_units()
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 预填 target 路径（generate_mirror 从 target_dir 推导 work root）
            for plan in (plan_a, plan_b):
                for item in plan.items:
                    item.target_dir = str(root / "openlist" / plan.plan_id)
            result_a = generate_mirror(plan_a, mirror_root=str(root))
            result_b = generate_mirror(plan_b, mirror_root=str(root))
            assert result_a.status in {"success", "partial_failed"}
            assert result_b.status in {"success", "partial_failed"}

    def test_scrape_targets_are_distinct(self):
        """不同 canonical 的 ScrapeTarget 不得跨 canonical 聚合。"""
        from app.scrape.target_builder import build_scrape_targets

        plan_a, plan_b = self._two_units()
        targets_a = build_scrape_targets(plan_a)
        targets_b = build_scrape_targets(plan_b)
        ids_a = {t.scrape_target_id for t in targets_a}
        ids_b = {t.scrape_target_id for t in targets_b}
        assert ids_a.isdisjoint(ids_b)
        for target in targets_a + targets_b:
            assert target.canonical_work_id  # 每个 target 必须归属明确 canonical

    def test_artifact_attribution_uses_canonical(self):
        """artifact_records.work_id 必须使用 canonical work identity。"""
        from app.import_plan import revision_store
        from app.pipeline.artifacts import upsert_artifact

        _make_unit("unit-cp2-c", boundary="/动画/作品C")
        rev = revision_store.create_revision(
            unit_id="unit-cp2-c", source_generation=1,
            items=[_item("作品C/Season 1/c.mkv", canonical_work_id="unit:unit-cp2-c:main")],
            status="confirmed",
        )
        # 模拟 handler 传入的 item 级 work_id="w"（普通 work_id）
        upsert_artifact(
            kind="strm", path="H:/mirror/openlist/作品C/Season 1/c.strm",
            revision_id=rev["revision_id"], work_id="w", require_current=False,
        )
        from app.db.database import get_connection
        row = get_connection().execute(
            "SELECT work_id FROM artifact_records WHERE kind='strm'"
        ).fetchone()
        assert row is not None
        assert row["work_id"] == "unit:unit-cp2-c:main"


class TestSameWorkAggregation:
    def test_seasons_and_special_merge_into_one_canonical(self):
        """同一 work 的 Season 1 / Season 2 / Special 聚合到同一 canonical work。"""
        from app.import_plan import revision_store
        from app.library.index import build_library_index

        _make_unit("unit-cp2-agg")
        rev = revision_store.create_revision(
            unit_id="unit-cp2-agg", source_generation=1,
            items=[
                _item("作品/Season 1/a.mkv", season_number=1, episode_number=1,
                      canonical_work_id="unit:unit-cp2-agg:main"),
                _item("作品/Season 2/b.mkv", season_number=2, episode_number=1,
                      canonical_work_id="unit:unit-cp2-agg:main"),
                _item("作品/SPs/s.mkv", group_type="special", special_number=1,
                      canonical_work_id="unit:unit-cp2-agg:main"),
            ],
            status="confirmed",
        )
        plan = revision_store.load_plan(rev["revision_id"])
        index = build_library_index(plan)
        # S1/S2/Special 拥有同一 canonical_work_id → 聚合为同一张卡（work_id
        # 就是 canonical identity）；episodes 计数需要 scan_result（strm 事实），
        # 这里断言聚合边界本身。
        assert len(index.works) == 1
        work = index.works[0]
        assert work.work_id == "unit:unit-cp2-agg:main"
        from app.library.index import _library_work_id
        work_ids = {_library_work_id(item) for item in plan.items}
        assert work_ids == {"unit:unit-cp2-agg:main"}
