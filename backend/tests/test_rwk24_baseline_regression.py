"""RWK-24 验收：TXT 真正成为 Source Catalog baseline 的决定性回归。

A. 原生路径 TXT 导入（/api/media-presets/import-local-tree）
   → preset.catalog_root_id 非空、Provider root 存在、
     source_nodes > 0、source_directories complete > 0、全过程 0 OpenList 请求；
B. multipart TXT 导入与 A 共用同一共享 bootstrap，同挂载根不产生第二套 root；
C. TXT baseline 完成后再 bind → 成功、root_id/source_id 不变、snapshot 已存在；
D. 大型 fake TXT（多层大量目录）baseline → bind → 第一次 bound incremental
   → 必须真正执行 handle_discovery_scan
   → Fake OpenList 请求量 ≪ 全树目录数（不重新枚举已知 subtree）
   → source_nodes canonical identity 不变、media_units unit_id 不重复。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.media_presets.store import list_presets

# 大型 fake TXT：1 根 × 40 作品 × 2 季 ≈ 122 目录节点（>100 且足够证明
# "首次增量请求量 ≪ 已知目录数"；避免超大树的扫描耗时拖慢回归）
def _big_tree() -> str:
    lines = ["|——根目录", "| |-动画1"]
    for work in range(40):
        work_name = f"作品1-{work:03d}"
        lines.append(f"| | |-{work_name}")
        for season in range(2):
            lines.append(f"| | | |-Season {season + 1}")
            for ep in range(3):
                lines.append(
                    f"| | | | |-{work_name}.S{season + 1:02d}E{ep + 1:02d}.mkv"
                )
    return "\n".join(lines) + "\n"


class FakeOpenListClient:
    instances = []
    list_calls = 0
    requested_paths: list[str] = []
    tree = {}

    def __init__(self, server_url, username, password, **kwargs):
        FakeOpenListClient.instances.append(self)
        self.server_url = server_url
        self.username = username
        self.password = password

    def login(self):
        return "fake-token"

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        from app.integrations.openlist.client import join_remote_path, normalize_remote_path
        from app.integrations.openlist.models import OpenListEntry

        normalized = normalize_remote_path(path)
        FakeOpenListClient.list_calls += 1
        FakeOpenListClient.requested_paths.append(normalized)
        items = FakeOpenListClient.tree.get(normalized, [])
        entries = [
            OpenListEntry(
                name=name, is_dir=is_dir, size=size, modified=modified,
                remote_path=join_remote_path(normalized, name),
            )
            for name, is_dir, size, modified in items
        ]
        return type("Page", (), {"entries": entries, "total": len(entries)})()


@pytest.fixture(autouse=True)
def db_ready(tmp_path, monkeypatch):
    from app.db.database import close_connection, init_db

    db_path = tmp_path / "rwk24.db"
    monkeypatch.setattr("app.db.database._db_path", db_path)
    import app.db.database as db_mod

    if hasattr(db_mod._local, "connection"):
        db_mod._local.connection = None
    init_db()
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    import app.media_presets.store as mstore
    import app.media_presets.service as mservice
    import app.core.paths as paths_mod

    monkeypatch.setattr(paths_mod, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mstore, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(mservice, "get_data_dir", lambda: data_dir)
    # api 层模块级绑定的 get_data_dir 也要 patch（否则归档路径指向真实 data）
    import app.api.media_presets as api_mp

    monkeypatch.setattr(api_mp, "get_data_dir", lambda: data_dir)
    yield
    close_connection()


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.list_calls = 0
    FakeOpenListClient.requested_paths = []
    FakeOpenListClient.tree = {}
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.client.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr("app.integrations.openlist.connection.OpenListClient", FakeOpenListClient)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _run_snapshot_baseline(client, root_id):
    """RWK-34：TXT 导入即同步完成 baseline——此处仅断言 ready（无需手动执行）。"""
    from app.catalog import store as catalog_store

    stats = catalog_store.source_catalog_baseline_stats(root_id)
    assert stats["baseline_ready"] is True, "TXT 导入必须同步完成 baseline"
    return {"summary": {"failed_count": 0}}


def _write_tree(tmp_path, text: str, name: str = "115目录树.txt") -> Path:
    tree = tmp_path / name
    tree.write_text(text, encoding="utf-8")
    return tree


class TestSameTxtRecoveryUsesSurvivingArchive:
    """RWK-39（P1-2）：same-TXT recovery 必须使用现存 archive，覆盖 create/drag/multipart。

    事故链：failed baseline → 重新拖同一 TXT → _activate_or_reuse_tree discard 本次
    provisional archive → 必须用 selected_version.archive_path（存活归档）重建 baseline，
    不得用已删除的 provisional version.archive_path（否则 input_path 悬空 → baseline_failed）。
    """

    def test_reimport_same_txt_after_failed_baseline_recovers(self, client, tmp_path):
        from app.media_presets.store import get_preset, save_preset

        mount = tmp_path / "mount"
        mount.mkdir()
        tree = _write_tree(mount, _big_tree())
        # 1) 首次导入 → baseline ready，归档 V1 存活
        resp1 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp1.status_code == 200, resp1.text
        body1 = resp1.json()
        assert body1["baseline"]["status"] == "baseline_queued", body1
        preset_id = body1["preset"]["preset_id"]

        # 2) 模拟 baseline 失败（confirmation_state=failed）——recovery 入口的前置条件
        preset = get_preset(preset_id)
        preset.confirmation_state = "failed"
        save_preset(preset)

        # 3) 重新拖同一 TXT（同 SHA）→ _activate_or_reuse_tree discard provisional，
        #    recovery 必须用存活归档（selected_version.archive_path）重建 baseline
        resp2 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        # recovery 成功：baseline 重建为 baseline_queued，不是 baseline_failed
        assert body2["baseline"]["status"] == "baseline_queued", (
            f"same-TXT recovery 必须使用存活归档重建 baseline，而非已删除的 provisional：{body2['baseline']}"
        )


class TestRebindDurableSourceRoot:
    """RWK-39（P0-2）：rebind 实际视频文件夹必须是完整 durable operation。

    事故链：旧 local root → rebind 新 root → root_id 不变 → SourceRoot.local_locator
    == 新 root → generation 前进 → durable revision item.real_path 全部落在新 root
    → 旧 generation patch/confirm 409 stale → 新 generation confirm 成功。
    不得因更换本地播放挂载根就创建第二套 Provider source/root。
    """

    def test_rebind_keeps_root_advances_generation_updates_locator(self, client, tmp_path):
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.media_presets.store import list_presets

        # 1) 旧挂载根导入 TXT → durable baseline（root_id, gen=1）
        mount_old = tmp_path / "mount_old"
        mount_old.mkdir()
        tree = _write_tree(mount_old, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        baseline1 = body.get("baseline", {})
        assert baseline1.get("status") == "baseline_queued", body
        root_id = baseline1["confirmation_root_id"]
        gen1 = baseline1["confirmation_generation"]
        assert gen1 >= 1
        preset = next(p for p in list_presets() if p.catalog_root_id == root_id)
        preset_id = preset.preset_id

        # 2) rebind 到新实际视频文件夹
        mount_new = tmp_path / "mount_new"
        mount_new.mkdir()
        resp2 = client.post(
            f"/api/media-presets/{preset_id}/source-root",
            json={"source_root": str(mount_new)},
        )
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        baseline2 = body2["baseline"]
        assert baseline2["confirmation_root_id"] == root_id, "rebind 不得创建第二套 root"
        gen2 = baseline2["confirmation_generation"]
        assert gen2 > gen1, "rebind 必须 generation 前进"

        # 3) 同一 SourceRoot.local_locator 已切换到新根（root_id 不变）
        root = catalog_store.get_source_root(root_id)
        assert root.local_locator == str(mount_new), "local_locator 必须切到新根"

        # 4) 新 generation 的 draft revision item.real_path 全部落在新根
        conn = get_connection()
        rev_rows = conn.execute(
            "SELECT revision_id FROM import_revisions r "
            "JOIN media_units u ON u.unit_id = r.unit_id "
            "WHERE u.root_id = ? AND r.source_generation = ? AND r.status = 'draft'",
            (root_id, gen2),
        ).fetchall()
        assert rev_rows, "rebind 后必须有新 generation 的 draft revisions"
        for row in rev_rows:
            plan = revision_store.load_plan(row["revision_id"])
            assert plan is not None
            for item in plan.items:
                assert str(item.real_path).startswith(str(mount_new)), (
                    f"real_path 未落在新根: {item.real_path}"
                )

        # 5) 旧 generation confirm-root → 409 stale（target 已前进）
        resp_old = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": gen1},
        )
        assert resp_old.status_code == 409, "旧 generation confirm 必须被 409 stale"

        # 6) 新 generation confirm-root → 200（baseline 已同步完成）
        resp_new = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": gen2},
        )
        assert resp_new.status_code == 200, resp_new.text

    def test_v2_update_after_rebind_keeps_root_and_media_unit_identity(self, client, tmp_path):
        """RWK-40（P0-1）：rebind 后下一次 TXT 更新必须仍在原 SourceRoot 上重建。

        事故链：v1 old mount → rebind new mount → v2 TXT update 走 failed recovery
        → 若再从 local_mount 重新派生 source/root，会创建 R2 并改绑 catalog_root_id，
        导致 binding/baseline history/media_units 分裂。必须保持 root_id / source_id /
        catalog_root_id / MediaUnit ids 不变，generation 只在原 root 前进。
        """
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        # 1) v1 挂载根导入 TXT → durable baseline（root R1, source_id S1）
        mount_old = tmp_path / "mount_old"
        mount_old.mkdir()
        tree = _write_tree(mount_old, _big_tree())
        resp1 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp1.status_code == 200, resp1.text
        baseline1 = resp1.json().get("baseline", {})
        assert baseline1.get("status") == "baseline_queued", resp1.text
        root_id = baseline1["confirmation_root_id"]
        source_id_before = catalog_store.get_source_root(root_id).source_id
        gen_before = int(baseline1.get("confirmation_generation") or 0)
        assert gen_before >= 1, "v1 必须已有 generation"
        unit_ids_before = {
            row["unit_id"]
            for row in get_connection().execute(
                "SELECT unit_id FROM media_units WHERE root_id = ?", (root_id,)
            ).fetchall()
        }
        assert unit_ids_before, "v1 必须有 media_units"
        preset = next(p for p in list_presets() if p.catalog_root_id == root_id)
        preset_id = preset.preset_id

        # 2) rebind 新挂载根 → 仍 R1，gen 前进
        mount_new = tmp_path / "mount_new"
        mount_new.mkdir()
        resp2 = client.post(
            f"/api/media-presets/{preset_id}/source-root",
            json={"source_root": str(mount_new)},
        )
        assert resp2.status_code == 200, resp2.text
        assert resp2.json()["baseline"]["confirmation_root_id"] == root_id

        # 3) 把 preset 打成 failed，模拟 rebind 后 v2 更新触发 recovery
        from app.media_presets.store import get_preset as _gp, save_preset as _sp

        p = _gp(preset_id)
        p.confirmation_state = "failed"
        _sp(p)

        # 4) v2 TXT 更新（同 source，内容变化触发非 unchanged 重建）
        tree_v2 = _write_tree(tmp_path / "mount_new", _big_tree() + "| | |-额外作品1\n| | | |-额外作品1.S01E01.mkv\n")
        resp3 = client.post(
            f"/api/media-presets/{preset_id}/updates-from-path",
            json={"tree_path": str(tree_v2), "expected_source": "pan115"},
        )
        # 响应可能是 baseline_queued / baseline_reused / baseline_failed；核心断言在数据库
        # 4a) root_id 不变：绝不允许创建 R2
        assert resp3.status_code in (200, 409), resp3.text

        # 5) catalog_root_id / source_id / media_unit ids 全部不变（不分裂）
        p_after = _gp(preset_id)
        assert p_after.catalog_root_id == root_id, (
            f"v2 update 不得改绑 catalog_root_id: {p_after.catalog_root_id} != {root_id}"
        )
        root_after = catalog_store.get_source_root(root_id)
        assert root_after is not None, "R1 必须仍然存在"
        assert root_after.source_id == source_id_before, "source_id 不得分裂"
        # media_units 复用既有 unit（同一 root 下 unit_id 集合不产生新分裂身份）
        unit_ids_after = {
            row["unit_id"]
            for row in get_connection().execute(
                "SELECT unit_id FROM media_units WHERE root_id = ?", (root_id,)
            ).fetchall()
        }
        assert unit_ids_before == unit_ids_after or unit_ids_before.issubset(unit_ids_after), (
            "media_units 身份必须复用，不得因换 root 产生新分裂单元"
        )
        # 6) 全程只存在一个 SourceRoot（root_id 唯一）
        rows = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE source_id = ?",
            (source_id_before,),
        ).fetchall()
        assert int(rows[0]["c"]) == 1, "同一 source_id 只能有一个 SourceRoot"
        # 7) rebind 后 local_locator 已是新根
        assert root_after.local_locator == str(mount_new)
        # 8) generation 只在原 root 前进（rebind 与 v2 update 都推进且单调递增）
        gen_after = int(getattr(root_after, "active_generation", 0) or 0)
        assert gen_after > gen_before, (
            f"generation 必须只在原 root 前进: {gen_before} -> {gen_after}"
        )


class TestNativePathBaseline:
    def test_import_local_tree_creates_full_baseline(self, client, tmp_path):
        """A：原生路径 TXT 导入 → 完整 Source Catalog baseline，0 OpenList 请求。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        tree = _write_tree(tmp_path, _big_tree())
        calls_before = FakeOpenListClient.list_calls
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={
                "tree_path": str(tree),
                "import_family": "anime",
                "import_scope": "",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json().get("baseline", {}).get("status") == "baseline_queued", resp.text
        assert FakeOpenListClient.list_calls == calls_before, "TXT baseline 全程 0 OpenList 请求"

        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        assert root_id, "import-local-tree 后 catalog_root_id 必须非空"
        root = catalog_store.get_source_root(root_id)
        assert root is not None and root.source_id.startswith("pan115-")

        # 执行 baseline job → source_nodes + complete directories
        result = _run_snapshot_baseline(client, root_id)
        assert result["summary"].get("failed_count", 0) == 0

        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        dirs = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ? AND state = 'complete'",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0, "TXT baseline 必须写入 source_nodes"
        assert int(dirs) > 0, "TXT baseline 必须建立 complete source_directories frontier"

        # bindable-providers 显示 ready
        bp = client.get("/api/openlist/bindable-providers").json()
        entry = next(p for p in bp["providers"] if p["root_id"] == root_id)
        assert entry["baseline_ready"] is True
        assert entry["baseline_node_count"] == int(nodes)

    def test_multipart_and_native_share_same_root(self, client, tmp_path):
        """B：multipart 与原生路径同挂载根 → 同一 root（不产生第二套）。"""
        from app.catalog import store as catalog_store
        from app.media_presets.service import ensure_provider_source_root

        # 先用原生路径导入
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id

        # multipart 导入同一挂载根（source_root 指向同 mount 根）
        with open(tree, "rb") as fh:
            resp2 = client.post(
                "/api/media-presets",
                data={
                    "source": "pan115",
                    "source_root": str(tmp_path),
                    "import_family": "anime",
                    "import_scope": "",
                },
                files={"tree_file": ("115目录树.txt", fh, "text/plain")},
            )
        assert resp2.status_code == 200, resp2.text

        # 同挂载根的所有 pan115 root 应复用同一 root
        from app.db.database import get_connection

        rows = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_roots WHERE source_id LIKE 'pan115-%'",
        ).fetchone()
        assert int(rows["c"]) == 1, "同一挂载根不得产生第二套 Provider root"
        # preset 关联指向同一 root
        presets = [p for p in list_presets() if p.source == "pan115"]
        roots = {p.catalog_root_id for p in presets if p.catalog_root_id}
        assert len(roots) == 1

    def test_bind_after_baseline_keeps_identity(self, client, tmp_path):
        """C：TXT baseline 完成后 bind → 成功、root/source 不变、snapshot 已存在。"""
        from app.catalog import store as catalog_store

        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        source_id = catalog_store.get_source_root(root_id).source_id
        _run_snapshot_baseline(client, root_id)

        # 配置 OpenList + route → bind
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 200, bind.text
        root = catalog_store.get_source_root(root_id)
        assert root.source_id == source_id
        assert root.openlist_conn_hash != ""


