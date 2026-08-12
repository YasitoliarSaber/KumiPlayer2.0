"""模块3 Checkpoint 1：Import Revision 语义载荷与人工确认工作流测试。

覆盖：
- SQLite revision item 新增语义字段完整 round-trip（dict → 行 → ImportPlanItem）
- draft PATCH original_title/year/media_type/special_number → reload 仍存在
- 语义字段变化 → semantic_hash 变化；target_strm_path 变化 → hash 不变
- confirmed/executed → semantic PATCH 被拒绝（409 语义）
- 已有 confirmed revision → 新 draft → media_units.current_revision_id 仍指旧 confirmed
- load_revision_context → root_container 稳定重建（含 '/' 根安全空值）
"""

from __future__ import annotations

import pytest

from app.db.database import close_connection, get_connection, init_db


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "revision_workflow.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


def _ensure_unit(unit_id: str, root_id: str = "root-x", boundary: str = "/动画/作品") -> None:
    conn = get_connection()
    conn.execute(
        """
        INSERT OR IGNORE INTO media_units (
            unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
            status, closure_generation, current_revision_id, created_at, updated_at
        ) VALUES (?, '', ?, '', ?, 'w', 'discovered', 0, '', ?, ?)
        """,
        (unit_id, root_id, boundary, "2026-08-12T00:00:00+08:00", "2026-08-12T00:00:00+08:00"),
    )
    conn.commit()


def _make_items(paths, **overrides):
    """构造带完整语义载荷的 revision item dict 列表。"""
    defaults = dict(
        id="", source="openlist", provider_id="quark",
        relative_path="", real_path="", logical_locator="",
        resource_type="video", action="generate_strm",
        work_id="w1", work_title="作品", original_title="",
        year=2024, media_type="tv", show_type="anime_series",
        series_group="作品系列", card_type="main_series",
        belongs_to_series="", relation_type="",
        group_type="season", season_number=1, episode_number=1,
        special_number=None, title="", target_dir="", target_strm_path="",
        confidence="high", needs_review=False, availability="available",
        warnings=[], reasons=[], user_override_id="",
    )
    items = []
    for index, path in enumerate(paths):
        item = dict(defaults)
        item.update({
            "id": f"i-{index}",
            "relative_path": path,
            "real_path": f"H:/open/{path}",
            "logical_locator": f"H:/open/{path}",
            "episode_number": index + 1,
        })
        item.update(overrides)
        items.append(item)
    return items


class TestSemanticRoundTrip:
    def test_full_semantic_fields_roundtrip(self):
        """ImportPlanItem 语义字段 → SQLite 行 → load_plan 全字段不丢。"""
        from app.import_plan import revision_store

        _ensure_unit("unit-1")
        items = _make_items(
            ["分类/作品/Season 1/作品 - S01E01.mkv"],
            original_title="Sakuhin",
            year=2011,
            media_type="tv",
            show_type="anime_series",
            belongs_to_series="series-w1",
            relation_type="main",
            group_type="season",
            season_number=1,
            episode_number=1,
            special_number=None,
            warnings=["警告1", "警告2"],
            reasons=["原因A"],
            user_override_id="ov-001",
        )
        revision = revision_store.create_revision(
            unit_id="unit-1", source_generation=1, items=items, status="draft",
        )
        revision_id = revision["revision_id"]

        # 行级 JSON 载荷
        raw = revision["items"][0]
        assert raw["original_title"] == "Sakuhin"
        assert raw["year"] == 2011
        assert raw["media_type"] == "tv"
        assert raw["show_type"] == "anime_series"
        assert raw["belongs_to_series"] == "series-w1"
        assert raw["relation_type"] == "main"
        assert raw["special_number"] is None
        assert raw["warnings_json"] == '["警告1", "警告2"]'
        assert raw["reasons_json"] == '["原因A"]'
        assert raw["user_override_id"] == "ov-001"

        # dataclass 恢复
        plan = revision_store.load_plan(revision_id)
        assert plan is not None
        item = plan.items[0]
        assert item.original_title == "Sakuhin"
        assert item.year == 2011
        assert item.media_type == "tv"
        assert item.show_type == "anime_series"
        assert item.belongs_to_series == "series-w1"
        assert item.relation_type == "main"
        assert item.special_number is None
        assert item.warnings == ["警告1", "警告2"]
        assert item.reasons == ["原因A"]
        assert item.user_override_id == "ov-001"

    def test_special_number_roundtrip(self):
        """special_number 非 None 值 round-trip。"""
        from app.import_plan import revision_store

        _ensure_unit("unit-1b")
        items = _make_items(
            ["作品/OVA/作品 - OVA01.mkv"],
            group_type="special", special_number=1,
            season_number=None, episode_number=None,
        )
        revision = revision_store.create_revision(
            unit_id="unit-1b", source_generation=1, items=items, status="draft",
        )
        plan = revision_store.load_plan(revision["revision_id"])
        assert plan is not None
        assert plan.items[0].special_number == 1


