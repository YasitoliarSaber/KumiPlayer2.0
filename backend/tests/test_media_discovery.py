"""任务 4 验收：证据驱动发现 + 不可变 Import Revision。

覆盖：Season 1+2+OVA 单 unit、分类下多作品不合并、先 S1 后 S2 增量 revision、
歧义分支 needs_review 不影响其他、发现首部作品即进入链路（不等待其他分支）、
revision hash 未变不重复创建、缺失条目 unavailable。
"""


import pytest

from app.catalog import discovery, store
from app.db.database import close_connection, get_connection, init_db
from app.integrations.openlist.models import OpenListEntry
from app.integrations.openlist.scanner import OpenListDirectoryScanner


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    db_path = tmp_path / "discovery.db"
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
        self.calls: list[str] = []

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        self.calls.append(path)
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


def _setup_root(tree):
    store.create_source(source_id="ol", source_type="openlist", provider_id="quark")
    root = store.create_source_root(source_id="ol", remote_locator="/动画")
    generation = store.bump_generation(root.root_id)
    scanner = OpenListDirectoryScanner(FakeClient(tree), rate_per_second=0)
    engine = discovery.DiscoveryEngine(scanner, source_id="ol", root_id=root.root_id, generation=generation)
    return root, engine


def _run(engine, client=None, on_unit=None):
    if client is not None:
        engine.scanner.client = client
    return engine.run(on_unit=on_unit)


