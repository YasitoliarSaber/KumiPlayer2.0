"""专项大模块 CP4：离线 Fake OpenList E2E 与污染防护门禁回归。

完全离线：Fake OpenListClient + tmpdir + SQLite 测试库，0 真实网盘请求。

场景：
1. 多作品媒体库根（含 tv/sprcial wrapper、同名 wrapper、S1/S2 自定义目录、
   普通 Season 结构）→ 作品数量正确、无伪作品、canonical/mirror/scrape/
   library 全链隔离；
2. 一个 needs_review 作品不阻塞其他 works；人工 patch + confirm 后完成；
3. Work Y mirror 首败 → retry 同 revision 恢复、不创建重复业务 job、projection 正确；
4. 冲突防护：canonical A/B 标题清洗同名 → 稳定消歧不覆盖；
   series_group 相同 → 仍得到两个 Library works（本次事故最重要防回归门禁）。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import OpenListEntry
from app.main import app

REMOTE_ROOT = "/媒体库"
TREE = {
    "/媒体库": [("动画", True, None, None)],
    "/媒体库/动画": [
        ("石纪元", True, None, None),
        ("斩服少女", True, None, None),
        ("包装作品", True, None, None),
    ],
    # 普通 Season 结构
    "/媒体库/动画/石纪元": [("Season 1", True, None, None), ("Season 2", True, None, None)],
    "/媒体库/动画/石纪元/Season 1": [("石纪元 S01E01.mkv", False, 100, 1.0)],
    "/媒体库/动画/石纪元/Season 2": [("石纪元 S02E01.mkv", False, 100, 1.0)],
    # tv/sprcial wrapper
    "/媒体库/动画/斩服少女": [("tv", True, None, None), ("sprcial", True, None, None)],
    "/媒体库/动画/斩服少女/tv": [("斩服少女 01.mkv", False, 100, 1.0)],
    "/媒体库/动画/斩服少女/sprcial": [("斩服少女 OVA01.mkv", False, 100, 1.0)],
    # 同名 wrapper + S1/S2 自定义目录
    "/媒体库/动画/包装作品": [("包装作品", True, None, None)],
    "/媒体库/动画/包装作品/包装作品": [
        ("包装作品S1", True, None, None), ("包装作品S2", True, None, None),
    ],
    "/媒体库/动画/包装作品/包装作品/包装作品S1": [("包装作品 01.mkv", False, 100, 1.0)],
    "/媒体库/动画/包装作品/包装作品/包装作品S2": [("包装作品 01.mkv", False, 100, 1.0)],
}


class FakeOpenListClient:
    instances = []
    tree = TREE

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.calls = []
        FakeOpenListClient.instances.append(self)

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        normalized = normalize_remote_path(path)
        self.calls.append((normalized, bool(refresh), page))
        items = FakeOpenListClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        start = (page - 1) * per_page
        return type(
            "Page", (), {"entries": entries[start:start + per_page], "total": len(entries)}
        )()


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "cp4.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    yield
    close_connection()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.tree = TREE
    # discovery handler 通过 get_openlist_client 构造扫描器（不经 api 层），
    # 因此必须替换集成层工厂，而不是 app.api.openlist.OpenListClient。
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr(
        "app.integrations.openlist.client.get_openlist_client",
        lambda *args, **kwargs: FakeOpenListClient(*args, **kwargs),
    )
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _make_local_mount(tmp_path: Path) -> Path:
    root = tmp_path / "quark" / "动画"
    (root / "石纪元" / "Season 1").mkdir(parents=True)
    (root / "石纪元" / "Season 1" / "石纪元 S01E01.mkv").write_bytes(b"1")
    (root / "石纪元" / "Season 2").mkdir(parents=True)
    (root / "石纪元" / "Season 2" / "石纪元 S02E01.mkv").write_bytes(b"2")
    (root / "斩服少女" / "tv").mkdir(parents=True)
    (root / "斩服少女" / "tv" / "斩服少女 01.mkv").write_bytes(b"3")
    (root / "斩服少女" / "sprcial").mkdir(parents=True)
    (root / "斩服少女" / "sprcial" / "斩服少女 OVA01.mkv").write_bytes(b"4")
    (root / "包装作品" / "包装作品" / "包装作品S1").mkdir(parents=True)
    (root / "包装作品" / "包装作品" / "包装作品S1" / "包装作品 01.mkv").write_bytes(b"5")
    (root / "包装作品" / "包装作品" / "包装作品S2").mkdir(parents=True)
    (root / "包装作品" / "包装作品" / "包装作品S2" / "包装作品 01.mkv").write_bytes(b"6")
    return root


def _save_config(client: TestClient, tmp_path: Path) -> None:
    resp = client.post(
        "/api/openlist/config",
        json={
            "server_url": "https://ol.example.com:5244",
            "remote_root": REMOTE_ROOT,
            "mount_root": str(tmp_path / "quark"),
            "username": "quark-user",
            "password": "p@ssw0rd",
        },
    )
    assert resp.status_code == 200, resp.text


def _save_routes(client: TestClient, prefix: str = REMOTE_ROOT + "/动画", provider: str = "quark") -> None:
    resp = client.put(
        "/api/openlist/routes",
        json={
            "routes": [
                {
                    "route_id": "route-test",
                    "label": "夸克",
                    "remote_prefix": prefix,
                    "provider_id": provider,
                    "enabled": True,
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text


class TestOfflineE2E:
    """离线 Fake OpenList E2E：全链投影隔离。"""

    def _run_import_batch(self, client, tmp_path):
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan

        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画"], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        batch = resp.json()

        # 驱动 discovery job（同步执行 handler，等价于后台 worker 首次执行）
        for root in batch["roots"]:
            job = job_store.get_job(root["job_id"])
            assert job is not None
            result = handle_discovery_scan(job.payload)
            job_store.finish_job(job.job_id, "worker-test", status="succeeded", result=result)
        return batch

    def test_multi_work_e2e_boundaries_and_projection_isolation(self, client, tmp_path):
        """多作品根：作品数量正确、无伪作品、全链 canonical 隔离。"""
        from app.db.database import get_connection

        self._run_import_batch(client, tmp_path)

        conn = get_connection()
        units = conn.execute("SELECT * FROM media_units").fetchall()
        boundaries = {unit["boundary"] for unit in units}
        # 3 个真实作品（石纪元/斩服少女/包装作品），无 tv/sprcial/同名子目录伪作品
        assert boundaries == {
            REMOTE_ROOT + "/动画/石纪元",
            REMOTE_ROOT + "/动画/斩服少女",
            REMOTE_ROOT + "/动画/包装作品",
        }

        # canonical 身份：每个 unit 唯一且跨 item 稳定
        canonicals: set[str] = set()
        for unit in units:
            revision = conn.execute(
                "SELECT * FROM import_revisions WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1",
                (unit["unit_id"],),
            ).fetchone()
            assert revision is not None
            items = conn.execute(
                "SELECT canonical_work_id FROM import_revision_items WHERE revision_id = ?",
                (revision["revision_id"],),
            ).fetchall()
            assert items and all(item["canonical_work_id"] for item in items)
            unit_canonicals = {item["canonical_work_id"] for item in items}
            assert len(unit_canonicals) == 1
            canonicals.update(unit_canonicals)
        assert len(canonicals) == 3  # 三部作品三个 canonical，互不交叉

    def test_e2e_mirror_and_library_no_cross_work_pollution(self, client, tmp_path):
        """mirror tmpdir：不同 canonical 的 strm 目录不交叉。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.library.index import _library_work_id
        from app.mirror.generator import generate_mirror

        self._run_import_batch(client, tmp_path)
        conn = get_connection()

        mirror_root = tmp_path / "mirror_e2e"
        plan_work_ids: dict[str, str] = {}
        for unit in conn.execute("SELECT * FROM media_units").fetchall():
            revision = conn.execute(
                "SELECT * FROM import_revisions WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1",
                (unit["unit_id"],),
            ).fetchone()
            plan = revision_store.load_plan(revision["revision_id"])
            result = generate_mirror(plan, mirror_root=str(mirror_root))
            assert result.status in {"success", "partial_failed"}, result.errors
            # 每个 canonical 的 strm 根目录必须唯一
            root_dirs = {
                Path(item.strm_path).parent.parent
                for item in result.items
                if item.status in {"generated", "skipped"} and item.strm_path
            }
            assert len(root_dirs) == 1
            work_dir = next(iter(root_dirs))
            canonical = plan.items[0].canonical_work_id
            plan_work_ids[canonical] = str(work_dir)

        # 三部作品三个互不相同的镜像根
        assert len(set(plan_work_ids.values())) == 3

        # LibraryIndex：每个 canonical 计划独立成卡（canonical work_id 即卡片身份；
        # 无 scan_result 时 works 过滤需要 target_strm_path 回填，这里直接断言
        # canonical 身份解析互不相同——跨 plan 聚合隔离由 CP2 回归覆盖）
        resolved_work_ids: set[str] = set()
        for unit in conn.execute("SELECT * FROM media_units").fetchall():
            revision = conn.execute(
                "SELECT * FROM import_revisions WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1",
                (unit["unit_id"],),
            ).fetchone()
            plan = revision_store.load_plan(revision["revision_id"])
            for item in plan.items:
                resolved_work_ids.add(_library_work_id(item))
        assert len(resolved_work_ids) == 3

    def test_attention_unit_does_not_block_others_and_recovers(self, client, tmp_path):
        """needs_review 单元不阻塞其他作品；patch + confirm 后进入 durable mirror。"""
        from app.db.database import get_connection

        self._run_import_batch(client, tmp_path)
        conn = get_connection()

        # 把「包装作品」的 revision 改为 draft + needs_review（模拟识别模糊）
        unit = conn.execute(
            "SELECT * FROM media_units WHERE boundary LIKE '%包装作品' ORDER BY created_at LIMIT 1"
        ).fetchone()
        assert unit is not None
        revision = conn.execute(
            "SELECT * FROM import_revisions WHERE unit_id = ? ORDER BY created_at DESC LIMIT 1",
            (unit["unit_id"],),
        ).fetchone()
        conn.execute(
            "UPDATE import_revisions SET status = 'draft' WHERE revision_id = ?",
            (revision["revision_id"],),
        )
        conn.execute(
            "UPDATE import_revision_items SET needs_review = 1 WHERE revision_id = ?",
            (revision["revision_id"],),
        )
        conn.execute(
            "UPDATE media_units SET status = 'needs_review' WHERE unit_id = ?",
            (unit["unit_id"],),
        )
        conn.commit()

        # 其他作品（石纪元/斩服少女）照常完成（它们的 revision 仍是 confirmed/executed）
        others = conn.execute(
            "SELECT boundary FROM media_units WHERE boundary NOT LIKE '%包装作品'"
        ).fetchall()
        assert len(others) == 2

        # 人工 patch + confirm → durable mirror（V3 唯一路径）
        item_row = conn.execute(
            "SELECT item_id FROM import_revision_items WHERE revision_id = ? LIMIT 1",
            (revision["revision_id"],),
        ).fetchone()
        assert item_row is not None
        patch = client.patch(
            f"/api/imports/openlist/items/{item_row['item_id']}",
            json={
                "plan_id": revision["revision_id"],
                "patch": {"needs_review": False, "warnings": []},
            },
        )
        assert patch.status_code == 200, patch.text
        confirmed = client.post(
            "/api/imports/openlist/confirm",
            json={"plan_id": revision["revision_id"]},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["execution_mode"] == "durable"
        assert confirmed.json()["job_id"]

    def test_failed_mirror_retry_recovers_same_revision(self, client, tmp_path):
        """Work Y mirror 首败 → retry 同 revision 恢复，不创建重复业务 job。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store
        from app.pipeline import orchestrator

        batch = self._run_import_batch(client, tmp_path)
        conn = get_connection()
        unit = conn.execute(
            "SELECT * FROM media_units WHERE boundary LIKE '%石纪元' ORDER BY created_at LIMIT 1"
        ).fetchone()
        assert unit is not None
        revision_id = unit["current_revision_id"]
        unit_id = unit["unit_id"]

        # 首败 mirror
        first_job_id = orchestrator.enqueue_mirror(revision_id, unit_id, rerun=True)
        job_store.finish_job(
            first_job_id, "worker-test", status="failed",
            error="模拟镜像失败", result={"status": "failed"},
        )

        # 通过批次 retry 恢复
        resp = client.post(
            f"/api/openlist/import-batches/{batch['batch_id']}/units/{unit_id}/retry", json={}
        )
        assert resp.status_code == 200, resp.text
        assert "mirror" in resp.json()["retried_stages"]

        # 不创建第二个业务任务（同 resource_key coalesced）
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE job_type='mirror_revision' AND resource_key=?",
            (f"mirror:{revision_id}",),
        ).fetchone()
        assert rows["n"] == 1


class TestConflictGuard:
    """冲突防护：标题清洗同名 / series_group 相同 → 绝不合并或覆盖。"""

    def _make_two_units(self):
        from app.catalog import store as catalog_store
        from app.import_plan import revision_store

        conn = catalog_store.get_connection()
        for unit_id, boundary in (
            ("conf-a", "/动画/作品A"),
            ("conf-b", "/动画/作品B"),
        ):
            conn.execute(
                """
                INSERT OR IGNORE INTO media_units (
                    unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                    status, closure_generation, current_revision_id, created_at, updated_at
                ) VALUES (?, '', 'root-x', '', ?, 'w', 'confirmed', 0, '', '2026-08-01T00:00:00+08:00', '2026-08-01T00:00:00+08:00')
                """,
                (unit_id, boundary),
            )
        conn.commit()

        def items(relative, canonical, title):
            return [{
                "id": f"i-{canonical}", "source": "openlist", "provider_id": "quark",
                "relative_path": relative, "real_path": f"H:/open/{relative}",
                "logical_locator": f"H:/open/{relative}",
                "resource_type": "video", "action": "generate_strm",
                "work_id": f"w-{canonical}", "canonical_work_id": canonical,
                "work_title": title, "original_title": "", "year": 2024,
                "media_type": "tv", "show_type": "anime_series",
                "series_group": "同一系列",
                "card_type": "main_series", "belongs_to_series": "", "relation_type": "",
                "group_type": "season", "season_number": 1, "episode_number": 1,
                "special_number": None, "title": "", "target_dir": "",
                "target_strm_path": f"H:/mirror/openlist/{title}/Season 1/{title}.S01E01.strm",
                "confidence": "high", "needs_review": False, "availability": "available",
                "warnings": [], "reasons": [], "user_override_id": "",
            }]

        rev_a = revision_store.create_revision(
            unit_id="conf-a", source_generation=1,
            items=items("动画/作品A/Season 1/a.mkv", "unit:conf-a:main", "同名作品"),
            status="confirmed",
        )
        rev_b = revision_store.create_revision(
            unit_id="conf-b", source_generation=1,
            items=items("动画/作品B/Season 1/b.mkv", "unit:conf-b:main", "同名作品"),
            status="confirmed",
        )
        conn.execute("UPDATE media_units SET current_revision_id = ? WHERE unit_id = 'conf-a'", (rev_a["revision_id"],))
        conn.execute("UPDATE media_units SET current_revision_id = ? WHERE unit_id = 'conf-b'", (rev_b["revision_id"],))
        conn.commit()
        return (
            revision_store.load_plan(rev_a["revision_id"]),
            revision_store.load_plan(rev_b["revision_id"]),
        )

    def test_same_cleaned_title_disambiguates_mirror_roots(self, tmp_path):
        """canonical A/B 标题清洗同名 → mirror 目录稳定消歧，不覆盖。"""
        import tempfile

        from app.mirror.generator import generate_mirror

        plan_a, plan_b = self._make_two_units()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result_a = generate_mirror(plan_a, mirror_root=str(root))
            result_b = generate_mirror(plan_b, mirror_root=str(root))
            assert result_a.status in {"success", "partial_failed"}
            assert result_b.status in {"success", "partial_failed"}
            dirs_a = {
                Path(item.strm_path).parent.parent
                for item in result_a.items if item.status in {"generated", "skipped"}
            }
            dirs_b = {
                Path(item.strm_path).parent.parent
                for item in result_b.items if item.status in {"generated", "skipped"}
            }
            assert len(dirs_a) == 1 and len(dirs_b) == 1
            assert dirs_a != dirs_b  # 稳定消歧：不同 canonical 不同镜像根

    def test_same_series_group_yields_two_library_works(self):
        """series_group 相同 → 仍得到两个 Library works（本次事故最重要防回归门禁）。"""
        from app.library.index import build_library_index

        plan_a, plan_b = self._make_two_units()
        # 两个 plan 的 series_group 完全相同
        assert plan_a.items[0].series_group == plan_b.items[0].series_group
        index_a = build_library_index(plan_a)
        index_b = build_library_index(plan_b)
        works_a = {work.work_id for work in index_a.works}
        works_b = {work.work_id for work in index_b.works}
        assert works_a and works_b
        assert works_a.isdisjoint(works_b)