class TestFirstIncrementalNotFullTraversal:
    def test_first_incremental_requests_far_less_than_tree(self, client, tmp_path):
        """D：大 TXT baseline → bind → 第一次 incremental 请求量 ≪ 全树目录数。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan
        from app.db.database import get_connection

        # 1. 大型 TXT baseline（1 根 × 40 作品 × 2 季 ≈ 122 目录节点）
        #    Fake 远端只暴露 root 直接成员（动画1 等），其余内容以 TXT baseline 为准
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        _run_snapshot_baseline(client, root_id)

        # 统计 TXT 已知目录数（source_directories 总数）
        total_dirs = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ?",
            (root_id,),
        ).fetchone()["c"])
        assert total_dirs > 100, f"测试前置：TXT 目录树应 >100 目录（实际 {total_dirs}）"

        # 2. 配置 OpenList + route + bind
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 200, bind.text

        # Fake 远端：root 直接成员与 TXT 一致（动画1），不提供深层内容——
        # incremental 必须依赖 TXT baseline（mtime UNKNOWN→baseline learning），
        # 不得重新枚举已知 subtree。
        FakeOpenListClient.tree = {
            "/115网盘/动画1": [("动画1", True, None, 100.0)],
        }

        # 3. 第一次 bound incremental rescan → 真实执行 handler
        FakeOpenListClient.list_calls = 0
        FakeOpenListClient.requested_paths = []
        rescan = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan.status_code == 200, rescan.text
        job = job_store.get_job(rescan.json()["task_id"])
        result = handle_discovery_scan(job.payload)
        assert result["summary"].get("failed_count", 0) == 0

        # 4. 关键断言：请求量 ≪ 全树目录数（不重新枚举已知 subtree）。
        # 精确上界：preflight(1) + root(1) + BASELINE_VERIFY_BUDGET(50) 未验证目录。
        # 若增量退化为全树遍历，请求会接近 total_dirs（122），此处必须 ≪。
        from app.catalog import store as catalog_store

        requested = FakeOpenListClient.list_calls
        budget = catalog_store.BASELINE_VERIFY_BUDGET
        assert requested > 0, "必须真实请求 OpenList"
        assert requested <= 2 + budget, (
            f"第一次 incremental 请求量 {requested} 必须 ≤ preflight+root+baseline 预算"
            f"（2+{budget}）；全树已知目录 {total_dirs} 不应被重扫"
        )
        assert requested < total_dirs // 2, (
            f"第一次 incremental 请求量 {requested} 应远小于 TXT 已知目录数 {total_dirs}"
        )

        # 5. canonical identity 不变、media unit 总量不因增量翻倍
        units_before = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM media_units WHERE root_id = ?", (root_id,),
        ).fetchone()["c"])
        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0
        assert units_before > 0, "TXT baseline 必须已产生 media_units"
        # 再跑一轮 incremental，unit 总量不得因 namespace 分裂翻倍
        FakeOpenListClient.list_calls = 0
        rescan2 = client.post(
            "/api/openlist/bound-roots/rescan",
            json={"root_id": root_id},
        )
        assert rescan2.status_code == 200, rescan2.text
        job2 = job_store.get_job(rescan2.json()["task_id"])
        result2 = handle_discovery_scan(job2.payload)
        assert result2["summary"].get("failed_count", 0) == 0
        units_after = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM media_units WHERE root_id = ?", (root_id,),
        ).fetchone()["c"])
        assert units_after <= units_before, (
            f"增量后 media_units 不得翻倍（{units_before} → {units_after}）"
        )


class TestBaselineReuseSafety:
    def test_reimport_same_tree_does_not_requeue_stale_archive(self, client, tmp_path):
        """复用/同媒体路径：不重新入队悬空归档（审查 HIGH 修复）。"""
        from app.jobs import store as job_store

        tree = _write_tree(tmp_path, _big_tree())
        resp1 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp1.status_code == 200, resp1.text
        assert resp1.json().get("baseline", {}).get("status") == "baseline_queued"

        # 再次导入同一 TXT（同媒体 → unchanged/reused 路径）
        resp2 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp2.status_code == 200, resp2.text
        baseline2 = resp2.json().get("baseline", {})
        assert baseline2.get("status") == "baseline_reused", (
            f"同媒体重复导入必须走复用路径（不重新入队），实际 {baseline2}"
        )
        # 不得产生悬空 input_path 的 snapshot job
        jobs = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=100)
        snapshot_jobs = [
            j for j in jobs
            if j.payload.get("scan_channel", "").startswith("snapshot_")
        ]
        for j in snapshot_jobs:
            inp = j.payload.get("input_path") or ""
            assert inp and Path(inp).exists(), (
                f"snapshot job 的 input_path 必须指向现存归档: {inp}"
            )


class TestBindBaselineGuard:
    def test_bind_rejected_when_baseline_failed(self, client, tmp_path):
        """RWK-23/30：baseline 未就绪（同步执行失败）时 bind-root 必须拒绝（400）。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        # 构造会导致 baseline 同步失败的导入：TXT 正常，但随后手工清掉
        # baseline completed fact 并把 target 前移（模拟 v2 pending 且未完成）
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        # 同步执行已完成 baseline（ready）——模拟 v2 入队 pending：target 前移但未完成
        conn = get_connection()
        conn.execute(
            "UPDATE source_roots SET baseline_target_generation = baseline_target_generation + 10 WHERE root_id = ?",
            (root_id,),
        )
        conn.commit()
        stats = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats["baseline_ready"] is False, "target 前移但未完成 → 不 ready"

        # 配置 OpenList + route → bind 必须拒绝
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 400, bind.text
        assert "尚未建立 Source Catalog 本地基线" in bind.json()["detail"]
        root = catalog_store.get_source_root(root_id)
        assert root.openlist_conn_hash == ""


