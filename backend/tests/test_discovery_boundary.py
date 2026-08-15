"""专项大模块 CP1：Discovery 作品边界结构归属回归。

合成 fixture 覆盖规划员给出的真实样本结构（不提交真实百度目录树）：

- WorkA/{tv, sprcial}                 → 作品内 wrapper/内容段不得晋升
- WorkB/WorkB/{Season 1, SPs}         → 同名 wrapper 吸收到外层
- WorkC/{WorkCS1, WorkCS2}            → 父名+季标记 子目录吸收到外层
- WorkD/{WorkDSeason 1, WorkDSeason 2} → 父名+Season 标记 子目录吸收
- WorkE/Season 1                      → 标准结构段照常归并
- 用户直接选择单作品根（Season 1/2/SPs）→ boundary = 选中根

关键断言：
1. 顶级 MediaUnit boundary 只能是外层作品，禁止 tv/sprcial/同名子目录/
   父名+季标记目录 独立成作品；
2. 选中根为多作品分类（如「刮削好的动画」）时，各作品 series_group
   不得共享选中根名（结构逻辑修复，而非仅补 generic 关键词）。
"""

import pytest

from app.catalog import discovery, store
from app.db.database import close_connection, init_db
from app.integrations.openlist.models import OpenListEntry
from app.integrations.openlist.scanner import OpenListDirectoryScanner


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "boundary.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod
    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


class FakeClient:
    def __init__(self, tree):
        self.tree = tree

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
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


def _setup_root(tree, remote_locator="/刮削好的动画"):
    store.create_source(source_id="ol", source_type="openlist", provider_id="quark")
    root = store.create_source_root(source_id="ol", remote_locator=remote_locator)
    generation = store.bump_generation(root.root_id)
    scanner = OpenListDirectoryScanner(FakeClient(tree), rate_per_second=0)
    engine = discovery.DiscoveryEngine(
        scanner, source_id="ol", root_id=root.root_id, generation=generation
    )
    return root, engine


def _run(engine):
    return engine.run()


def _boundaries(results, status="plan_ready"):
    return {
        item["boundary"]
        for item in results
        if item.get("status") == status
    }


