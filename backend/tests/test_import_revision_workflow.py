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

import threading

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


# ============================================================
# 模块3 Checkpoint 2：唯一 SQLite 确认事务（confirm_revision_state）
# ============================================================


def _make_json_plan(plan_id: str, status: str = "confirmed", source: str = "openlist"):
    """构造一个 legacy JSON 形状的 ImportPlan（与 SQLite revision 同 ID 时用于分流测试）。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem

    item = ImportPlanItem(
        id="j-1", plan_id=plan_id, raw_file_id="raw-j", source=source,
        relative_path="动画/作品X/作品X - S01E01.mkv",
        real_path="H:\\动画\\作品X\\作品X - S01E01.mkv",
        resource_type="video", action="generate_strm",
        work_title="作品X", work_id="wx", group_type="season",
        card_type="main_series", media_type="tv",
        season_number=1, episode_number=1,
    )
    return ImportPlan(
        plan_id=plan_id, source=source, status=status, items=[item],
        created_at="2026-08-12T00:00:00+08:00",
        updated_at="2026-08-12T00:00:00+08:00",
    )


class TestConfirmStateTransition:
    """confirm_revision_state / confirm_revision_manually / try_auto_confirm_revision 唯一事务。"""

    def test_manual_confirm_full_transition(self):
        """V3 manual confirm → confirmed + confirm_method=manual + confirmed_at 非空 + unit 指针更新。"""
        from app.import_plan import revision_store

        unit_id = "unit-c2-manual"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]

        result = revision_store.confirm_revision_manually(revision_id)
        assert result["transitioned"] is True
        assert result["confirm_method"] == "manual"
        assert result["confirmed_at"]

        loaded = revision_store.load_revision(revision_id)
        assert loaded["status"] == "confirmed"
        assert loaded["confirm_method"] == "manual"
        assert loaded["confirmed_at"]  # 修复：原实现缺 confirmed_at
        conn = get_connection()
        row = conn.execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
        assert row["status"] == "confirmed"
        assert row["current_revision_id"] == revision_id

    def test_manual_confirm_rejects_blockers(self):
        """error 级别 issue 阻塞 manual confirm（与 legacy confirm_plan 同一套验证）。"""
        from app.import_plan import revision_store

        unit_id = "unit-c2-blocked"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["a.mkv"], work_title="", group_type=""),
            status="draft",
        )
        with pytest.raises(revision_store.RevisionStatusError, match="error"):
            revision_store.confirm_revision_manually(revision["revision_id"])
        # 未确认，仍为 draft
        assert revision_store.load_revision(revision["revision_id"])["status"] == "draft"

    def test_manual_confirm_force_skips_blockers(self):
        """force=True 跳过 error 阻塞（与 legacy confirm_plan force 语义一致）。"""
        from app.import_plan import revision_store

        unit_id = "unit-c2-force"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["a.mkv"], work_title="", group_type=""),
            status="draft",
        )
        result = revision_store.confirm_revision_manually(revision["revision_id"], force=True)
        assert result["transitioned"] is True
        assert revision_store.load_revision(revision["revision_id"])["status"] == "confirmed"

    def test_new_confirm_supersedes_old_confirmed_and_switches_pointer(self):
        """旧 confirmed → 新 revision confirmed → old=superseded + unit 指针切到新 revision。"""
        from app.import_plan import revision_store

        unit_id = "unit-c2-chain"
        _ensure_unit(unit_id)
        first = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        ok, reason = revision_store.try_auto_confirm_revision(first["revision_id"])
        assert ok, reason

        # 第二轮：模拟 DiscoveryEngine 新 generation draft（parent 为旧 confirmed）
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status = 'plan_ready', updated_at = ? WHERE unit_id = ?",
            ("2026-08-12T00:00:00+08:00", unit_id),
        )
        conn.commit()
        new_items = _make_items(
            ["作品 - S01E01.mkv", "作品 - S01E02.mkv"],
        )
        second = revision_store.create_revision(
            unit_id=unit_id, source_generation=2, items=new_items,
            parent_revision_id=first["revision_id"], status="draft",
        )

        result = revision_store.confirm_revision_manually(second["revision_id"])
        assert result["transitioned"] is True
        assert first["revision_id"] in result["superseded"]
        assert revision_store.load_revision(first["revision_id"])["status"] == "superseded"
        assert revision_store.load_revision(second["revision_id"])["status"] == "confirmed"
        row = conn.execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id = ?", (unit_id,)
        ).fetchone()
        assert row["status"] == "confirmed"
        assert row["current_revision_id"] == second["revision_id"]

    def test_auto_confirm_sets_confirmed_at_and_supersedes_old_current(self):
        """auto confirm → confirmed_at 非空 + 旧 current 被 superseded（修复缺项）。"""
        from app.import_plan import revision_store

        unit_id = "unit-c2-auto-chain"
        _ensure_unit(unit_id)
        first = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        ok, reason = revision_store.try_auto_confirm_revision(first["revision_id"])
        assert ok, reason

        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status = 'plan_ready', updated_at = ? WHERE unit_id = ?",
            ("2026-08-12T00:00:00+08:00", unit_id),
        )
        conn.commit()
        second = revision_store.create_revision(
            unit_id=unit_id, source_generation=2,
            items=_make_items(["作品 - S01E02.mkv"]),
            parent_revision_id=first["revision_id"], status="draft",
        )
        ok, reason = revision_store.try_auto_confirm_revision(second["revision_id"])
        assert ok, reason

        loaded = revision_store.load_revision(second["revision_id"])
        assert loaded["status"] == "confirmed"
        assert loaded["confirmed_at"]  # 修复：auto 确认必须写 confirmed_at
        assert revision_store.load_revision(first["revision_id"])["status"] == "superseded"

    def test_confirm_idempotent(self):
        """已 confirmed/executed 再次确认 → transitioned=False，不重复转换。"""
        from app.import_plan import revision_store

        unit_id = "unit-c2-idem"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["a.mkv"]), status="draft",
        )
        first = revision_store.confirm_revision_state(revision["revision_id"], method="manual")
        assert first["transitioned"] is True

        second = revision_store.confirm_revision_state(revision["revision_id"], method="manual")
        assert second["transitioned"] is False
        assert second["superseded"] == []
        assert revision_store.load_revision(revision["revision_id"])["status"] == "confirmed"

    def test_confirm_does_not_supersede_other_unit(self):
        """确认一个 unit 的新 revision 不得误 supersede 其他 unit 的 confirmed revision。"""
        from app.import_plan import revision_store

        unit_a = "unit-c2-a"
        unit_b = "unit-c2-b"
        _ensure_unit(unit_a)
        _ensure_unit(unit_b)
        rev_a = revision_store.create_revision(
            unit_id=unit_a, source_generation=1,
            items=_make_items(["a1.mkv"]), status="draft",
        )
        ok, reason = revision_store.try_auto_confirm_revision(rev_a["revision_id"])
        assert ok, reason
        rev_b = revision_store.create_revision(
            unit_id=unit_b, source_generation=1,
            items=_make_items(["b1.mkv"]), status="draft",
        )
        ok, reason = revision_store.try_auto_confirm_revision(rev_b["revision_id"])
        assert ok, reason

        # unit_b 新 draft（parent=rev_b）确认 → unit_a 的 confirmed 不得被触碰
        rev_b2 = revision_store.create_revision(
            unit_id=unit_b, source_generation=2,
            items=_make_items(["b1.mkv", "b2.mkv"]),
            parent_revision_id=rev_b["revision_id"], status="draft",
        )
        result = revision_store.confirm_revision_manually(rev_b2["revision_id"])
        assert result["transitioned"] is True
        assert rev_a["revision_id"] not in result["superseded"]
        assert revision_store.load_revision(rev_a["revision_id"])["status"] == "confirmed"
        assert revision_store.load_revision(rev_b["revision_id"])["status"] == "superseded"


class TestV3SplitBrainApi:
    """V3 人工确认 split-brain 修复的 API 级验证。"""

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app

        return TestClient(app)

    def test_preview_same_id_prefers_sqlite_draft(self):
        """同 ID 同时存在 SQLite draft + JSON confirmed → preview 必须读 SQLite draft。"""
        from app.import_plan import revision_store
        from app.import_plan.store import save_import_plan

        unit_id = "unit-c2-api-1"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]
        # 同 ID 的 legacy JSON confirmed（split-brain 残留）
        save_import_plan(_make_json_plan(revision_id, status="confirmed"))

        response = self._client().get(
            f"/api/imports/openlist/preview?plan_id={revision_id}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["plan_id"] == revision_id
        assert data["status"] == "draft", "SQLite draft 必须优先于同 ID JSON confirmed"

    def test_v3_patch_updates_sqlite_without_legacy_json(self):
        """V3 PATCH → SQLite item 更新 → data/import_plans/<same-id>.json 不出现。"""
        from pathlib import Path

        from app.import_plan import revision_store
        from app.core.paths import get_data_dir

        unit_id = "unit-c2-api-2"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]

        response = self._client().patch(
            f"/api/imports/openlist/items/i-0",
            json={"plan_id": revision_id, "patch": {"work_title": "修正作品名"}},
        )
        assert response.status_code == 200
        assert response.json()["item"]["work_title"] == "修正作品名"

        item = revision_store.load_revision(revision_id)["items"][0]
        assert item["work_title"] == "修正作品名"
        json_path = Path(get_data_dir()) / "import_plans" / f"{revision_id}.json"
        assert not json_path.exists(), "V3 PATCH 不得回写 legacy JSON（split-brain 源头）"

    def test_v3_confirm_enqueues_mirror_exactly_once_and_idempotent(self):
        """V3 confirm → durable mirror job 恰好一次 + 重复 confirm 不重复入队。"""
        from app.import_plan import revision_store

        unit_id = "unit-c2-api-3"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]

        client = self._client()
        first = client.post(
            "/api/imports/openlist/confirm",
            json={"plan_id": revision_id},
        )
        assert first.status_code == 200
        data = first.json()
        assert data["status"] == "confirmed"
        assert data["execution_mode"] == "durable"
        assert data["job_id"], "transitioned=True 必须返回可用 job_id"
        job_id = data["job_id"]

        conn = get_connection()
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = 'mirror_revision' AND payload LIKE ?",
            (f'%"revision_id": "{revision_id}"%',),
        ).fetchone()[0]
        assert count == 1, f"mirror job 应恰好一个，实际 {count}"
        assert conn.execute(
            "SELECT job_id FROM jobs WHERE job_type = 'mirror_revision' LIMIT 1"
        ).fetchone()["job_id"] == job_id

        # 重复 confirm：幂等，不创建第二个 mirror job
        second = client.post(
            "/api/imports/openlist/confirm",
            json={"plan_id": revision_id},
        )
        assert second.status_code == 200
        assert second.json()["execution_mode"] == "durable"
        assert second.json()["job_id"] == job_id, "重复 confirm 复用已有 job_id"
        count = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type = 'mirror_revision' AND payload LIKE ?",
            (f'%"revision_id": "{revision_id}"%',),
        ).fetchone()[0]
        assert count == 1, "重复 confirm 不得创建第二个 mirror job"

        loaded = revision_store.load_revision(revision_id)
        assert loaded["status"] == "confirmed"
        assert loaded["confirm_method"] == "manual"
        assert loaded["confirmed_at"]

    def test_legacy_json_confirm_keeps_old_behavior(self):
        """Legacy JSON plan confirm → 仍写 JSON + 旧行为（不生成镜像、无 durable 字段）。"""
        from app.import_plan.store import save_import_plan

        plan = _make_json_plan("plan-legacy-c2", status="draft")
        save_import_plan(plan)

        response = self._client().post(
            "/api/imports/openlist/confirm",
            json={"plan_id": "plan-legacy-c2"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "confirmed"
        assert "execution_mode" not in data
        assert "job_id" not in data
        assert "task_id" not in data

        # JSON 仍被写入并更新
        from app.import_plan.store import load_import_plan

        reloaded = load_import_plan(plan_id="plan-legacy-c2")
        assert reloaded is not None
        assert reloaded.status == "confirmed"


# ============================================================
# 模块3 Review Fix A：PATCH/confirm optimistic fence 与 mirror get-or-create
# ============================================================


def _mirror_job_count(revision_id: str) -> int:
    """统计 revision 的 durable mirror job 数量（按 resource_key 精确匹配）。"""
    conn = get_connection()
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM jobs "
            "WHERE job_type = 'mirror_revision' AND resource_key = ?",
            (f"mirror:{revision_id}",),
        ).fetchone()[0]
    )


class TestPatchConfirmFence:
    """PATCH/confirm TOCTOU 修复：乐观并发 fence（确定性 Barrier，无 sleep）。"""

    def test_patch_read_then_confirm_rejected(self, monkeypatch):
        """PATCH 已读取 draft（事务外读取后暂停）→ confirm 先提交 → PATCH 被拒。"""
        from app.import_plan import revision_store, service

        unit_id = "unit-fence-pc"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]

        read_done = threading.Event()
        allow_continue = threading.Event()
        orig_apply = service.apply_patch_rules

        def sync_apply(plan, item_id, patch):
            read_done.set()  # PATCH 线程事务外读取完成后暂停
            assert allow_continue.wait(timeout=10), "等待 confirm 提交超时"
            return orig_apply(plan, item_id, patch)

        monkeypatch.setattr(service, "apply_patch_rules", sync_apply)
        errors: list[Exception] = []

        def do_patch() -> None:
            try:
                revision_store.patch_draft_revision_item(
                    revision_id, "i-0", {"original_title": "并发修改"},
                )
            except Exception as exc:  # noqa: BLE001 - 收集异常供断言
                errors.append(exc)

        thread = threading.Thread(target=do_patch)
        thread.start()
        assert read_done.wait(timeout=10), "PATCH 线程未完成事务外读取"
        # confirm 先提交（确定性：PATCH 已暂停，confirm 必赢）
        result = revision_store.confirm_revision_manually(revision_id)
        assert result["transitioned"] is True
        allow_continue.set()
        thread.join(timeout=10)
        assert not thread.is_alive(), "PATCH 线程超时未退出"

        assert len(errors) == 1
        assert isinstance(errors[0], revision_store.RevisionStatusError)
        loaded = revision_store.load_revision(revision_id)
        assert loaded["status"] == "confirmed"
        assert loaded["items"][0]["original_title"] == "", "confirmed revision 不得被 PATCH 修改"

    def test_two_concurrent_patches_only_one_wins(self, monkeypatch):
        """两个 PATCH 基于同一 updated_at 并发 → 恰好一个成功，失败者不覆盖。"""
        from app.import_plan import revision_store, service

        unit_id = "unit-fence-pp"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["a.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]

        barrier = threading.Barrier(2)
        orig_apply = service.apply_patch_rules

        def sync_apply(plan, item_id, patch):
            barrier.wait(timeout=10)  # 两个线程都完成事务外读取后一起放行
            return orig_apply(plan, item_id, patch)

        monkeypatch.setattr(service, "apply_patch_rules", sync_apply)
        outcomes: list = []

        def do_patch(title: str) -> None:
            try:
                revision_store.patch_draft_revision_item(
                    revision_id, "i-0", {"original_title": title},
                )
                outcomes.append(("ok", title))
            except Exception as exc:  # noqa: BLE001 - 收集异常供断言
                outcomes.append(("err", exc))

        threads = [
            threading.Thread(target=do_patch, args=("甲",)),
            threading.Thread(target=do_patch, args=("乙",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads), "PATCH 线程超时未退出"

        oks = [item for item in outcomes if item[0] == "ok"]
        errs = [
            item for item in outcomes
            if isinstance(item[1], revision_store.RevisionStatusError)
        ]
        assert len(oks) == 1, f"应恰好一个成功，实际 {outcomes}"
        assert len(errs) == 1, f"应恰好一个 conflict，实际 {outcomes}"
        loaded = revision_store.load_revision(revision_id)
        assert loaded["items"][0]["original_title"] == oks[0][1], "失败者不得覆盖成功者"


class TestMirrorEnsureSemantics:
    """mirror get-or-create：confirmed→durable 可靠恰好一个 + crash self-healing。"""

    def test_confirm_self_heals_missing_mirror_job(self):
        """confirmed revision 无 mirror job（人为构造 crash 窗口）→ confirm 自动补出。"""
        from app.api.imports import ConfirmRequest, confirm
        from app.import_plan import revision_store

        unit_id = "unit-mirror-heal"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]
        # 人为构造 crash 窗口：只确认、不经 enqueue_mirror
        result = revision_store.confirm_revision_state(revision_id, method="auto")
        assert result["transitioned"] is True
        assert _mirror_job_count(revision_id) == 0, "前置：确认后无 mirror job（模拟崩溃）"

        response = confirm("openlist", ConfirmRequest(plan_id=revision_id))
        assert response["status"] == "confirmed"
        assert response["execution_mode"] == "durable"
        assert response["job_id"], "self-healing：重复 confirm 必须补出非空 job_id"
        assert _mirror_job_count(revision_id) == 1

    def test_concurrent_confirm_returns_same_job_id(self):
        """两个并发 confirm → job_id 均非空且完全相同 → DB 仅 1 个 mirror job。"""
        from app.api.imports import ConfirmRequest, confirm
        from app.import_plan import revision_store

        unit_id = "unit-mirror-conc"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]

        barrier = threading.Barrier(2)
        responses: list = []

        def do_confirm() -> None:
            barrier.wait(timeout=10)
            try:
                responses.append(confirm("openlist", ConfirmRequest(plan_id=revision_id)))
            except Exception as exc:  # noqa: BLE001 - 收集异常供断言
                responses.append(exc)

        threads = [threading.Thread(target=do_confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads), "confirm 线程超时未退出"

        assert len(responses) == 2
        assert all(isinstance(r, dict) for r in responses), f"并发 confirm 出现异常: {responses}"
        job_ids = {r["job_id"] for r in responses}
        assert all(job_ids), f"job_id 不得为空: {responses}"
        assert len(job_ids) == 1, f"并发 confirm 必须返回同一 job_id: {job_ids}"
        assert _mirror_job_count(revision_id) == 1, "并发 confirm 只能创建一个 mirror job"

    @pytest.mark.parametrize("final_status", ["failed", "succeeded"])
    def test_confirm_reuses_existing_terminal_job(self, final_status):
        """已有 failed/succeeded mirror job → 再 confirm → 复用原 job_id，count==1。"""
        from app.api.imports import ConfirmRequest, confirm
        from app.import_plan import revision_store

        unit_id = f"unit-mirror-{final_status}"
        _ensure_unit(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=_make_items(["作品 - S01E01.mkv"]), status="draft",
        )
        revision_id = revision["revision_id"]

        # 先确认并产生 mirror job，再模拟历史终态
        first = confirm("openlist", ConfirmRequest(plan_id=revision_id))
        existing_job_id = first["job_id"]
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET status = ? WHERE job_id = ?",
            (final_status, existing_job_id),
        )
        conn.commit()

        response = confirm("openlist", ConfirmRequest(plan_id=revision_id))
        assert response["job_id"] == existing_job_id, "已存在终态 job 必须复用原身份"
        assert _mirror_job_count(revision_id) == 1