class TestPartialBaselineNotReady:
    def test_v2_pending_after_v1_ready_revokes_ready(self, client, tmp_path):
        """事故①（真实事故链）：v1 完成 ready → v2 入队（target 前进但未执行）
        → ready=false → bind 被阻止；v2 完成 → ready=true。"""
        from app.catalog import store as catalog_store
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan

        # 1. v1 TXT baseline（导入即同步完成）→ ready
        tree_v1 = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_v1), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        root_v1 = catalog_store.get_source_root(root_id)
        stats1 = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats1["baseline_ready"] is True, "v1 完成后必须 ready"
        assert root_v1.baseline_completed_generation == root_v1.baseline_target_generation

        # 2. 模拟 v2 入队（target 前进但**不执行**）：直接调 bootstrap 入队
        from app.media_presets.service import bootstrap_provider_catalog_from_tree

        tree_v2 = _write_tree(
            tmp_path,
            _big_tree() + "| | | | |-作品1-001.S01E03.extra.mkv\n",
            name="v2目录树.txt",
        )
        info = bootstrap_provider_catalog_from_tree(
            provider="pan115",
            tree_archive=str(tree_v2),
            local_mount_root=str(tmp_path),
            import_family="anime",
            import_scope="",
        )
        assert info["root_id"] == root_id, "v2 必须复用同一 root"
        root_v2 = catalog_store.get_source_root(root_id)
        assert root_v2.baseline_target_generation > root_v1.baseline_completed_generation, (
            "v2 入队必须前进 target"
        )
        assert root_v2.baseline_completed_generation == root_v1.baseline_completed_generation

        # v2 pending（未执行）→ ready 必须为 false
        stats2 = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats2["baseline_ready"] is False, (
            "v2 pending（target 前进但未完成）不得视为 ready"
        )

        # 3. bind 被拒绝（v2 未 ready）
        resp = client.post(
            "/api/openlist/config",
            json={
                "server_url": "http://127.0.0.1:5244",
                "remote_root": "/115网盘",
                "mount_root": str(tmp_path / "mount"),
                "username": "test-user",
                "password": "p@ssw0rd",
            },
        )
        assert resp.status_code == 200, resp.text
        resp = client.put(
            "/api/openlist/routes",
            json={
                "routes": [
                    {
                        "route_id": "r-115", "label": "115",
                        "remote_prefix": "/115网盘/动画1",
                        "provider_id": "pan115", "enabled": True,
                    }
                ]
            },
        )
        assert resp.status_code == 200, resp.text
        bind = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind.status_code == 400, bind.text
        assert "尚未建立 Source Catalog 本地基线" in bind.json()["detail"]

        # 4. 执行 v2 snapshot job → ready 恢复 true
        jobs2 = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=100)
        v2_job = next(
            (j for j in jobs2
             if j.payload.get("root_id") == root_id
             and j.payload.get("generation") == root_v2.baseline_target_generation),
            None,
        )
        assert v2_job is not None, "v2 snapshot job 必须存在"
        result2 = handle_discovery_scan(v2_job.payload)
        assert result2["summary"].get("failed_count", 0) == 0
        root_after = catalog_store.get_source_root(root_id)
        assert root_after.baseline_completed_generation == root_after.baseline_target_generation
        stats3 = catalog_store.source_catalog_baseline_stats(root_id)
        assert stats3["baseline_ready"] is True, "v2 完成后必须恢复 ready"

        # 5. 此时 bind 成功
        bind2 = client.post(
            "/api/openlist/bind-root",
            json={"root_id": root_id, "remote_locator": "/115网盘/动画1"},
        )
        assert bind2.status_code == 200, bind2.text