class TestSyntheticBoundaryFixture:
    """规划员合成 fixture：tv/sprcial/同名/父名+季标记 全部吸收到外层。"""

    def test_worka_tv_and_sprcial_wrappers_absorb_to_worka(self):
        tree = {
            "/刮削好的动画": [("WorkA", True, None, None)],
            "/刮削好的动画/WorkA": [("tv", True, None, None), ("sprcial", True, None, None)],
            "/刮削好的动画/WorkA/tv": [("WorkA 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkA/sprcial": [("WorkA OVA01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert _boundaries(results) == {"/刮削好的动画/WorkA"}
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["video_count"] == 2

        from app.import_plan import revision_store
        revision = revision_store.load_revision(ready[0]["revision_id"])
        relative = {item["relative_path"] for item in revision["items"]}
        assert relative == {
            "WorkA/tv/WorkA 01.mkv",
            "WorkA/sprcial/WorkA OVA01.mkv",
        }

    def test_workb_same_name_wrapper_absorbs_to_outer_workb(self):
        tree = {
            "/刮削好的动画": [("WorkB", True, None, None)],
            "/刮削好的动画/WorkB": [("WorkB", True, None, None)],
            "/刮削好的动画/WorkB/WorkB": [
                ("Season 1", True, None, None), ("SPs", True, None, None),
            ],
            "/刮削好的动画/WorkB/WorkB/Season 1": [("WorkB S01E01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkB/WorkB/SPs": [("WorkB OVA01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert _boundaries(results) == {"/刮削好的动画/WorkB"}
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["video_count"] == 2

    def test_workc_parent_prefix_season_dirs_absorb_to_workc(self):
        tree = {
            "/刮削好的动画": [("WorkC", True, None, None)],
            "/刮削好的动画/WorkC": [("WorkCS1", True, None, None), ("WorkCS2", True, None, None)],
            "/刮削好的动画/WorkC/WorkCS1": [("WorkC 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkC/WorkCS2": [("WorkC 01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert _boundaries(results) == {"/刮削好的动画/WorkC"}
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["video_count"] == 2

    def test_workd_parent_prefix_season_verbose_dirs_absorb(self):
        tree = {
            "/刮削好的动画": [("WorkD", True, None, None)],
            "/刮削好的动画/WorkD": [
                ("WorkDSeason 1", True, None, None), ("WorkDSeason 2", True, None, None),
            ],
            "/刮削好的动画/WorkD/WorkDSeason 1": [("WorkD 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkD/WorkDSeason 2": [("WorkD 01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert _boundaries(results) == {"/刮削好的动画/WorkD"}
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["video_count"] == 2

    def test_worke_standard_season_structure_unchanged(self):
        tree = {
            "/刮削好的动画": [("WorkE", True, None, None)],
            "/刮削好的动画/WorkE": [("Season 1", True, None, None)],
            "/刮削好的动画/WorkE/Season 1": [("WorkE S01E01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert _boundaries(results) == {"/刮削好的动画/WorkE"}
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["video_count"] == 1

    def test_all_five_works_in_one_root_do_not_merge(self):
        """完整合成 fixture：WorkA~WorkE 同 root，必须各自独立 boundary。"""
        tree = {
            "/刮削好的动画": [("WorkA", True, None, None), ("WorkB", True, None, None),
                              ("WorkC", True, None, None), ("WorkD", True, None, None),
                              ("WorkE", True, None, None)],
            "/刮削好的动画/WorkA": [("tv", True, None, None), ("sprcial", True, None, None)],
            "/刮削好的动画/WorkA/tv": [("WorkA 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkA/sprcial": [("WorkA OVA01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkB": [("WorkB", True, None, None)],
            "/刮削好的动画/WorkB/WorkB": [("Season 1", True, None, None), ("SPs", True, None, None)],
            "/刮削好的动画/WorkB/WorkB/Season 1": [("WorkB S01E01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkB/WorkB/SPs": [("WorkB OVA01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkC": [("WorkCS1", True, None, None), ("WorkCS2", True, None, None)],
            "/刮削好的动画/WorkC/WorkCS1": [("WorkC 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkC/WorkCS2": [("WorkC 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkD": [("WorkDSeason 1", True, None, None), ("WorkDSeason 2", True, None, None)],
            "/刮削好的动画/WorkD/WorkDSeason 1": [("WorkD 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkD/WorkDSeason 2": [("WorkD 01.mkv", False, 100, 1.0)],
            "/刮削好的动画/WorkE": [("Season 1", True, None, None)],
            "/刮削好的动画/WorkE/Season 1": [("WorkE S01E01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert _boundaries(results) == {
            "/刮削好的动画/WorkA",
            "/刮削好的动画/WorkB",
            "/刮削好的动画/WorkC",
            "/刮削好的动画/WorkD",
            "/刮削好的动画/WorkE",
        }
        forbidden = {
            "/刮削好的动画/WorkA/tv",
            "/刮削好的动画/WorkA/sprcial",
            "/刮削好的动画/WorkB/WorkB",
            "/刮削好的动画/WorkC/WorkCS1",
            "/刮削好的动画/WorkC/WorkCS2",
            "/刮削好的动画/WorkD/WorkDSeason 1",
            "/刮削好的动画/WorkD/WorkDSeason 2",
        }
        assert not (_boundaries(results, status="plan_ready") & forbidden)
        assert not (_boundaries(results, status="needs_review") & forbidden)

    def test_single_work_selected_root_is_the_boundary(self):
        """用户直接选择单作品根：root 直属 Season/Specials，boundary=选中根。"""
        tree = {
            "/选中作品": [("Season 1", True, None, None), ("Season 2", True, None, None), ("SPs", True, None, None)],
            "/选中作品/Season 1": [("作品 S01E01.mkv", False, 100, 1.0)],
            "/选中作品/Season 2": [("作品 S02E01.mkv", False, 100, 1.0)],
            "/选中作品/SPs": [("作品 OVA01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree, remote_locator="/选中作品")
        results = _run(engine)
        assert _boundaries(results) == {"/选中作品"}
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["video_count"] == 3

    def test_generic_named_collection_root_does_not_leak_into_series_group(self):
        """结构修复核心：选中根名不在 generic 表（如「MediaLibrary」这类
        任意用户目录名），多个独立 boundary 也不得共享同一个 series_group。
        只加 generic 关键词（如把该目录名塞进 _GENERIC_CATEGORY_NAMES）而不修
        结构逻辑，本测试仍必须失败（root_container 必须来自 boundary）。"""
        tree = {
            "/MediaLibrary": [("石纪元", True, None, None), ("斩服少女", True, None, None)],
            "/MediaLibrary/石纪元": [("Season 1", True, None, None)],
            "/MediaLibrary/石纪元/Season 1": [("石纪元 S01E01.mkv", False, 100, 1.0)],
            "/MediaLibrary/斩服少女": [("Season 1", True, None, None)],
            "/MediaLibrary/斩服少女/Season 1": [("斩服少女 S01E01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree, remote_locator="/MediaLibrary")
        results = _run(engine)
        assert _boundaries(results) == {"/MediaLibrary/石纪元", "/MediaLibrary/斩服少女"}

        from app.import_plan import revision_store
        groups: set[str] = set()
        for item in results:
            if item.get("status") != "plan_ready":
                continue
            revision = revision_store.load_revision(item["revision_id"])
            for entry in revision["items"]:
                groups.add(entry["series_group"])
        assert groups == {"石纪元", "斩服少女"}
        assert "MediaLibrary" not in groups

    def test_baidu_collection_keyword_still_works_as_fallback(self):
        """「刮削好的动画」已加入 generic 兼容表：即使 root_container 仍
        回退到 SourceRoot basename（旧数据路径），也不会把根名写成 series_group。"""
        tree = {
            "/刮削好的动画": [("石纪元", True, None, None), ("斩服少女", True, None, None)],
            "/刮削好的动画/石纪元": [("Season 1", True, None, None)],
            "/刮削好的动画/石纪元/Season 1": [("石纪元 S01E01.mkv", False, 100, 1.0)],
            "/刮削好的动画/斩服少女": [("Season 1", True, None, None)],
            "/刮削好的动画/斩服少女/Season 1": [("斩服少女 S01E01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert _boundaries(results) == {"/刮削好的动画/石纪元", "/刮削好的动画/斩服少女"}

        from app.import_plan import revision_store
        groups: set[str] = set()
        for item in results:
            if item.get("status") != "plan_ready":
                continue
            revision = revision_store.load_revision(item["revision_id"])
            for entry in revision["items"]:
                groups.add(entry["series_group"])
        assert groups == {"石纪元", "斩服少女"}
        assert "刮削好的动画" not in groups