class TestPatchDraft:
    def test_patch_persists_semantic_fields_and_rehash(self):
        """draft PATCH original_title/year/media_type/special_number → reload 仍存在 + hash 更新。"""
        from app.import_plan import revision_store

        _ensure_unit("unit-2")
        items = _make_items(
            ["作品/OVA/作品 - OVA01.mkv"],
            group_type="special", special_number=1,
            season_number=None, episode_number=None,
        )
        revision = revision_store.create_revision(
            unit_id="unit-2", source_generation=1, items=items, status="draft",
        )
        revision_id = revision["revision_id"]
        old_hash = revision["hash"]

        patched = revision_store.patch_draft_revision_item(
            revision_id, "i-0",
            {"original_title": "原名", "year": 2015, "media_type": "tv", "special_number": 2},
        )
        assert patched["hash"] != old_hash  # 语义字段变化 → hash 重算
        item = patched["items"][0]
        assert item["original_title"] == "原名"
        assert item["year"] == 2015
        assert item["media_type"] == "tv"
        assert item["special_number"] == 2

        # reload 后仍存在（SQLite 持久化，而非仅内存）
        reloaded = revision_store.load_revision(revision_id)
        item = reloaded["items"][0]
        assert item["original_title"] == "原名"
        assert item["year"] == 2015
        assert item["special_number"] == 2
        # 全列 UPDATE 不得清空 dataclass 未建模的历史遗留列
        assert item["logical_locator"] == "H:/open/作品/OVA/作品 - OVA01.mkv"

    def test_patch_unknown_revision_raises(self):
        from app.import_plan import revision_store

        with pytest.raises(ValueError, match="不存在"):
            revision_store.patch_draft_revision_item("no-such", "i-0", {"original_title": "X"})

    def test_patch_forbidden_target_field_rejected(self):
        """白名单复用：target_strm_path 不可 patch。"""
        from app.import_plan import revision_store

        _ensure_unit("unit-2b")
        revision = revision_store.create_revision(
            unit_id="unit-2b", source_generation=1,
            items=_make_items(["a.mkv"]), status="draft",
        )
        with pytest.raises(ValueError, match="禁止"):
            revision_store.patch_draft_revision_item(
                revision["revision_id"], "i-0", {"target_strm_path": "x.strm"},
            )

    @pytest.mark.parametrize("status", ["confirmed", "executed"])
    def test_patch_rejected_for_non_draft(self, status):
        """confirmed/executed → semantic PATCH 被拒绝（409 语义业务错误）。"""
        from app.import_plan import revision_store

        _ensure_unit(f"unit-{status}")
        revision = revision_store.create_revision(
            unit_id=f"unit-{status}", source_generation=1,
            items=_make_items(["a.mkv"]), status=status,
        )
        with pytest.raises(revision_store.RevisionStatusError, match="仅 draft"):
            revision_store.patch_draft_revision_item(
                revision["revision_id"], "i-0", {"original_title": "X"},
            )


class TestSemanticHash:
    def test_semantic_field_change_changes_hash(self):
        from app.import_plan import revision_store

        base = _make_items(["a.mkv"], original_title="A")
        changed = _make_items(["a.mkv"], original_title="B")
        assert revision_store.items_hash(base) != revision_store.items_hash(changed)

    def test_target_strm_path_change_keeps_hash(self):
        from app.import_plan import revision_store

        left = _make_items(["a.mkv"], target_strm_path="mirror/作品/S01/1.strm")
        right = _make_items(["a.mkv"], target_strm_path="mirror/作品/S01/2.strm")
        assert revision_store.items_hash(left) == revision_store.items_hash(right)