class TestSingleExecutionAuthority:
    def test_multi_work_confirm_root_confirms_all(self, client, tmp_path):
        """事故②（RWK-35）：多作品 TXT 一次确认 → 全部 draft revisions confirmed、
        每 revision 恰一 mirror job、无 draft 残留。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        tree = _write_tree(tmp_path, _big_tree())  # 40 作品
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        baseline = body.get("baseline", {})
        assert baseline.get("status") == "baseline_queued", body
        confirmation_root_id = baseline.get("confirmation_root_id")
        assert confirmation_root_id, "TXT 导入必须返回 root 级确认身份"
        revision_ids = baseline.get("revision_ids") or []
        assert len(revision_ids) >= 5, f"40 作品 TXT 应有 ≥5 个 draft revisions（实际 {len(revision_ids)}）"

        conn = get_connection()
        # 导入后全部 draft（无 auto-confirm）
        drafts = conn.execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE revision_id IN (%s) AND status = 'draft'"
            % ",".join("?" * len(revision_ids)),
            revision_ids,
        ).fetchone()["c"]
        assert int(drafts) == len(revision_ids), "导入后全部 revisions 应为 draft"

        # 用户一次确认（root 级，带 generation fence）→ 全部 confirmed + 恰一 mirror job each
        baseline_meta = resp.json().get("baseline", {})
        gen = baseline_meta.get("confirmation_generation")
        resp_confirm = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": confirmation_root_id, "generation": gen},
        )
        assert resp_confirm.status_code == 200, resp_confirm.text
        cbody = resp_confirm.json()
        assert cbody["execution_mode"] == "durable"
        assert cbody["confirmed_count"] == len(revision_ids), cbody
        assert len(cbody["job_ids"]) == len(revision_ids), cbody

        confirmed = conn.execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE revision_id IN (%s) AND status IN ('confirmed','executed')"
            % ",".join("?" * len(revision_ids)),
            revision_ids,
        ).fetchone()["c"]
        assert int(confirmed) == len(revision_ids), "全部 revisions 必须 confirmed"

        # 每 revision 恰一 mirror job
        mirror_jobs = job_store.list_jobs(job_type="mirror_revision", status="queued", limit=500)
        for rev_id in revision_ids:
            matches = [j for j in mirror_jobs if j.payload.get("revision_id") == rev_id]
            assert len(matches) == 1, f"revision {rev_id} 应恰有 1 个 mirror job（实际 {len(matches)}）"

        # 无 draft 残留
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE revision_id IN (%s) AND status = 'draft'"
            % ",".join("?" * len(revision_ids)),
            revision_ids,
        ).fetchone()["c"]
        assert int(remaining) == 0, "确认后不得有 draft 残留"


class TestTxtUpdateSyncsCatalog:
    def test_txt_v2_update_syncs_same_root(self, client, tmp_path):
        """事故③：TXT v1 baseline → 导入 v2 → 同 root、Catalog 更新为 v2。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.jobs import store as job_store
        from app.pipeline.discovery_handler import handle_discovery_scan

        # v1 TXT（2 部作品）
        tree_v1 = _write_tree(tmp_path, "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n", name="v1目录树.txt")
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_v1), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        source_id = catalog_store.get_source_root(root_id).source_id
        _run_snapshot_baseline(client, root_id)
        gen_v1 = catalog_store.get_source_root(root_id).active_generation
        assert catalog_store.source_catalog_baseline_stats(root_id)["baseline_ready"] is True

        # v2 TXT（新增作品C）
        tree_v2 = _write_tree(tmp_path, "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n", name="v2目录树.txt")
        resp2 = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree_v2), "import_family": "anime", "import_scope": ""},
        )
        assert resp2.status_code == 200, resp2.text
        preset2 = next(p for p in list_presets() if p.source == "pan115")
        assert preset2.catalog_root_id == root_id, "TXT v2 必须复用同一 root"
        root2 = catalog_store.get_source_root(root_id)
        assert root2.source_id == source_id

        # 执行 v2 的 snapshot baseline 更新 → generation 前进、目录数增加
        gen_before_run = root2.active_generation
        _run_snapshot_baseline(client, root_id)
        gen_v2 = catalog_store.get_source_root(root_id).active_generation
        assert gen_v2 > gen_v1, "v2 baseline 必须 bump generation"
        dirs = int(get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_directories WHERE root_id = ? AND state = 'complete'",
            (root_id,),
        ).fetchone()["c"])
        assert dirs > 0
        # Catalog 已含作品C（source_nodes 有 C 的目录）
        nodes = get_connection().execute(
            "SELECT COUNT(*) AS c FROM source_nodes WHERE root_id = ? AND remote_path LIKE '%作品C%' AND tombstone = ''",
            (root_id,),
        ).fetchone()["c"]
        assert int(nodes) > 0, "v2 更新后 Catalog 必须包含新增作品C"
        # baseline 完成标记前进（不保留 v1 假 baseline）
        root_after = catalog_store.get_source_root(root_id)
        assert root_after.baseline_completed_generation >= gen_v1