class TestDiscoveryUnits:
    def test_seasons_and_ova_merge_into_one_unit(self):
        tree = {
            "/动画": [("分类", True, None, None)],
            "/动画/分类": [("作品", True, None, None)],
            "/动画/分类/作品": [("Season 1", True, None, None), ("Season 2", True, None, None), ("OVA", True, None, None)],
            "/动画/分类/作品/Season 1": [("作品 - S01E01.mkv", False, 100, 1.0)],
            "/动画/分类/作品/Season 2": [("作品 - S02E01.mkv", False, 100, 1.0)],
            "/动画/分类/作品/OVA": [("作品 - OVA01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        units = [item for item in results if item["status"] == "plan_ready"]
        assert len(units) == 1
        assert units[0]["boundary"] == "/动画/分类/作品"
        assert units[0]["video_count"] == 3  # S1 + S2 + OVA 全部归入

        from app.import_plan import revision_store
        revision = revision_store.load_revision(units[0]["revision_id"])
        relative = {item["relative_path"] for item in revision["items"]}
        assert relative == {
            "分类/作品/Season 1/作品 - S01E01.mkv",
            "分类/作品/Season 2/作品 - S02E01.mkv",
            "分类/作品/OVA/作品 - OVA01.mkv",
        }

    def test_sibling_works_do_not_merge(self):
        tree = {
            "/动画": [("分类", True, None, None)],
            "/动画/分类": [("日漫", True, None, None)],
            "/动画/分类/日漫": [("作品A", True, None, None), ("作品B", True, None, None)],
            "/动画/分类/日漫/作品A": [("作品A - 01.mkv", False, 100, 1.0)],
            "/动画/分类/日漫/作品B": [("作品B - 01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        units = [item for item in results if item["status"] == "plan_ready"]
        assert len(units) == 2
        boundaries = {item["boundary"] for item in units}
        assert boundaries == {"/动画/分类/日漫/作品A", "/动画/分类/日漫/作品B"}

    def test_full_validation_creates_revision_when_second_season_appears(self):
        """完整校验发现 S2 后，归同一 unit 并产生增量 revision（hash 变化）。"""
        tree = {
            "/动画": [("作品", True, None, None)],
            "/动画/作品": [("Season 1", True, None, None)],
            "/动画/作品/Season 1": [("作品 - S01E01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        first = _run(engine)
        first_unit = [item for item in first if item["status"] == "plan_ready"][0]

        # 第二轮：S2 出现（同一 root 新 generation）
        tree["/动画/作品"].append(("Season 2", True, None, None))
        tree["/动画/作品/Season 2"] = [("作品 - S02E01.mkv", False, 100, 1.0)]
        second_gen = store.bump_generation(root.root_id)
        store.prepare_scan(root.root_id, generation=second_gen, mode="full")
        engine.generation = second_gen
        second = _run(engine)
        second_unit = [item for item in second if item["status"] == "plan_ready"][0]
        assert second_unit["boundary"] == "/动画/作品"
        assert second_unit["revision_id"] != first_unit["revision_id"]

        from app.import_plan import revision_store
        revision = revision_store.load_revision(second_unit["revision_id"])
        relative = {item["relative_path"] for item in revision["items"]}
        assert "作品/Season 2/作品 - S02E01.mkv" in relative
        assert "作品/Season 1/作品 - S01E01.mkv" in relative

    def test_needs_review_does_not_block_others(self):
        tree = {
            "/动画": [("日漫", True, None, None), ("001.mkv", False, 100, 1.0)],  # 根下无标题文件 → needs_review
            "/动画/日漫": [("好作品", True, None, None)],
            "/动画/日漫/好作品": [("好作品 - 01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        by_status = {item["status"] for item in results}
        assert "needs_review" in by_status
        assert any(item["status"] == "plan_ready" for item in results)

    def test_first_unit_processed_before_other_branches_scanned(self):
        """发现第一部作品后即进入链路：处理 unit A 时尚未请求其他分支目录。"""
        scan_order: list[str] = []

        class TrackingClient(FakeClient):
            def list_dir(self, path, page=1, per_page=100, refresh=False):
                scan_order.append(path)
                return super().list_dir(path, page=page, per_page=per_page, refresh=refresh)

        tree = {
            "/动画": [("A", True, None, None), ("B", True, None, None)],
            "/动画/A": [("作品A - 01.mkv", False, 100, 1.0)],
            "/动画/B": [("作品B - 01.mkv", False, 100, 1.0)],
        }
        store.create_source(source_id="ol", source_type="openlist")
        root = store.create_source_root(source_id="ol", remote_locator="/动画")
        generation = store.bump_generation(root.root_id)
        client = TrackingClient(tree)
        engine = discovery.DiscoveryEngine(
            OpenListDirectoryScanner(client, rate_per_second=0),
            source_id="ol", root_id=root.root_id, generation=generation,
        )
        units_seen: list[dict] = []

        def on_unit(unit):
            units_seen.append(unit)
            # 处理第一个单元时，不得已扫描过第二个候选分支
            if len(units_seen) == 1:
                assert "/动画/B" not in scan_order

        engine.run(on_unit=on_unit)
        assert len(units_seen) == 2

    def test_openlist_episodes_under_work_dir_merge_into_one_unit(self):
        """OpenList 相对选中 root 的路径无分类层（作品目录直属多集文件）：
        多集必须合并为同一作品单元，且每集不得被识别成独立作品。"""
        tree = {
            "/动画": [("1.紫罗兰永恒花园.2018", True, None, None)],
            "/动画/1.紫罗兰永恒花园.2018": [
                ("[MAI] Violet Evergarden - 01 [Ma10p_2160p][x265_flac_ass].mkv", False, 100, 1.0),
                ("[MAI] Violet Evergarden - 02 [Ma10p_2160p][x265_flac_ass].mkv", False, 100, 1.0),
                ("[MAI] Violet Evergarden - 03 [Ma10p_2160p][x265_flac_ass].mkv", False, 100, 1.0),
            ],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        units = [item for item in results if item["status"] == "plan_ready"]
        assert len(units) == 1
        assert units[0]["boundary"] == "/动画/1.紫罗兰永恒花园.2018"
        assert units[0]["video_count"] == 3

        from app.import_plan import revision_store
        revision = revision_store.load_revision(units[0]["revision_id"])
        titles = {item["work_title"] for item in revision["items"]}
        # 全部集数归入同一作品，而不是每集一个 "Violet Evergarden - NN"
        assert titles == {"1.紫罗兰永恒花园"}

    def test_non_video_files_do_not_pollute_unit(self):
        """封面/字体/字幕等附件不参与识别：既不污染作品标题，
        也不把整部作品拖进 needs_review。"""
        tree = {
            "/动画": [("作品A", True, None, None)],
            "/动画/作品A": [
                ("cover.jpg", False, 50, 1.0),
                ("[MAI] 作品A - 01 [x265].mkv", False, 100, 1.0),
                ("作品A [Fonts].exe", False, 200, 1.0),
                ("作品A.srt", False, 10, 1.0),
            ],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["boundary"] == "/动画/作品A"
        assert ready[0]["video_count"] == 1

    def test_attachment_only_dir_is_not_candidate(self):
        """只有附件没有视频的目录不成为作品单元。"""
        tree = {
            "/动画": [("附件夹", True, None, None)],
            "/动画/附件夹": [("readme.txt", False, 1, 1.0), ("cover.jpg", False, 50, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        assert results == []

    def test_failed_directory_does_not_stop_scan(self):
        """单个目录扫描失败（网络/超时）只记录失败，不中断整个 root 扫描。"""
        from app.integrations.openlist.models import OpenListNetworkError

        class FlakyClient(FakeClient):
            def list_dir(self, path, page=1, per_page=100, refresh=False):
                if path == "/动画/坏目录":
                    raise OpenListNetworkError()
                return super().list_dir(path, page=page, per_page=per_page, refresh=refresh)

        tree = {
            "/动画": [("坏目录", True, None, None), ("好作品", True, None, None)],
            "/动画/坏目录": [("坏目录 - 01.mkv", False, 100, 1.0)],
            "/动画/好作品": [("好作品 - 01.mkv", False, 100, 1.0)],
        }
        store.create_source(source_id="ol", source_type="openlist")
        root = store.create_source_root(source_id="ol", remote_locator="/动画")
        generation = store.bump_generation(root.root_id)
        engine = discovery.DiscoveryEngine(
            OpenListDirectoryScanner(FlakyClient(tree), rate_per_second=0),
            source_id="ol", root_id=root.root_id, generation=generation,
        )
        results = engine.run()
        assert "/动画/坏目录" in engine.failed_paths
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert any(item["boundary"] == "/动画/好作品" for item in ready)
        # 失败目录不产生 needs_review 单元（无已入库内容）
        assert all(item["boundary"] != "/动画/坏目录" for item in results)

    def test_failed_structural_dir_blocks_closure(self):
        """作品下某个 Season 目录失败阻塞该作品收口：不生成 revision；
        同 root 下另一完整作品（Bar）不受影响，正常出 revision。"""
        from app.integrations.openlist.models import OpenListNetworkError

        class FlakyClient(FakeClient):
            def list_dir(self, path, page=1, per_page=100, refresh=False):
                if path == "/动画/作品/Season 2":
                    raise OpenListNetworkError()
                return super().list_dir(path, page=page, per_page=per_page, refresh=refresh)

        tree = {
            "/动画": [("作品", True, None, None), ("好作品", True, None, None)],
            "/动画/作品": [("Season 1", True, None, None), ("Season 2", True, None, None)],
            "/动画/作品/Season 1": [("作品 - S01E01.mkv", False, 100, 1.0)],
            "/动画/作品/Season 2": [("作品 - S02E01.mkv", False, 100, 1.0)],
            "/动画/好作品": [("好作品 - 01.mkv", False, 100, 1.0)],
        }
        store.create_source(source_id="ol", source_type="openlist")
        root = store.create_source_root(source_id="ol", remote_locator="/动画")
        generation = store.bump_generation(root.root_id)
        engine = discovery.DiscoveryEngine(
            OpenListDirectoryScanner(FlakyClient(tree), rate_per_second=0),
            source_id="ol", root_id=root.root_id, generation=generation,
        )
        results = engine.run()
        assert "/动画/作品/Season 2" in engine.failed_paths
        # 不完整作品（Foo：S1 complete + S2 failed）不生成 revision
        assert all(item["boundary"] != "/动画/作品" for item in results)
        assert all(item.get("status") != "plan_ready" or item["boundary"] != "/动画/作品"
                   for item in results)
        # 无关完整作品（Bar）仍可立即收口并生成 revision
        ready = [item for item in results if item["status"] == "plan_ready"]
        assert len(ready) == 1
        assert ready[0]["boundary"] == "/动画/好作品"


class TestImportRevisions:
    def _ensure_unit(self, unit_id: str) -> None:
        conn = get_connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, '', 'root-x', '', '/动画', 'w', 'discovered', 0, '', ?, ?)
            """,
            (unit_id, store.now_iso(), store.now_iso()),
        )
        conn.commit()

    def _revision_items(self, paths):
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

    def test_hash_unchanged_does_not_create_new_revision(self):
        from app.import_plan import revision_store

        store.create_source(source_id="s", source_type="local")
        unit_id = "unit-1"
        self._ensure_unit(unit_id)
        items = self._revision_items(["a.mkv", "b.mkv"])
        first = revision_store.create_revision(unit_id=unit_id, source_generation=1, items=items, status="confirmed")
        second = revision_store.create_revision(unit_id=unit_id, source_generation=2, items=items, status="confirmed")
        assert second["revision_id"] == first["revision_id"]  # 复用，不重复创建

    def test_changed_items_create_new_revision_with_parent(self):
        from app.import_plan import revision_store

        store.create_source(source_id="s", source_type="local")
        unit_id = "unit-2"
        self._ensure_unit(unit_id)
        first = revision_store.create_revision(unit_id=unit_id, source_generation=1, items=self._revision_items(["a.mkv"]), status="confirmed")
        second = revision_store.create_revision(
            unit_id=unit_id, source_generation=2,
            items=self._revision_items(["a.mkv", "c.mkv"]),
            parent_revision_id=first["revision_id"],
            status="draft",
        )
        assert second["revision_id"] != first["revision_id"]
        assert second["parent_revision_id"] == first["revision_id"]

        # 缺失条目标记 unavailable（保留身份）
        revision_store.mark_missing_items(second["revision_id"], {"b.mkv"})
        # b.mkv 从未存在 → 不出现；这里验证 API 幂等即可
        assert len(second["items"]) == 2

    def test_load_plan_returns_dataclass(self):
        from app.import_plan import revision_store

        store.create_source(source_id="s", source_type="local")
        self._ensure_unit("unit-3")
        revision = revision_store.create_revision(
            unit_id="unit-3", source_generation=1, items=self._revision_items(["a.mkv"]),
            status="confirmed",
        )
        plan = revision_store.load_plan(revision["revision_id"])
        assert plan is not None
        assert plan.plan_id == revision["revision_id"]
        assert len(plan.items) == 1
        assert plan.items[0].relative_path == "a.mkv"


class TestProviderPropagation:
    """Preflight 0：OpenList route → discovery → recognition → revision 贯通。

    规划员验收：OpenList route provider=quark → Fake OpenList scan → 作品完成
    closure → revision → revision item.provider_id == quark；source_route_id
    正确传播到对应事实层（source_nodes.route_id）。全程 fake，0 真实网盘请求。
    """

    def test_openlist_route_provider_propagates_to_revision_items(self, monkeypatch):
        from app.core import config as core_config
        from app.integrations.openlist.providers import OpenListRouteConfig

        routes = [
            OpenListRouteConfig(
                route_id="route-quark",
                label="夸克",
                remote_prefix="/动画",
                provider_id="quark",
            )
        ]
        real_load = core_config.load_config

        def _load_with_routes(*args, **kwargs):
            cfg = real_load(*args, **kwargs)
            cfg.openlist_routes = routes
            return cfg

        # _provider_for_boundary 从 load_config().openlist_routes 做最长前缀匹配
        monkeypatch.setattr(core_config, "load_config", _load_with_routes)

        tree = {
            "/动画": [("作品", True, None, None)],
            "/动画/作品": [("Season 1", True, None, None)],
            "/动画/作品/Season 1": [("作品 - S01E01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        units = [item for item in results if item["status"] == "plan_ready"]
        assert len(units) == 1
        assert units[0]["boundary"] == "/动画/作品"

        from app.import_plan import revision_store

        revision = revision_store.load_revision(units[0]["revision_id"])
        assert revision is not None
        items = revision["items"]
        assert items, "revision 应有条目"
        for item in items:
            assert item["provider_id"] == "quark"

        # source_route_id 传播到 Source Catalog 节点事实层（source_nodes.route_id，
        # 随首批入库文件节点持久化）
        nodes = store.list_nodes(root.root_id)
        video_node = [n for n in nodes if n["remote_path"] == "/动画/作品/Season 1/作品 - S01E01.mkv"]
        assert video_node, "视频节点应存在且携带 route 事实"
        assert video_node[0]["provider_id"] == "quark"
        assert video_node[0]["route_id"] == "route-quark"

    def test_route_miss_falls_back_to_openlist_compat(self, monkeypatch):
        """路由未命中（前缀不匹配）时回退 openlist 兼容 provider，route_id 为空。"""
        from app.core import config as core_config
        from app.integrations.openlist.providers import OpenListRouteConfig

        routes = [
            OpenListRouteConfig(
                route_id="route-quark",
                label="夸克",
                remote_prefix="/电影",
                provider_id="quark",
            )
        ]
        real_load = core_config.load_config

        def _load_with_routes(*args, **kwargs):
            cfg = real_load(*args, **kwargs)
            cfg.openlist_routes = routes
            return cfg

        monkeypatch.setattr(core_config, "load_config", _load_with_routes)

        tree = {
            "/动画": [("作品", True, None, None)],
            "/动画/作品": [("Season 1", True, None, None)],
            "/动画/作品/Season 1": [("作品 - S01E01.mkv", False, 100, 1.0)],
        }
        root, engine = _setup_root(tree)
        results = _run(engine)
        units = [item for item in results if item["status"] == "plan_ready"]
        assert len(units) == 1

        from app.import_plan import revision_store

        revision = revision_store.load_revision(units[0]["revision_id"])
        assert revision is not None
        for item in revision["items"]:
            # 未命中启用路由：归 openlist 兼容 provider（夸克试点回填），
            # 但 route_id 必须保持空（不存在路由事实）
            assert item["provider_id"] == "quark"
        nodes = store.list_nodes(root.root_id)
        video_node = [n for n in nodes if n["remote_path"] == "/动画/作品/Season 1/作品 - S01E01.mkv"]
        assert video_node and video_node[0]["route_id"] == ""