class TestCurrentRevisionPointer:
    def test_new_draft_keeps_previous_confirmed_current_revision_id(self):
        """已有 confirmed revision → 创建新 draft → current_revision_id 仍指旧 confirmed。"""
        from app.import_plan import revision_store

        unit_id = "unit-5"
        _ensure_unit(unit_id)

        # 第一轮：draft → auto confirm（确认时才切换 current_revision_id）
        first = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        ok, reason = revision_store.try_auto_confirm_revision(first["revision_id"])
        assert ok, reason
        assert revision_store.load_revision(first["revision_id"])["status"] == "confirmed"
        conn = get_connection()
        row = conn.execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
        assert row["current_revision_id"] == first["revision_id"]
        assert row["status"] == "confirmed"

        # 第二轮：模拟 DiscoveryEngine._create_unit —— 只更新 status=plan_ready，
        # 不触碰 current_revision_id；再创建新 draft（hash 变化）。
        conn.execute(
            """
            UPDATE media_units SET work_key = ?, status = 'plan_ready', updated_at = ?
            WHERE unit_id = ?
            """,
            ("w", "2026-08-12T00:00:00+08:00", unit_id),
        )
        conn.commit()
        new_items = _make_items(
            ["作品 - S01E01.mkv", "作品 - S01E02.mkv"],
            episode_number=2,
        )
        parent = revision_store.latest_confirmed_revision(unit_id)
        assert parent is not None
        draft = revision_store.create_revision(
            unit_id=unit_id, source_generation=2, items=new_items,
            parent_revision_id=parent["revision_id"], status="draft",
        )
        assert draft["revision_id"] != first["revision_id"]

        row = conn.execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
        assert row["status"] == "plan_ready"
        # 新 draft 不抢占指针：仍指旧 confirmed revision
        assert row["current_revision_id"] == first["revision_id"]


class TestRevisionContext:
    def test_load_revision_context_rebuilds_root_container(self):
        """root_container 不落库，可通过 source_roots.remote_locator 稳定重建。"""
        from app.catalog import store
        from app.import_plan import revision_store
        from app.import_plan.context import load_revision_context

        store.create_source(source_id="s1", source_type="openlist", provider_id="quark")
        root = store.create_source_root(
            source_id="s1", remote_locator="/动画/测试",
            import_family="anime", import_scope="seasonal",
        )
        _ensure_unit("unit-6", root_id=root.root_id, boundary="/动画/测试")
        revision = revision_store.create_revision(
            unit_id="unit-6", source_generation=1,
            items=_make_items(["测试 - S01E01.mkv"]), status="confirmed",
        )

        context = load_revision_context(revision["revision_id"])
        assert context["revision_id"] == revision["revision_id"]
        assert context["unit_id"] == "unit-6"
        assert context["root_id"] == root.root_id
        assert context["remote_locator"] == "/动画/测试"
        assert context["root_container"] == "测试"
        assert context["import_family"] == "anime"
        assert context["import_scope"] == "seasonal"
        assert context["source"] == "openlist"
        assert context["provider_id"] == "quark"

    def test_load_revision_context_root_path_safe_empty_container(self):
        """remote_locator='/' → root_container 安全为空字符串。"""
        from app.catalog import store
        from app.import_plan import revision_store
        from app.import_plan.context import load_revision_context

        store.create_source(source_id="s2", source_type="local")
        root = store.create_source_root(source_id="s2", remote_locator="/")
        _ensure_unit("unit-7", root_id=root.root_id, boundary="/")
        revision = revision_store.create_revision(
            unit_id="unit-7", source_generation=1,
            items=_make_items(["a.mkv"]), status="draft",
        )

        context = load_revision_context(revision["revision_id"])
        assert context["remote_locator"] == ""
        assert context["root_container"] == ""

    def test_load_revision_context_unknown_revision_raises(self):
        from app.import_plan.context import load_revision_context

        with pytest.raises(ValueError, match="不存在"):
            load_revision_context("no-such")