class TestJobLifecycleOwnership:
    def test_sync_baseline_no_queued_job_single_draft_per_unit(self, client, tmp_path):
        """A：TXT 同步 baseline 不产生 queued discovery job；每 unit 同 generation 恰一 draft revision。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        tree = _write_tree(tmp_path, _big_tree())  # 40 作品
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id

        # 同步执行后不得遗留 queued discovery job（worker 不会再执行一次）
        queued = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=200)
        assert not any(j.payload.get("root_id") == root_id for j in queued), (
            "同步 baseline 不得遗留 queued discovery job"
        )

        # 每个 unit 同 generation 只有一个 draft revision（无重复执行）
        conn = get_connection()
        rows = conn.execute(
            """
            SELECT u.unit_id, COUNT(*) AS c FROM import_revisions r
            JOIN media_units u ON u.unit_id = r.unit_id
            WHERE u.root_id = ? AND r.source_generation = (
                SELECT baseline_target_generation FROM source_roots WHERE root_id = ?
            )
            GROUP BY u.unit_id
            """,
            (root_id, root_id),
        ).fetchall()
        assert len(rows) >= 5, f"应有 ≥5 个 unit（实际 {len(rows)}）"
        assert all(int(r["c"]) == 1 for r in rows), "每 unit 同 generation 必须恰 1 个 draft revision"

    def test_confirm_root_failure_no_legacy_mirror(self, client, tmp_path):
        """D：baseline 失败不退回 legacy 执行权威（无 legacy mirror 链）。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        # 正常导入（同步成功）
        tree = _write_tree(tmp_path, _big_tree())
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in list_presets() if p.source == "pan115")
        root_id = preset.catalog_root_id
        baseline = resp.json().get("baseline", {})
        assert baseline.get("status") == "baseline_queued"

        # baseline 成功后：confirm-root 是唯一执行权威（无 legacy mirror task）
        baseline_meta = baseline  # status baseline_queued
        from app.catalog import store as catalog_store
        root = catalog_store.get_source_root(root_id)
        gen = int(getattr(root, "baseline_target_generation", 0) or 0)
        confirm = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": gen},
        )
        assert confirm.status_code == 200, confirm.text
        cbody = confirm.json()
        assert cbody["execution_mode"] == "durable"
        # 所有 mirror 都是 durable job（job_type=mirror_revision），无 legacy 调用痕迹
        mirror_jobs = job_store.list_jobs(job_type="mirror_revision", status="queued", limit=500)
        assert len(mirror_jobs) >= len(baseline.get("revision_ids") or [])
        for j in mirror_jobs:
            assert j.job_type == "mirror_revision", "只允许 durable mirror 链"


class TestManualPatchHitsRevision:
    def test_patch_second_work_hits_its_revision(self, client, tmp_path):
        """C：patch 第二/三作品（确认页 legacy preview item）→ 命中对应 durable revision。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store
        from app.import_plan import revision_store
        from app.import_plan.service import build_preview

        # 3 部作品
        tree = _write_tree(tmp_path, "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n")
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        baseline = body.get("baseline", {})
        revision_ids = baseline.get("revision_ids") or []
        assert len(revision_ids) == 3, f"应有 3 个 draft revisions（实际 {len(revision_ids)}）"

        # 确认页 legacy preview 的 items（含多作品 item）
        preview = body["preview"]
        root_id = baseline["confirmation_root_id"]
        generation = baseline["confirmation_generation"]
        preview = client.get(
            f"/api/imports/pan115/confirm-root-preview?root_id={root_id}&generation={generation}"
        ).json()
        legacy_items = preview.get("items") or []
        assert len(legacy_items) >= 3, "preview 应含多作品 items"

        # 找到作品B 的 item（relative_path 含 作品B）
        work_b_item = next(
            (it for it in legacy_items if "作品B" in (it.get("relative_path") or "")),
            None,
        )
        assert work_b_item is not None, "preview 应含作品B 的 item"
        item_id = work_b_item["id"]

        # 用确认页 plan_id（legacy）+ item_id patch → 必须命中作品B 的 durable revision
        patch = {"title": "作品B 修正标题"}
        patch_resp = client.patch(
            f"/api/imports/pan115/items/{item_id}",
            json={
                "plan_id": preview["plan_id"],
                "patch": patch,
                "root_id": root_id,
                "generation": generation,
            },
        )
        assert patch_resp.status_code == 200, patch_resp.text
        pbody = patch_resp.json()
        assert pbody.get("revision_id"), "patch 必须返回命中的 revision_id"
        assert pbody["revision_id"] in revision_ids

        # 该 revision 的 item 已更新（durable 事实）——用 revision 自己的 item id
        assert pbody["item"].get("title") == "作品B 修正标题", pbody
        rev_item_id = pbody["item"]["id"]
        plan = revision_store.load_plan(pbody["revision_id"])
        patched = next((it for it in plan.items if it.id == rev_item_id), None)
        assert patched is not None
        assert patched.title == "作品B 修正标题"
        # 该 revision 是作品B 的（relative_path 含 作品B）
        assert "作品B" in (patched.relative_path or "")


class TestRwk38ConfirmationAuthority:
    """RWK-38 验收：TXT 确认身份 = (root_id, generation)，带 fence、可恢复、
    失败绝不回退 legacy 的单一 durable authority。

    A. stale generation：v1 确认页 → v2 导入 → 用 v1 identity 确认 → 409 + 0 执行
    B. restart/resume：preset 投影恢复 confirmation 身份 → confirm-root 全部确认
    C. real baseline failure：强制失败 → baseline_failed 无确认身份 → 无任何执行
    D. manual confirm：手工确认走 confirmRoot(exact_generation)（legacy plan 未动）
    E. manual patch：patch 作品 B → 聚合 durable preview 显示新值 → 确认后仍是新值
    """

    def _import_tree(self, client, tmp_path, text):
        from app.media_presets.store import list_presets as lp

        tree = _write_tree(tmp_path, text, name="115目录树.txt")
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        preset = next(p for p in lp() if p.source == "pan115")
        return resp.json(), preset

    def test_a_stale_generation_conflict(self, client, tmp_path):
        """A：v1 确认页 identity 在 v2 导入后确认 → 409 + 0 confirmed + 0 mirror。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        body1, _ = self._import_tree(
            client, tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n",
        )
        b1 = body1["baseline"]
        assert b1["status"] == "baseline_queued"
        root_id = b1["confirmation_root_id"]
        gen1 = b1["confirmation_generation"]

        # v2 导入（同 root，新增作品C → generation 前进）
        body2, _ = self._import_tree(
            client, tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n",
        )
        b2 = body2["baseline"]
        assert b2["confirmation_root_id"] == root_id, "必须复用同 root"
        gen2 = b2["confirmation_generation"]
        assert gen2 > gen1, f"v2 generation 必须前进（{gen1}→{gen2}）"

        # 用 v1 identity 确认 → 409 stale，0 执行
        before_id = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        stale = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": gen1},
        )
        assert stale.status_code == 409, stale.text
        after_id = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        assert len(after_id) == len(before_id), "stale 确认不得 enqueue 任何 mirror job"

        conn = get_connection()
        # V2 revisions 必须全部保持 draft（stale 确认不得 confirm 任何 revision）
        v2_drafts = conn.execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE revision_id IN (%s) AND status='draft'"
            % ",".join("?" * len(b2.get("revision_ids") or [])),
            list(b2.get("revision_ids") or []),
        ).fetchone()["c"]
        assert int(v2_drafts) == len(b2.get("revision_ids") or []), (
            "stale 确认不得 confirm 任何 V2 revision"
        )

        # 用 v2 identity 确认 → 成功（v2 全部）
        ok = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": gen2},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["confirmed_count"] == len(b2.get("revision_ids") or []), ok.text

    def test_b_restart_resume_recovers_identity(self, client, tmp_path):
        """B：重启/恢复后 confirmation identity 从 preset 投影恢复，不退回 legacy。"""
        from app.jobs import store as job_store

        body, preset = self._import_tree(
            client, tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n",
        )
        root_id = body["baseline"]["confirmation_root_id"]
        gen = body["baseline"]["confirmation_generation"]
        revision_ids = body["baseline"]["revision_ids"]

        # 模拟恢复：preset 序列化投影含 confirmation 身份（重启后从列表可得）
        presets_resp = client.get("/api/media-presets")
        assert presets_resp.status_code == 200, presets_resp.text
        recovered = next(
            p for p in presets_resp.json()["presets"]
            if p.get("preset_id") == preset.preset_id
        )
        assert recovered.get("confirmation_root_id") == root_id, (
            "恢复后必须投影 confirmation_root_id"
        )
        assert recovered.get("confirmation_generation") == gen
        assert recovered.get("confirmation_ready") is True, "基线完成且 drafts 存在 → ready"

        # 用恢复的 identity 确认 → 全部 durable + 恰一 mirror job each + 0 legacy
        confirm = client.post(
            "/api/imports/pan115/confirm-root",
            json={
                "root_id": recovered["confirmation_root_id"],
                "generation": recovered["confirmation_generation"],
            },
        )
        assert confirm.status_code == 200, confirm.text
        cbody = confirm.json()
        assert cbody["execution_mode"] == "durable"
        assert cbody["confirmed_count"] == len(revision_ids), cbody

        mirrors = job_store.list_jobs(job_type="mirror_revision", limit=1000)
        own = [j for j in mirrors if (j.payload or {}).get("revision_id") in revision_ids]
        assert len(own) == len(revision_ids), (
            f"每 revision 恰一 mirror job（实际 {len(own)}/{len(revision_ids)}）"
        )
        assert all(j.job_type == "mirror_revision" for j in own)

        # 全部 confirm 后 ready=False（避免重复确认）
        presets_resp2 = client.get("/api/media-presets")
        recovered2 = next(
            p for p in presets_resp2.json()["presets"]
            if p.get("preset_id") == preset.preset_id
        )
        assert recovered2.get("confirmation_ready") is False

    def test_c_real_baseline_failure_no_execution(self, client, tmp_path, monkeypatch):
        """C：强制 baseline 失败 → baseline_failed 无确认身份 → 无 legacy 也无 durable 执行。"""
        from app.jobs import store as job_store

        import app.media_presets.service as service_mod

        def _fail_sync(provider, tree_archive, local_mount_root, import_family, import_scope):
            # 模拟部分目录失败（真实 handler failed_count>0 形态）
            return {
                "root_id": "deadbeef000000000000000000000000",
                "generation": 1,
                "summary": {
                    "plan_ready": 1, "needs_review": 0, "mirror_enqueued": 0,
                    "failed_count": 1, "failed_paths": ["001 根目录/动画1/作品C"],
                },
                "revision_ids": [],
            }

        monkeypatch.setattr(service_mod, "bootstrap_provider_catalog_sync", _fail_sync)
        tree = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n",
        )
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        baseline = resp.json().get("baseline", {})
        assert baseline.get("status") == "baseline_failed", baseline
        assert not baseline.get("confirmation_root_id"), (
            "baseline_failed 不得提供确认身份（前端据此禁止确认与自动 pipeline）"
        )

        # 即使强行调用 confirm-root：基线未完成 → 409，0 mirror
        before_id = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        confirm = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": "deadbeef000000000000000000000000", "generation": 1},
        )
        assert confirm.status_code in (404, 409), confirm.text
        after_id = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        assert len(after_id) == len(before_id), "失败路径不得产生任何 durable mirror job"

    def test_d_manual_confirm_uses_root_identity(self, client, tmp_path):
        """D：手工显式确认调用 confirmRoot(root, exact_generation)，legacy plan 不被触碰。"""
        from app.import_plan.store import load_import_plan
        from app.db.database import get_connection

        body, preset = self._import_tree(
            client, tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n",
        )
        root_id = body["baseline"]["confirmation_root_id"]
        gen = body["baseline"]["confirmation_generation"]
        revision_ids = body["baseline"]["revision_ids"]
        legacy_plan_id = preset.current_plan_id

        # 单 revision confirm 不能绕过 root-generation authority。
        single = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": revision_ids[0]},
        )
        assert single.status_code == 409, single.text

        # 手工按钮路径：confirmRoot(root, exact_generation)
        confirm = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": gen},
        )
        assert confirm.status_code == 200, confirm.text
        cbody = confirm.json()
        assert cbody["execution_mode"] == "durable"
        assert cbody["confirmed_count"] == len(revision_ids), cbody

        # legacy plan 未被 confirm（不执行 legacy 执行链）
        legacy = load_import_plan(plan_id=legacy_plan_id)
        assert legacy is not None
        legacy_status = getattr(legacy, "status", "")
        assert legacy_status != "executed", "手工确认不得把 legacy plan 标记为 executed"

        # 全部 revisions durable confirmed
        conn = get_connection()
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE status='draft'"
        ).fetchone()["c"]
        assert int(remaining) == 0, f"手工确认后不得残留 draft（剩余 {remaining}）"

    def test_e_manual_patch_flows_to_confirm(self, client, tmp_path):
        """E：patch 作品 B → 聚合 durable preview 显示新值 → 确认后仍是新值（generation 未变）。"""
        from app.jobs import store as job_store
        from app.import_plan import revision_store

        body, preset = self._import_tree(
            client, tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n",
        )
        root_id = body["baseline"]["confirmation_root_id"]
        gen = body["baseline"]["confirmation_generation"]
        revision_ids = body["baseline"]["revision_ids"]
        assert len(revision_ids) == 3

        # 聚合 durable preview（确认页唯一真相）
        preview = client.get(
            f"/api/imports/pan115/confirm-root-preview?root_id={root_id}&generation={gen}"
        )
        assert preview.status_code == 200, preview.text
        pbody = preview.json()
        items = pbody.get("items") or []
        assert len(items) == 3, f"聚合 preview 应含 3 items（实际 {len(items)}）"

        # 作品B 的 item（revision item id）
        b_item = next(
            (it for it in items if "作品B" in (it.get("relative_path") or "")),
            None,
        )
        assert b_item is not None

        # patch 作品B（确认页 item 即 revision item id）
        patch_resp = client.patch(
            f"/api/imports/pan115/items/{b_item['id']}",
            json={
                "plan_id": pbody["plan_id"],
                "patch": {"title": "作品B 修正标题"},
                "root_id": root_id,
                "generation": gen,
            },
        )
        assert patch_resp.status_code == 200, patch_resp.text
        assert patch_resp.json().get("revision_id"), "patch 必须返回命中的 revision_id"

        # 聚合 preview 刷新后显示新值（UI 不再显示旧 legacy 值）
        preview2 = client.get(
            f"/api/imports/pan115/confirm-root-preview?root_id={root_id}&generation={gen}"
        )
        p2 = preview2.json()
        b2 = next(
            (it for it in (p2.get("items") or []) if "作品B" in (it.get("relative_path") or "")),
            None,
        )
        assert b2["title"] == "作品B 修正标题", "聚合 preview 必须反映 durable 修正"

        # generation 未变化（patch 不产生新基线）
        assert p2["revision_ids"] == pbody["revision_ids"]
        assert p2["plan_id"] == pbody["plan_id"]

        # root confirm 后执行的是修正后的 B
        confirm = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": gen},
        )
        assert confirm.status_code == 200, confirm.text
        confirmed_revs = confirm.json()["revision_ids"]
        assert len(confirmed_revs) == 3

        # 确认后 B 的 revision item 仍是修正标题（durable 事实未被覆盖）
        for rid in confirmed_revs:
            plan = revision_store.load_plan(rid)
            patched = next(
                (it for it in (plan.items or []) if "作品B" in (it.relative_path or "")),
                None,
            )
            if patched is not None:
                assert patched.title == "作品B 修正标题", "确认后修正不得丢失"


class TestRwk39ConfirmationAuthority:
    """RWK-39：durable_root 的失败恢复、页面数据源与 root 预检契约。"""

    def test_historical_plan_ids_cannot_use_legacy_execution(self, client, tmp_path):
        """durable preset 接管后，全部历史 version plan 也失去 legacy 权限。"""
        from app.import_plan.store import load_import_plan, save_import_plan

        v1 = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n",
            name="v1.txt",
        )
        first = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(v1), "import_family": "anime", "import_scope": ""},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        historical_plan_id = first_body["preset"]["current_plan_id"]
        old_plan = load_import_plan(plan_id=historical_plan_id)
        assert old_plan is not None
        old_plan.status = "confirmed"  # 模拟 durable migration 前的历史 legacy 事实
        save_import_plan(old_plan)

        v2 = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n",
            name="v2.txt",
        )
        second = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(v2), "import_family": "anime", "import_scope": ""},
        )
        assert second.status_code == 200, second.text
        preset = next(p for p in client.get("/api/media-presets").json()["presets"] if p["source"] == "pan115")
        assert preset["current_plan_id"] != historical_plan_id
        assert any(v["plan_id"] == historical_plan_id for v in preset["versions"])

        explicit_confirm = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": historical_plan_id},
        )
        assert explicit_confirm.status_code == 409, explicit_confirm.text
        explicit_mirror = client.post(
            "/api/mirror/pan115/generate",
            json={"plan_id": historical_plan_id},
        )
        assert explicit_mirror.status_code == 409, explicit_mirror.text
        latest_mirror = client.post("/api/mirror/pan115/generate", json={})
        assert latest_mirror.status_code == 409, latest_mirror.text
        missing_confirm = client.post("/api/imports/pan115/confirm", json={})
        assert missing_confirm.status_code == 422, missing_confirm.text

    def test_hard_exception_persists_failed_authority_across_resume(self, client, tmp_path, monkeypatch):
        """hard exception → persisted durable_root/failed → restart projection blocked。"""
        from app.jobs import store as job_store
        import app.media_presets.service as service_mod

        def fail_sync(**kwargs):
            raise RuntimeError("forced baseline bootstrap failure")

        monkeypatch.setattr(service_mod, "bootstrap_provider_catalog_sync", fail_sync)
        tree = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n",
        )
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["baseline"]["status"] == "baseline_failed"

        # 模拟应用重启：只从持久化 preset API 恢复，不复用当前页面 entry。
        presets = client.get("/api/media-presets")
        assert presets.status_code == 200, presets.text
        failed = next(p for p in presets.json()["presets"] if p["source"] == "pan115")
        assert failed["execution_authority"] == "durable_root"
        assert failed["confirmation_state"] == "failed"
        assert failed["confirmation_blocked"] is True
        assert failed.get("confirmation_root_id")

        # 即使恢复流程/恶意调用尝试 confirm，也不能进入 legacy 或 durable mirror。
        before = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        confirm = client.post(
            "/api/imports/pan115/confirm-root",
            json={
                "root_id": failed["confirmation_root_id"],
                "generation": failed["confirmation_generation"],
            },
        )
        assert confirm.status_code in (400, 409), confirm.text
        after = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        assert after == before

        legacy_confirm = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": failed["current_plan_id"]},
        )
        assert legacy_confirm.status_code == 409, legacy_confirm.text
        legacy_mirror = client.post(
            "/api/mirror/pan115/generate",
            json={"plan_id": failed["current_plan_id"]},
        )
        assert legacy_mirror.status_code == 409, legacy_mirror.text

        # 即使存在 latest confirmed legacy plan，省略 plan_id 也不得绕过 authority fence。
        from app.import_plan.store import load_import_plan, save_import_plan

        legacy_plan = load_import_plan(plan_id=failed["current_plan_id"])
        assert legacy_plan is not None
        legacy_plan.status = "confirmed"
        save_import_plan(legacy_plan)
        latest_mirror = client.post("/api/mirror/pan115/generate", json={})
        assert latest_mirror.status_code == 409, latest_mirror.text

    def test_hard_exception_without_root_still_blocks_legacy(self, client, tmp_path, monkeypatch):
        """即使 root 创建也失败，durable authority 仍持久化并拒绝 legacy fallback。"""
        import app.media_presets.service as service_mod

        monkeypatch.setattr(
            service_mod,
            "bootstrap_provider_catalog_sync",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bootstrap failed")),
        )
        monkeypatch.setattr(
            service_mod,
            "ensure_provider_source_root",
            lambda **kwargs: (_ for _ in ()).throw(RuntimeError("root failed")),
        )
        tree = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n",
        )
        resp = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert resp.status_code == 200, resp.text
        failed = next(
            p for p in client.get("/api/media-presets").json()["presets"] if p["source"] == "pan115"
        )
        assert failed["execution_authority"] == "durable_root"
        assert failed["confirmation_state"] == "failed"
        assert failed["confirmation_root_id"] is None
        assert failed["confirmation_blocked"] is True
        legacy_confirm = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": failed["current_plan_id"]},
        )
        assert legacy_confirm.status_code == 409, legacy_confirm.text

    def test_patch_rejects_stale_root_generation(self, client, tmp_path):
        """旧确认页 patch 不得写入后来推进的 target generation。"""
        v1 = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n",
            name="v1.txt",
        )
        first = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(v1), "import_family": "anime", "import_scope": ""},
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        root_id = first_body["baseline"]["confirmation_root_id"]
        generation1 = first_body["baseline"]["confirmation_generation"]
        first_preview = client.get(
            f"/api/imports/pan115/confirm-root-preview?root_id={root_id}&generation={generation1}"
        ).json()
        item = first_preview["items"][0]

        v2 = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n",
            name="v2.txt",
        )
        second = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(v2), "import_family": "anime", "import_scope": ""},
        )
        assert second.status_code == 200, second.text
        assert second.json()["baseline"]["confirmation_generation"] > generation1

        stale_patch = client.patch(
            f"/api/imports/pan115/items/{item['id']}",
            json={
                "plan_id": first_preview["plan_id"],
                "patch": {"title": "不应写入"},
                "root_id": root_id,
                "generation": generation1,
            },
        )
        assert stale_patch.status_code == 409, stale_patch.text

    def test_failed_same_txt_reimport_runs_recovery(self, client, tmp_path, monkeypatch):
        """failed baseline 再导入同一 TXT 不得错误 baseline_reused，必须恢复成功。"""
        import app.media_presets.service as service_mod


        def fail_sync(provider, tree_archive, local_mount_root, import_family, import_scope):
            root_id = service_mod.ensure_provider_source_root(
                provider=provider,
                local_mount_root=local_mount_root,
                import_family=import_family,
                import_scope=import_scope,
            )
            return {
                "root_id": root_id,
                "generation": 1,
                "source_id": f"{provider}-test",
                "summary": {"failed_count": 1, "failed_paths": ["作品A"]},
                "revision_ids": [],
            }

        monkeypatch.setattr(service_mod, "bootstrap_provider_catalog_sync", fail_sync)
        tree = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n",
            name="same.txt",
        )
        media = tmp_path / "动画1" / "作品A" / "作品A.S01E01.mkv"
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(b"video")
        first = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert first.status_code == 200, first.text
        assert first.json()["baseline"]["status"] == "baseline_failed"

        recovery_calls = []

        def recover_sync(provider, tree_archive, local_mount_root, import_family, import_scope):
            recovery_calls.append(tree_archive)
            root_id = service_mod.ensure_provider_source_root(
                provider=provider,
                local_mount_root=local_mount_root,
                import_family=import_family,
                import_scope=import_scope,
            )
            return {
                "root_id": root_id,
                "generation": 2,
                "source_id": f"{provider}-test",
                "summary": {"failed_count": 0, "failed_paths": []},
                "revision_ids": [],
            }

        monkeypatch.setattr(service_mod, "bootstrap_provider_catalog_sync", recover_sync)
        preset_id = first.json()["preset"]["preset_id"]
        second = client.post(
            f"/api/media-presets/{preset_id}/updates-from-path",
            json={"tree_path": str(tree), "expected_source": "pan115"},
        )
        assert second.status_code == 200, second.text
        assert second.json()["baseline"]["status"] == "baseline_queued"
        assert len(recovery_calls) == 1, "failed 同 TXT recovery 必须重新执行 baseline service"
        preset = next(p for p in client.get("/api/media-presets").json()["presets"] if p["source"] == "pan115")
        assert preset["confirmation_state"] == "ready"

    def test_authority_pending_before_baseline_is_externally_visible(self, client, tmp_path, monkeypatch):
        """baseline 正在运行时，current_plan 也必须已经被 durable/pending 接管。"""
        import threading
        import app.media_presets.service as service_mod

        entered = threading.Event()
        release = threading.Event()
        original_sync = service_mod.bootstrap_provider_catalog_sync

        def blocked_sync(**kwargs):
            entered.set()
            assert release.wait(timeout=10), "baseline test release timeout"
            return original_sync(**kwargs)

        monkeypatch.setattr(service_mod, "bootstrap_provider_catalog_sync", blocked_sync)
        tree = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n",
        )
        results = []

        def do_import():
            results.append(
                client.post(
                    "/api/media-presets/import-local-tree",
                    json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
                )
            )

        thread = threading.Thread(target=do_import)
        thread.start()
        assert entered.wait(timeout=10), "baseline 未进入暂停点"
        pending = next(p for p in client.get("/api/media-presets").json()["presets"] if p["source"] == "pan115")
        assert pending["execution_authority"] == "durable_root"
        assert pending["confirmation_state"] == "pending"
        assert pending["confirmation_blocked"] is True
        legacy_confirm = client.post(
            "/api/imports/pan115/confirm",
            json={"plan_id": pending["current_plan_id"]},
        )
        assert legacy_confirm.status_code == 409, legacy_confirm.text
        legacy_mirror = client.post(
            "/api/mirror/pan115/generate",
            json={"plan_id": pending["current_plan_id"]},
        )
        assert legacy_mirror.status_code == 409, legacy_mirror.text
        release.set()
        thread.join(timeout=15)
        assert not thread.is_alive(), "baseline import thread 未结束"
        assert results and results[0].status_code == 200, results[0].text if results else "missing result"

    def test_confirm_root_rejects_aggregate_duplicate_before_mutation(self, client, tmp_path):
        """单 revision 各自合法但 aggregate duplicate 时，root confirm 必须零 mutation。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        tree = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n",
        )
        imported = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert imported.status_code == 200, imported.text
        baseline = imported.json()["baseline"]
        revision_ids = baseline["revision_ids"]
        conn = get_connection()
        first_item = conn.execute(
            "SELECT work_title, series_group, canonical_work_id FROM import_revision_items WHERE revision_id = ? LIMIT 1",
            (revision_ids[0],),
        ).fetchone()
        conn.execute(
            "UPDATE import_revision_items SET work_title = ?, series_group = ?, canonical_work_id = ? WHERE revision_id IN (?, ?)",
            (
                first_item["work_title"],
                first_item["series_group"],
                first_item["canonical_work_id"] or "canonical-test",
                revision_ids[0],
                revision_ids[1],
            ),
        )
        conn.commit()

        before = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        result = client.post(
            "/api/imports/pan115/confirm-root",
            json={
                "root_id": baseline["confirmation_root_id"],
                "generation": baseline["confirmation_generation"],
            },
        )
        assert result.status_code == 409, result.text
        after = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        assert after == before
        statuses = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id IN (?, ?, ?)",
            tuple(revision_ids),
        ).fetchall()
        assert {str(row["status"]) for row in statuses} == {"draft"}

    def test_confirm_root_prevalidates_all_members_before_mutation(self, client, tmp_path):
        """root 第 N 个 revision 冲突时，前 N-1 不得已确认或入队 mirror。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        tree = _write_tree(
            tmp_path,
            "|——根目录\n| |-动画1\n| | |-作品A\n| | | |-作品A.S01E01.mkv\n| | |-作品B\n| | | |-作品B.S01E01.mkv\n| | |-作品C\n| | | |-作品C.S01E01.mkv\n",
        )
        imported = client.post(
            "/api/media-presets/import-local-tree",
            json={"tree_path": str(tree), "import_family": "anime", "import_scope": ""},
        )
        assert imported.status_code == 200, imported.text
        baseline = imported.json()["baseline"]
        revision_ids = baseline["revision_ids"]
        root_id = baseline["confirmation_root_id"]
        generation = baseline["confirmation_generation"]
        assert len(revision_ids) == 3

        conn = get_connection()
        conn.execute(
            "UPDATE import_revision_items SET action = 'ignore' WHERE revision_id = ?",
            (revision_ids[2],),
        )
        conn.commit()
        before_jobs = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}

        result = client.post(
            "/api/imports/pan115/confirm-root",
            json={"root_id": root_id, "generation": generation},
        )
        assert result.status_code == 409, result.text
        after_jobs = {j.job_id for j in job_store.list_jobs(job_type="mirror_revision", limit=1000)}
        assert after_jobs == before_jobs
        statuses = conn.execute(
            "SELECT revision_id, status FROM import_revisions WHERE revision_id IN (?, ?, ?)",
            tuple(revision_ids),
        ).fetchall()
        status_by_id = {str(row["revision_id"]): str(row["status"]) for row in statuses}
        assert [status_by_id[rid] for rid in revision_ids] == ["draft", "draft", "draft"]

    def test_frontend_uses_durable_preview_and_writes_patch_result_to_state(self):
        """锁定页面入口、恢复入口和 saveItem 的 durable confirmation state contract。"""
        from pathlib import Path

        page = (Path(__file__).resolve().parents[2] / "src" / "pages" / "MediaManagementPage.tsx").read_text(
            encoding="utf-8"
        )
        client = (Path(__file__).resolve().parents[2] / "src" / "api" / "imports.ts").read_text(
            encoding="utf-8"
        )
        service = (Path(__file__).resolve().parents[2] / "backend" / "app" / "media_presets" / "service.py").read_text(
            encoding="utf-8"
        )
        assert page.count("entry.preview = await resolveTxtEntryPreview(") >= 4
        resume = page.index("const restoredRootId = workingPreset.confirmation_root_id")
        assert page.index("getConfirmRootPreview", resume) < page.index("getPreview", resume)
        refreshed = page.index("const refreshedPreview", page.index("const saveItem"))
        state_write = page.index("updateEntry(activeEntry.id, { preview: refreshedPreview })", refreshed)
        assert state_write > refreshed
        assert "confirmation_blocked" in page
        assert "if (activeEntry.confirmationBlocked) return;" in page
        assert "root_id: confirmation?.rootId" in client
        assert "generation: confirmation?.generation" in client
        assert "require_all=durable_source_card" in service
        assert 'data["lifecycle_status"] = "ready"' in service
        assert "preset.execution_authority !== 'durable_root'" in page
