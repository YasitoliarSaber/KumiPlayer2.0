"""专项大模块返工 CP8：干净 HEAD 的身份隔离与恢复链回归。

规划员返工强制回归（本文件在仅含 GitHub 已提交代码的干净 HEAD 上执行）：

1. A/B 同 series_group、仅 B 有 scrape metadata → A 绝不能拿到 B 的
   poster/fanart/title（canonical 身份隔离，禁止跨边界复用投影资料）。
2. 一个 revision 中两个 standalone canonical 即使普通 work_id 相同，
   两条 artifact path 也必须分别归属正确 canonical（path 精确匹配，
   绝不取 revision 第一条 canonical）。
3. Work A scrape failed，同时 Work B 是全局最新 scrape job → 点击 A 的
   retry 只能重试 A（按 revision 精确归属，不读 scrape:global 最近一条）。
4. 重复点击同一失败 unit 不产生重复业务任务（rerun_terminal_job 同行重入队）。
5. OpenList 来源卡首次导入立即可见、同 root 不重复、无空 current_plan_id
   对应的“继续处理”死入口、可触发增量扫描。
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.integrations.openlist.client import join_remote_path, normalize_remote_path
from app.integrations.openlist.models import OpenListEntry
from app.main import app

REMOTE_ROOT = "/夸克网盘"
TREE = {
    "/夸克网盘": [("动画", True, None, None)],
    "/夸克网盘/动画": [("冰菓", True, None, None), ("蜂蜜与四叶草", True, None, None)],
    "/夸克网盘/动画/冰菓": [
        ("冰菓 - 01.mkv", False, 100, 1700000000),
        ("冰菓 - 02.mkv", False, 200, 1700000001),
    ],
    "/夸克网盘/动画/蜂蜜与四叶草": [
        ("蜂蜜与四叶草 - 01.mkv", False, 100, 1700000002),
    ],
}


class FakeOpenListClient:
    """替换 app.api.openlist.OpenListClient 的假客户端（全程离线）。"""

    instances = []
    login_user = ""
    tree = TREE

    def __init__(self, server_url, username, password, **kwargs):
        self.server_url = server_url
        self.username = username
        self.password = password
        self.calls = []
        FakeOpenListClient.instances.append(self)

    def login(self):
        if self.username == FakeOpenListClient.login_user:
            from app.integrations.openlist.models import OpenListAuthError
            raise OpenListAuthError()
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


def _make_local_mount(tmp_path: Path) -> Path:
    root = tmp_path / "quark" / "动画"
    (root / "冰菓").mkdir(parents=True)
    (root / "冰菓" / "冰菓 - 01.mkv").write_bytes(b"1")
    (root / "冰菓" / "冰菓 - 02.mkv").write_bytes(b"2")
    (root / "蜂蜜与四叶草").mkdir(parents=True)
    (root / "蜂蜜与四叶草" / "蜂蜜与四叶草 - 01.mkv").write_bytes(b"3")
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


def _make_unit(unit_id: str, root_id: str = "root-x", boundary: str = "/动画/作品") -> None:
    from app.catalog import store
    from app.db.database import get_connection

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
    """构造 revision item（series_group 故意相同，work_id 共享，canonical 可覆盖）。"""
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


@pytest.fixture(autouse=True)
def fake_client(monkeypatch):
    FakeOpenListClient.instances = []
    FakeOpenListClient.login_user = ""
    FakeOpenListClient.tree = TREE
    monkeypatch.setattr("app.api.openlist.OpenListClient", FakeOpenListClient)
    monkeypatch.setattr(
        "app.integrations.openlist.connection.OpenListClient", FakeOpenListClient
    )
    yield


@pytest.fixture
def client():
    return TestClient(app)


class TestCrossCanonicalMetadataIsolation:
    """A/B 同 series_group，仅 B 有 metadata → A 不得跨边界借 B 的投影资料。"""

    def test_work_a_never_borrows_work_b_metadata(self, tmp_path):
        """只要 item 有 canonical_work_id，Library 绝不通过 series_group 兜底借料。"""
        from app.import_plan import revision_store
        from app.library.index import build_library_index
        from app.scrape.models import ScrapeMap, ScrapeMapItem

        _make_unit("unit-cp8-a", boundary="/动画/作品A")
        _make_unit("unit-cp8-b", boundary="/动画/作品B")
        rev = revision_store.create_revision(
            unit_id="unit-cp8-a", source_generation=1,
            items=[
                _item("作品A/Season 1/a.mkv", canonical_work_id="unit:unit-cp8-a:main"),
                _item("作品B/Season 1/b.mkv", canonical_work_id="unit:unit-cp8-b:main"),
            ],
            status="confirmed",
        )
        plan = revision_store.load_plan(rev["revision_id"])
        poster_b = tmp_path / "poster-b.jpg"
        fanart_b = tmp_path / "fanart-b.jpg"
        poster_b.write_bytes(b"x")
        fanart_b.write_bytes(b"x")
        scrape_map = ScrapeMap(items=[
            ScrapeMapItem(
                scrape_target_id="t-b", source="openlist",
                import_plan_id=rev["revision_id"],
                work_id="w", canonical_work_id="unit:unit-cp8-b:main",
                card_type="main_series", media_type="tv",
                series_group="SAME_SERIES_GROUP", local_title="作品B",
                scrape_title="作品B", tmdb_id=1002, tmdb_type="tv",
                poster_path=str(poster_b), fanart_path=str(fanart_b),
            ),
        ])
        index = build_library_index(plan, scrape_map=scrape_map)
        works = {work.work_id: work for work in index.works}
        work_a = works.get("unit:unit-cp8-a:main")
        work_b = works.get("unit:unit-cp8-b:main")
        assert work_a is not None and work_b is not None
        # A 不得拿到 B 的 poster/fanart/title
        assert not work_a.poster_path
        assert not work_a.fanart_path
        assert work_a.title != "作品B"
        # B 正常拿到自己的投影资料
        assert work_b.poster_path == str(poster_b)
        assert work_b.fanart_path == str(fanart_b)


class TestArtifactExactAttribution:
    """同 revision 多 standalone canonical 共享 work_id → path 精确归属，绝不取第一条。"""

    def test_two_standalone_same_work_id_path_exact(self):
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.pipeline.artifacts import upsert_artifact

        _make_unit("unit-cp8-art", boundary="/动画/合集")
        rev = revision_store.create_revision(
            unit_id="unit-cp8-art", source_generation=1,
            items=[
                _item("剧集/剧场版A/a.mkv", canonical_work_id="unit:unit-cp8-art:sub:A"),
                _item("剧集/剧场版B/b.mkv", canonical_work_id="unit:unit-cp8-art:sub:B"),
            ],
            status="confirmed",
        )
        revision_id = rev["revision_id"]
        strm_a = "H:/mirror/openlist/剧集/剧场版A/a.strm"
        strm_b = "H:/mirror/openlist/剧集/剧场版B/b.strm"
        # 模拟 handlers._register_artifacts：只传 item 级 work_id（两者共享 "w"）
        upsert_artifact(kind="strm", path=strm_a, revision_id=revision_id, work_id="w")
        upsert_artifact(kind="strm", path=strm_b, revision_id=revision_id, work_id="w")
        conn = get_connection()
        attrs = {
            row["path"]: row["work_id"]
            for row in conn.execute(
                "SELECT work_id, path FROM artifact_records ORDER BY path"
            ).fetchall()
        }
        assert attrs[strm_a] == "unit:unit-cp8-art:sub:A"
        assert attrs[strm_b] == "unit:unit-cp8-art:sub:B"


class TestRerunTerminalIdempotent:
    """终态 job 重入队复用同一行，重复调用幂等，不新建业务任务。"""

    def test_rerun_terminal_job_same_row(self):
        from app.db.database import get_connection
        from app.jobs import store as job_store

        job = job_store.create_job(
            job_type="mirror_revision", resource_key="mirror:r1",
            payload={"revision_id": "r1"},
        )
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET status='failed', error='模拟失败' WHERE job_id=?",
            (job.job_id,),
        )
        conn.commit()

        assert job_store.rerun_terminal_job(job.job_id)
        row = conn.execute(
            "SELECT status, attempt FROM jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()
        assert row["status"] == "queued"
        assert row["attempt"] == 1

        # 重复调用幂等：已 queued，不动作
        assert not job_store.rerun_terminal_job(job.job_id)
        row2 = conn.execute(
            "SELECT status, attempt FROM jobs WHERE job_id=?", (job.job_id,)
        ).fetchone()
        assert row2["status"] == "queued"
        assert row2["attempt"] == 1

        # 同 resource_key 始终只有一行
        n = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type='mirror_revision' AND resource_key='mirror:r1'"
        ).fetchone()[0]
        assert n == 1


class TestExactStageRetryIsolation:
    """retry 按 revision 精确定位阶段，不读全局 scrape:global 最近一条。"""

    def _make_batch_with_two_units(self, client, tmp_path):
        from app.db.database import get_connection
        from app.import_plan import revision_store

        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        batch_id = data["batch_id"]
        root_id = data["roots"][0]["root_id"]

        conn = get_connection()
        now = revision_store.now_iso()
        units = {}
        for unit_id, boundary in (("cp8-unit-a", "/动画/冰菓"), ("cp8-unit-b", "/动画/冰菓")):
            conn.execute(
                """
                INSERT OR IGNORE INTO media_units (
                    unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                    status, closure_generation, current_revision_id, created_at, updated_at
                ) VALUES (?, ?, ?, '', ?, 'w', 'confirmed', 0, '', ?, ?)
                """,
                (unit_id, batch_id, root_id, boundary, now, now),
            )
        conn.commit()

        revs = {}
        for unit_id, canonical in (("cp8-unit-a", "unit:cp8-unit-a:main"), ("cp8-unit-b", "unit:cp8-unit-b:main")):
            rev = revision_store.create_revision(
                unit_id=unit_id, source_generation=1,
                items=[_item(f"{unit_id}/Season 1/{unit_id}.mkv", canonical_work_id=canonical)],
                status="confirmed",
            )
            conn.execute(
                "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
                (rev["revision_id"], unit_id),
            )
            # 立即提交，避免循环下一次 create_revision 的 BEGIN IMMEDIATE 与
            # 未提交 UPDATE 隐式事务冲突（sqlite3 默认隔离级别）。
            conn.commit()
            revs[unit_id] = rev["revision_id"]
        return batch_id, root_id, units, revs

    def test_retry_work_a_only_retries_a_when_b_is_global_latest(self, client, tmp_path):
        """Work A scrape failed 而 B 是全局最新 scrape → retry A 只重试 A。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        batch_id, root_id, _units, revs = self._make_batch_with_two_units(client, tmp_path)
        rev_a = revs["cp8-unit-a"]
        rev_b = revs["cp8-unit-b"]
        conn = get_connection()

        # mirror 两单元都成功（retry 才会越过 mirror 检查到 scrape 阶段）
        for rev, unit in ((rev_a, "cp8-unit-a"), (rev_b, "cp8-unit-b")):
            mirror = job_store.create_job(
                job_type="mirror_revision", resource_key=f"mirror:{rev}",
                payload={"revision_id": rev, "unit_id": unit},
            )
            conn.execute("UPDATE jobs SET status='succeeded' WHERE job_id=?", (mirror.job_id,))
            conn.commit()
        # A 的 scrape 失败；B 的 scrape 后创建（全局最近一条），且成功
        scrape_a = job_store.create_job(
            job_type="scrape_revision", resource_key="scrape:global",
            payload={"revision_id": rev_a, "source": "openlist", "unit_id": "cp8-unit-a"},
        )
        conn.execute(
            "UPDATE jobs SET status='failed', error='模拟刮削失败' WHERE job_id=?",
            (scrape_a.job_id,),
        )
        conn.commit()
        scrape_b = job_store.create_job(
            job_type="scrape_revision", resource_key="scrape:global",
            payload={"revision_id": rev_b, "source": "openlist", "unit_id": "cp8-unit-b"},
        )
        conn.execute("UPDATE jobs SET status='succeeded' WHERE job_id=?", (scrape_b.job_id,))
        conn.commit()

        resp = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/cp8-unit-a/retry", json={}
        )
        assert resp.status_code == 200, resp.text
        retried = resp.json().get("retried_stages") or {}
        assert "scrape" in retried
        assert retried["scrape"] == scrape_a.job_id
        # A 的 scrape 已被重新入队；B 的 scrape 未被误重试
        assert job_store.get_job(scrape_a.job_id).status == "queued"
        assert job_store.get_job(scrape_b.job_id).status == "succeeded"


class TestOpenlistSourceCardLifecycle:
    """来源卡首次导入立即可见、同 root 复用、无死入口、可增量扫描。"""

    def test_source_card_created_and_reused(self, client, tmp_path):
        from app.media_presets.store import list_presets

        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)

        r1 = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert r1.status_code == 200, r1.text
        data1 = r1.json()
        assert data1.get("presets"), "import-batch 必须同步返回来源卡"
        assert data1["presets"][0]["catalog_root_id"], "来源卡必须带 catalog_root_id 权威关联"

        # 全量预设接口：来源卡是 SourceRoot 生命周期卡（不是旧式 plan 卡）
        cards = client.get("/api/media-presets").json()["presets"]
        openlist_cards = [p for p in cards if p["source"] == "openlist" and p["catalog_root_id"]]
        assert len(openlist_cards) == 1
        p1 = openlist_cards[0]
        assert p1["update_mode"] == "openlist_scan"
        assert p1["ingest_method"] == "openlist_api"
        assert p1["catalog_root_id"]
        assert "openlist_unit_count" in p1      # root 投影字段存在
        assert "openlist_attention_count" in p1
        assert p1["current_plan_id"] == ""      # 不是旧式 plan 卡 → 不得显示“继续处理”

        # 同 root 再导入 → 仍只有 1 张来源卡，且复用同一张
        r2 = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert r2.status_code == 200, r2.text
        openlist_stored = [
            p for p in list_presets()
            if p.source == "openlist" and p.catalog_root_id
        ]
        assert len(openlist_stored) == 1
        assert openlist_stored[0].preset_id == p1["preset_id"]

    def test_ui_openlist_card_excludes_dead_continue_button(self):
        """openlist_scan 卡不得显示旧式“继续处理”（resumePreset 读空 current_plan_id）。"""
        src = Path(__file__).resolve().parents[2] / "src" / "pages" / "MediaManagementPage.tsx"
        text = src.read_text(encoding="utf-8")
        assert "继续处理" in text
        # “继续处理”按钮的条件必须显式排除 openlist_scan
        assert "preset.update_mode !== 'openlist_scan'" in text
        # 来源卡保留“增量扫描”入口
        assert "rescanOpenlistPreset(preset)" in text

    def test_ui_durable_root_card_keeps_continue_confirm_entry(self):
        """RWK-39（P0-1）：durable_root TXT 卡重启后必须保留「继续确认」入口。

        事故链：TXT baseline 完成 → durable revisions 仍 draft → 用户退出页面
        → 重启 → preset 投影 needs_attention → 卡片必须有入口重新进入 durable
        aggregate 确认页。resumePreset 已是 durable-safe（读 confirmation_root_id
        + generation，调 getConfirmRootPreview），不得因防 legacy 把 durable 自己
        的入口也隐藏。"""
        src = Path(__file__).resolve().parents[2] / "src" / "pages" / "MediaManagementPage.tsx"
        text = src.read_text(encoding="utf-8")
        # durable_root + confirmation_ready 专用入口（与 legacy「继续处理」互斥）
        assert "preset.execution_authority === 'durable_root'" in text
        assert "preset.confirmation_ready === true" in text
        assert "继续确认" in text
        # 该入口必须调用 durable-safe resumePreset
        assert "resumePreset(preset)" in text

    def test_ui_durable_txt_card_shows_attention(self):
        """RWK-39（P1-1）：durable TXT 来源卡必须显示 attention（不只 OpenList 卡）。

        needs_review 无 revision 的 unit 投影成 attention_count 后，TXT 来源卡
        也必须可见，给用户真实处理入口（继续确认 / 重新导入），而非只有 OpenList 卡
        才展示 openlist_attention_count。
        """
        src = Path(__file__).resolve().parents[2] / "src" / "pages" / "MediaManagementPage.tsx"
        text = src.read_text(encoding="utf-8")
        # attention 显示条件必须覆盖 durable directory_tree 卡
        assert "preset.update_mode === 'directory_tree' && preset.execution_authority === 'durable_root'" in text
        assert "openlist_attention_count" in text

    def test_ui_durable_txt_card_has_needs_review_resolve_entry(self):
        """RWK-40（P0-2）：durable TXT 卡 attention 且 confirmation_ready=false 时，
        必须显示「处理识别结果」入口（needs_review 无 revision 单元的 durable 处理
        闭环：不能只显示 attention 数字而无处理路径）。"""
        src = Path(__file__).resolve().parents[2] / "src" / "pages" / "MediaManagementPage.tsx"
        text = src.read_text(encoding="utf-8")
        # 入口按钮条件：durable_root + 未 ready + attention>0 + needs_review_units 非空
        assert "preset.execution_authority === 'durable_root'" in text
        assert "preset.confirmation_ready !== true" in text
        assert "preset.openlist_needs_review_units" in text
        assert "处理识别结果" in text
        # resolve 必须走 durable 人工入口（生成可编辑版本 → confirmation_ready）
        assert "resolveNeedsReview" in text
        assert "importsApi.resolveNeedsReview" in text

    def test_ui_durable_txt_card_resolve_opens_confirmation(self):
        """RWK-40（P0-2）：resolve 成功后必须进入既有确认流程（resumePreset），
        保证处理识别结果后能继续确认（与 P0-1 的「继续确认」闭环衔接）。"""
        src = Path(__file__).resolve().parents[2] / "src" / "pages" / "MediaManagementPage.tsx"
        text = src.read_text(encoding="utf-8")
        assert "await loadPresets(true)" in text
        # resolve 成功后用最新投影的 preset 恢复确认身份（stale preset 会误设
        # confirmationBlocked），再 resumePreset
        assert "void resumePreset(refreshed)" in text

    def test_ui_resume_preset_durable_identity_enters_confirm(self):
        """RWK-40（P0-1）：resumePreset 必须按 durable confirmation identity 进入 confirm。

        事故链：durable draft revision → confirmation_ready=true → attention_count>0 →
        lifecycle 投影 needs_attention（非 draft）→ 若按 legacy lifecycle_status 判
        页面会送 workbench。必须：durable_root + confirmation_root_id + generation>0 +
        confirmation_ready=true → confirm；只有非 durable legacy 才沿用 lifecycle 规则。
        """
        src = Path(__file__).resolve().parents[2] / "src" / "pages" / "MediaManagementPage.tsx"
        text = src.read_text(encoding="utf-8")
        # durable 优先分支的条件全部存在
        assert "workingPreset.execution_authority === 'durable_root'" in text
        assert "workingPreset.confirmation_root_id" in text
        assert "(workingPreset.confirmation_generation ?? 0) > 0" in text
        assert "workingPreset.confirmation_ready === true" in text
        # 进入 confirm 的分支结果
        assert "? 'confirm'" in text
        # durable 条件必须优先于 legacy lifecycle 规则（源码出现位置更靠前，
        # 即「durable 先判断、legacy 兜底」的优先级顺序）
        idx_durable = text.index("workingPreset.execution_authority === 'durable_root'")
        idx_legacy = text.index("workingPreset.lifecycle_status === 'draft'")
        assert idx_durable < idx_legacy, "durable confirmation identity 必须优先于 legacy lifecycle"

class TestMediaLibrariesCanonicalProjection:
    """CP9：media_libraries 是最后一个 SQLite projection——必须按 effective
    canonical identity 分组，raw work_id 相同但 canonical 不同绝不互相覆盖
    （ON CONFLICT 合并），同 revision 多 standalone canonical 各占一行。"""

    def _set_current(self, unit_id: str, revision_id: str) -> None:
        from app.db.database import get_connection

        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (revision_id, unit_id),
        )
        conn.commit()

    def test_two_units_same_raw_work_id_distinct_canonical(self, tmp_path):
        """两个 current unit：raw work_id 相同、canonical A != B
        → handle_library_rebuild 产生两条 media_libraries，LibraryIndex 两个 works。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.library.store import load_library_index
        from app.pipeline.artifacts import upsert_artifact
        from app.pipeline.library_handler import handle_library_rebuild

        # 镜像发布链要求 .strm 真实存在（无现存 strm 的 WorkIndex 不发布空卡）
        strm_a = tmp_path / "mirror" / "A" / "Season 1" / "a.strm"
        strm_b = tmp_path / "mirror" / "B" / "Season 1" / "b.strm"
        strm_a.parent.mkdir(parents=True)
        strm_b.parent.mkdir(parents=True)
        strm_a.write_text("H:/open/A/Season 1/a.mkv", encoding="utf-8")
        strm_b.write_text("H:/open/B/Season 1/b.mkv", encoding="utf-8")

        _make_unit("unit-lib-a")
        _make_unit("unit-lib-b")
        rev_a = revision_store.create_revision(
            unit_id="unit-lib-a", source_generation=1,
            items=[_item(
                "A/Season 1/a.mkv", canonical_work_id="unit:unit-lib-a:main", work_id="w",
                target_strm_path=str(strm_a), target_dir=str(strm_a.parent),
            )],
            status="confirmed",
        )
        upsert_artifact(kind="strm", path=str(strm_a), revision_id=rev_a["revision_id"], work_id="w")
        self._set_current("unit-lib-a", rev_a["revision_id"])
        rev_b = revision_store.create_revision(
            unit_id="unit-lib-b", source_generation=1,
            items=[_item(
                "B/Season 1/b.mkv", canonical_work_id="unit:unit-lib-b:main", work_id="w",
                target_strm_path=str(strm_b), target_dir=str(strm_b.parent),
            )],
            status="confirmed",
        )
        upsert_artifact(kind="strm", path=str(strm_b), revision_id=rev_b["revision_id"], work_id="w")
        self._set_current("unit-lib-b", rev_b["revision_id"])

        result = handle_library_rebuild(
            {"unit_id": "unit-lib-a"},
            progress_callback=lambda *a, **k: None,
        )
        assert result["status"] == "succeeded"

        conn = get_connection()
        rows = conn.execute(
            "SELECT library_id, current_revision_id FROM media_libraries "
            "WHERE current_revision_id IN (?, ?) ORDER BY library_id",
            (rev_a["revision_id"], rev_b["revision_id"]),
        ).fetchall()
        # A/B raw work_id 相同（都是 "w"）→ canonical 不同必须两行，不互相覆盖
        assert [row["library_id"] for row in rows] == [
            "unit:unit-lib-a:main",
            "unit:unit-lib-b:main",
        ]
        # LibraryIndex 两个 works（canonical 身份分开）
        index = load_library_index()
        assert index is not None
        work_ids = {work.work_id for work in index.works}
        assert "unit:unit-lib-a:main" in work_ids
        assert "unit:unit-lib-b:main" in work_ids

    def test_one_revision_two_standalone_same_raw_work_id(self):
        """一个 revision 两个 standalone：raw work_id 相同、canonical A != B
        → media_libraries 两条记录（不再只取第一条 work_id）。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.pipeline.library_handler import handle_library_rebuild

        _make_unit("unit-lib-solo")
        rev = revision_store.create_revision(
            unit_id="unit-lib-solo", source_generation=1,
            items=[
                _item("剧集/剧场版A/a.mkv", canonical_work_id="unit:unit-lib-solo:sub:A", work_id="w"),
                _item("剧集/剧场版B/b.mkv", canonical_work_id="unit:unit-lib-solo:sub:B", work_id="w"),
            ],
            status="confirmed",
        )
        self._set_current("unit-lib-solo", rev["revision_id"])

        handle_library_rebuild(
            {"unit_id": "unit-lib-solo"},
            progress_callback=lambda *a, **k: None,
        )

        conn = get_connection()
        rows = conn.execute(
            "SELECT library_id FROM media_libraries WHERE current_revision_id = ? ORDER BY library_id",
            (rev["revision_id"],),
        ).fetchall()
        assert [row["library_id"] for row in rows] == [
            "unit:unit-lib-solo:sub:A",
            "unit:unit-lib-solo:sub:B",
        ]


class TestOpenlistSourceCardDurableLifecycle:
    """CP9：来源卡 is_library_indexed 必须投影完整 durable 链
    （mirror → scrape → library_rebuild）；只有 library_rebuild=succeeded
    （LibraryIndex 已发布）才 indexed；任一 stage 失败计入 attention；
    needs_review unit 不阻塞已成功发布的其他 unit。"""

    def _root_with_unit(self, unit_id: str = "card-a", root_id: str = "root-x", rev_status: str = "confirmed") -> str:
        from app.db.database import get_connection
        from app.import_plan import revision_store

        _make_unit(unit_id, root_id=root_id)
        rev = revision_store.create_revision(
            unit_id=unit_id, source_generation=1,
            items=[_item(f"{unit_id}/Season 1/{unit_id}.mkv")],
            status=rev_status,
        )
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (rev["revision_id"], unit_id),
        )
        conn.commit()
        return rev["revision_id"]

    def _finish(self, job_id: str, status: str, result: dict | None = None) -> None:
        import json

        from app.db.database import get_connection

        conn = get_connection()
        row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        payload = json.loads(row["payload"] or "{}")
        if result:
            payload["result"] = result
        conn.execute(
            "UPDATE jobs SET status = ?, payload = ? WHERE job_id = ?",
            (status, json.dumps(payload, ensure_ascii=False), job_id),
        )
        conn.commit()

    def _mirror_job(self, revision_id: str, unit_id: str, status: str = "succeeded", scrape_job_id: str = ""):
        from app.jobs import store as job_store

        job = job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:{revision_id}",
            payload={"revision_id": revision_id, "unit_id": unit_id},
        )
        self._finish(job.job_id, status, {"scrape_job_id": scrape_job_id} if scrape_job_id else None)
        return job

    def _scrape_job(self, revision_id: str, unit_id: str, status: str = "succeeded", library_job_id: str = ""):
        from app.jobs import store as job_store

        job = job_store.create_job(
            job_type="scrape_revision", resource_key="scrape:global",
            payload={"revision_id": revision_id, "unit_id": unit_id},
        )
        self._finish(
            job.job_id, status,
            {"library_rebuild_job": library_job_id} if library_job_id else None,
        )
        return job

    def _library_job(self, unit_id: str, status: str = "succeeded"):
        from app.jobs import store as job_store

        job = job_store.create_job(
            job_type="library_rebuild", resource_key="library:global",
            payload={"unit_id": unit_id},
        )
        self._finish(job.job_id, status)
        return job

    def test_mirror_success_scrape_pending_not_indexed(self):
        """mirror success / scrape queued → 未 indexed（发布链未走完）。"""
        from app.media_presets.service import openlist_preset_state

        rev = self._root_with_unit("card-a")
        self._mirror_job(rev, "card-a", status="succeeded")
        self._scrape_job(rev, "card-a", status="queued")
        state = openlist_preset_state("root-x")
        assert state["is_library_indexed"] is False
        assert state["attention_count"] == 0
        assert state["unit_count"] == 1

    def test_scrape_success_library_queued_not_indexed(self):
        """scrape success / library queued → 未 indexed。"""
        from app.media_presets.service import openlist_preset_state

        rev = self._root_with_unit("card-a")
        self._mirror_job(rev, "card-a", status="succeeded")
        self._scrape_job(rev, "card-a", status="succeeded")
        self._library_job("card-a", status="queued")
        state = openlist_preset_state("root-x")
        assert state["is_library_indexed"] is False
        assert state["attention_count"] == 0

    def test_library_failed_attention_not_indexed(self):
        """library rebuild failed → attention + 未 indexed。"""
        from app.media_presets.service import openlist_preset_state

        rev = self._root_with_unit("card-a")
        self._mirror_job(rev, "card-a", status="succeeded")
        self._scrape_job(rev, "card-a", status="succeeded")
        self._library_job("card-a", status="failed")
        state = openlist_preset_state("root-x")
        assert state["is_library_indexed"] is False
        assert state["attention_count"] == 1

    def test_library_success_indexed(self):
        """library rebuild succeeded → indexed（LibraryIndex 已发布）。"""
        from app.media_presets.service import openlist_preset_state

        rev = self._root_with_unit("card-a")
        self._mirror_job(rev, "card-a", status="succeeded")
        self._scrape_job(rev, "card-a", status="succeeded")
        self._library_job("card-a", status="succeeded")
        state = openlist_preset_state("root-x")
        assert state["is_library_indexed"] is True
        assert state["attention_count"] == 0

    def test_current_revision_failed_is_attention_not_indexed(self):
        """current revision 自身失败也必须进入 durable attention。"""
        from app.media_presets.service import openlist_preset_state

        self._root_with_unit("card-a", rev_status="failed")
        state = openlist_preset_state("root-x", require_all=True)
        assert state["is_library_indexed"] is False
        assert state["attention_count"] == 1

    def test_needs_review_unit_does_not_block_published_unit(self):
        """一个 unit needs_review（draft）+ 另一个全链成功 → indexed=true 且 attention=1。"""
        from app.media_presets.service import openlist_preset_state

        rev_a = self._root_with_unit("card-a", root_id="root-x")
        self._mirror_job(rev_a, "card-a", status="succeeded")
        self._scrape_job(rev_a, "card-a", status="succeeded")
        self._library_job("card-a", status="succeeded")
        self._root_with_unit("card-b", root_id="root-x", rev_status="draft")
        state = openlist_preset_state("root-x")
        assert state["is_library_indexed"] is True
        assert state["attention_count"] == 1
        assert state["unit_count"] == 2

    def test_mirror_failed_attention(self):
        """mirror failed → attention，且不进入 indexed。"""
        from app.media_presets.service import openlist_preset_state

        rev = self._root_with_unit("card-a")
        self._mirror_job(rev, "card-a", status="failed")
        state = openlist_preset_state("root-x")
        assert state["is_library_indexed"] is False
        assert state["attention_count"] == 1

    def test_needs_review_unit_without_revision_counts_as_attention(self):
        """RWK-39（P1-1）：needs_review 且无 revision 的 unit 不得被静默跳过。

        boundary 未识别 / evidence 需复核时 DiscoveryEngine 会建 needs_review unit
        但不生成 revision；projector 必须计入 attention，让用户知道有需处理项，
        否则会出现 published_count != unit_count 但 attention=0、lifecycle 不变
        needs_attention、用户不知道哪一项需要处理的 UX 死角。
        """
        from app.db.database import get_connection
        from app.media_presets.service import openlist_preset_state

        _make_unit("card-review", root_id="root-x")  # 默认 discovered + 无 revision
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status='needs_review', current_revision_id='' "
            "WHERE unit_id=?",
            ("card-review",),
        )
        conn.commit()
        state = openlist_preset_state("root-x")
        assert state["unit_count"] == 1
        assert state["attention_count"] == 1, "needs_review 无 revision 的 unit 必须计入 attention"
        assert state["is_library_indexed"] is False

    def test_inconsistent_revision_row_missing_counts_as_attention(self):
        """RWK-39（P1-1）：current_revision_id 指向的 revision 行缺失（数据不一致）→ attention。"""
        from app.db.database import get_connection
        from app.media_presets.service import openlist_preset_state

        _make_unit("card-inconsistent", root_id="root-x")
        conn = get_connection()
        # current_revision_id 指向一个不存在的 revision_id（数据不一致）
        conn.execute(
            "UPDATE media_units SET current_revision_id='rev-missing' WHERE unit_id=?",
            ("card-inconsistent",),
        )
        conn.commit()
        state = openlist_preset_state("root-x")
        assert state["unit_count"] == 1
        assert state["attention_count"] == 1, "revision 行缺失的 unit 必须计入 attention"
        assert state["is_library_indexed"] is False

    def test_needs_review_resolve_generates_durable_draft_revision(self, tmp_path):
        """RWK-40（P0-2）：needs_review 无 revision 的 unit 可通过人工入口生成 durable draft revision。

        事故链：TXT baseline 成功 → 识别失败 → needs_review unit 无 revision →
        confirmation_ready=false → 无「继续确认」入口 → 重导同 TXT baseline_reused →
        永久卡住。resolve 必须：source_nodes 重建 items → 生成 draft revision →
        进入 root-generation 确认集合（preset_confirmation_state ready=true）。
        """
        from app.db.database import get_connection
        from app.media_presets.service import (
            preset_confirmation_state,
            resolve_needs_review_unit,
        )

        # 建 root（source_id 前缀 pan115）供 DiscoveryEngine 读
        from app.media_presets.service import ensure_provider_source_root

        mount = tmp_path / "mount"
        mount.mkdir()
        root_id = ensure_provider_source_root(
            provider="pan115",
            local_mount_root=str(mount),
            import_family="anime",
            import_scope="",
        )
        from app.catalog import store as catalog_store

        catalog_store.set_baseline_target(root_id, 1)
        catalog_store.mark_baseline_completed(root_id, 1)
        if not root_id:  # pragma: no cover
            from app.catalog import lifecycle
            from app.catalog import store as catalog_store

            norm = "/" + str(mount).replace("\\", "/").strip("/")
            res = lifecycle.resolve_root_for_import(
                "pan115-x", norm, import_family="anime", import_scope="",
                local_locator=str(mount),
            )
            raise AssertionError(
                f"root 建立失败: resolution={res.action} canonical={getattr(res, 'canonical_root_id', '')} "
                f"covered={getattr(res, 'covered_root_ids', [])} mount={mount} norm={norm}"
            )
        assert root_id, "must create root"
        # needs_review unit（无 revision）
        _make_unit("card-needs-review", root_id=root_id, boundary="/动画/未识别作品")
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status='needs_review' WHERE unit_id=?",
            ("card-needs-review",),
        )
        # source_nodes：模拟 DiscoveryEngine 持久化过的边界内文件节点
        node_locator = str(mount / "未识别作品.S01E01.mkv")
        conn.execute(
            """
            INSERT INTO source_nodes (
                root_id, remote_path, parent_path, name, kind, size, mtime, etag,
                content_hash, remote_id, logical_locator, provider_id, route_id,
                tombstone
            ) VALUES (?, '/动画/未识别作品/未识别作品.S01E01.mkv', '/动画/未识别作品',
                '未识别作品.S01E01.mkv', 'file', 100, 1700000000, '', '', '',
                ?, '', '', '')
            """,
            (root_id, node_locator),
        )
        conn.commit()

        # resolve：人工提供作品名 → 生成可编辑 draft revision（generation fence 通过）
        result = resolve_needs_review_unit(root_id, "card-needs-review", "未识别作品", generation=1)
        assert result["revision_id"], "must produce draft revision"
        assert result["root_id"] == root_id

        # draft revision 进入 root-generation 确认集合 → confirmation_ready=true
        gen, ready = preset_confirmation_state(root_id)
        assert ready is True, "resolve 后必须可确认（不再永久卡住）"

        # unit 状态不再是 needs_review（resolve 内 init_db 可能重建连接，重新取）
        row = get_connection().execute(
            "SELECT status FROM media_units WHERE unit_id=?", ("card-needs-review",)
        ).fetchone()
        assert row["status"] != "needs_review"

    def test_needs_review_resolve_endpoint_api_layer(self, client, tmp_path):
        """RWK-40（P0-2）：/needs-review/resolve HTTP 端点（成功 + 400/409 错误分支）。"""
        from app.db.database import get_connection
        from app.media_presets.service import ensure_provider_source_root

        mount = tmp_path / "mount"
        mount.mkdir()
        root_id = ensure_provider_source_root(
            provider="pan115",
            local_mount_root=str(mount),
            import_family="anime",
            import_scope="",
        )
        from app.catalog import store as catalog_store

        catalog_store.set_baseline_target(root_id, 1)
        catalog_store.mark_baseline_completed(root_id, 1)
        _make_unit("card-nr-api", root_id=root_id, boundary="/动画/未识别作品")
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status='needs_review' WHERE unit_id=?",
            ("card-nr-api",),
        )
        node_locator = str(mount / "未识别作品.S01E01.mkv")
        conn.execute(
            """
            INSERT INTO source_nodes (
                root_id, remote_path, parent_path, name, kind, size, mtime, etag,
                content_hash, remote_id, logical_locator, provider_id, route_id,
                tombstone
            ) VALUES (?, '/动画/未识别作品/未识别作品.S01E01.mkv', '/动画/未识别作品',
                '未识别作品.S01E01.mkv', 'file', 100, 1700000000, '', '', '',
                ?, '', '', '')
            """,
            (root_id, node_locator),
        )
        conn.commit()

        # 成功路径：pan115 + root + generation + unit → 200 + revision_id
        resp = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"root_id": root_id, "generation": 1, "unit_id": "card-nr-api", "work_title": "未识别作品"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["revision_id"]
        assert resp.json()["root_id"] == root_id
        assert resp.json()["generation"] == 1

        # 缺 root_id → 422（Pydantic 模型缺失字段）
        resp2 = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"unit_id": "card-nr-api", "work_title": "x"},
        )
        assert resp2.status_code == 422, resp2.text

        # 不存在的 unit → service 层 404（HTTP 包装为 409）
        resp4 = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"root_id": root_id, "generation": 1, "unit_id": "card-missing", "work_title": "x"},
        )
        assert resp4.status_code in (404, 409), resp4.text

        # 缺 generation → 422（Pydantic 模型缺失字段）
        resp5 = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"root_id": root_id, "unit_id": "card-nr-api", "work_title": "x"},
        )
        assert resp5.status_code == 422, resp5.text

    def _make_resolveable_root(self, tmp_path, *, target: int, completed: int) -> tuple[str, str]:
        """建可 resolve 的 pan115 root + needs_review unit（source_nodes 齐备）。"""
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.media_presets.service import ensure_provider_source_root

        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        root_id = ensure_provider_source_root(
            provider="pan115",
            local_mount_root=str(mount),
            import_family="anime",
            import_scope="",
        )
        assert root_id, "must create root"
        catalog_store.set_baseline_target(root_id, target)
        catalog_store.mark_baseline_completed(root_id, completed)
        unit_id = f"card-rwk40-{target}-{completed}"
        _make_unit(unit_id, root_id=root_id, boundary="/动画/未识别作品")
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status='needs_review' WHERE unit_id=?", (unit_id,)
        )
        node_locator = str(mount / "未识别作品.S01E01.mkv")
        conn.execute(
            """
            INSERT INTO source_nodes (
                root_id, remote_path, parent_path, name, kind, size, mtime, etag,
                content_hash, remote_id, logical_locator, provider_id, route_id,
                tombstone
            ) VALUES (?, '/动画/未识别作品/未识别作品.S01E01.mkv', '/动画/未识别作品',
                '未识别作品.S01E01.mkv', 'file', 100, 1700000000, '', '', '',
                ?, '', '', '')
            """,
            (root_id, node_locator),
        )
        conn.commit()
        return root_id, unit_id

    def test_resolve_rejects_cross_source(self, client, tmp_path):
        """RWK-40（P0-3）：pan115 root + /baidu/needs-review/resolve → 409 + 0 mutation。

        service 只验证「pan115 或 baidu」不够——baidu URL + pan115 root 会跨来源
        修改。URL source 必须与 root.source_id 前缀一致。
        """
        from app.db.database import get_connection

        root_id, unit_id = self._make_resolveable_root(tmp_path, target=1, completed=1)
        resp = client.post(
            "/api/imports/baidu/needs-review/resolve",
            json={"root_id": root_id, "generation": 1, "unit_id": unit_id, "work_title": "未识别作品"},
        )
        assert resp.status_code == 409, resp.text
        # 0 mutation：unit 仍 needs_review、无 current_revision_id、无 revision
        row = get_connection().execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        assert row["status"] == "needs_review"
        assert not row["current_revision_id"]
        # 0 revision 生成必须落到 import_revisions 表（不只依赖 unit.current_revision_id）
        rev_count = get_connection().execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE unit_id=?", (unit_id,)
        ).fetchone()["c"]
        assert rev_count == 0, "跨来源 resolve 不得生成任何 revision"

    def test_resolve_rejects_stale_generation(self, client, tmp_path):
        """RWK-40（P0-3）：页面 generation=1 → target 前进到 2 → resolve gen=1 → 409 + 0 revision。

        用户看到 A、期间导入 v2 使 target 前进，仍按旧页面提交 → 必须 409 拒绝。
        """
        from app.catalog import store as catalog_store
        from app.db.database import get_connection

        root_id, unit_id = self._make_resolveable_root(tmp_path, target=1, completed=1)
        # 期间导入 v2 → target 前进到 2（completed 也推进，模拟成功 v2）
        catalog_store.set_baseline_target(root_id, 2)
        catalog_store.mark_baseline_completed(root_id, 2)
        # 用户仍按页面 generation=1 提交
        resp = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"root_id": root_id, "generation": 1, "unit_id": unit_id, "work_title": "未识别作品"},
        )
        assert resp.status_code == 409, resp.text
        # 0 mutation：unit 仍 needs_review、无 current_revision_id
        row = get_connection().execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        assert row["status"] == "needs_review"
        assert not row["current_revision_id"]
        # 0 revision 生成必须落到 import_revisions 表
        rev_count = get_connection().execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE unit_id=?", (unit_id,)
        ).fetchone()["c"]
        assert rev_count == 0, "stale generation resolve 不得生成任何 revision"

    def test_resolve_rejects_incomplete_baseline(self, client, tmp_path):
        """RWK-40（P0-3）：target=2, completed=1 → resolve gen=2 → 409 + 0 revision。

        partial/failed baseline（completed < target）即使恰好有 needs_review units
        也不得 resolve——否则污染失败 generation、给用户假闭环。
        """
        from app.db.database import get_connection

        root_id, unit_id = self._make_resolveable_root(tmp_path, target=2, completed=1)
        resp = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"root_id": root_id, "generation": 2, "unit_id": unit_id, "work_title": "未识别作品"},
        )
        assert resp.status_code == 409, resp.text
        # 0 mutation
        row = get_connection().execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        assert row["status"] == "needs_review"
        assert not row["current_revision_id"]
        # 0 revision 生成必须落到 import_revisions 表
        rev_count = get_connection().execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE unit_id=?", (unit_id,)
        ).fetchone()["c"]
        assert rev_count == 0, "incomplete baseline resolve 不得生成任何 revision"

    def test_resolve_rejects_zero_generation(self, client, tmp_path):
        """RWK-40（P0-3）：target=0（从未跑 baseline）→ resolve gen=0 → 409 + 0 mutation。

        三零边界（generation=0, target=0, completed=0）不得因 0==0 放行——
        否则在无基线 root 上生成孤儿 revision，confirm-root 无法确认它。
        """
        from app.db.database import get_connection

        root_id, unit_id = self._make_resolveable_root(tmp_path, target=0, completed=0)
        resp = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"root_id": root_id, "generation": 0, "unit_id": unit_id, "work_title": "未识别作品"},
        )
        assert resp.status_code == 409, resp.text
        row = get_connection().execute(
            "SELECT status, current_revision_id FROM media_units WHERE unit_id=?", (unit_id,)
        ).fetchone()
        assert row["status"] == "needs_review"
        assert not row["current_revision_id"]
        rev_count = get_connection().execute(
            "SELECT COUNT(*) AS c FROM import_revisions WHERE unit_id=?", (unit_id,)
        ).fetchone()["c"]
        assert rev_count == 0, "零基线 resolve 不得生成任何 revision"

    def _published_root(self, unit_id: str, tmp_path) -> str:
        """建真实 pan115 root + v1 完整发布链（mirror→scrape→library succeeded）。"""
        from app.catalog import store as catalog_store
        from app.media_presets.service import ensure_provider_source_root

        mount = tmp_path / "mount"
        mount.mkdir()
        root_id = ensure_provider_source_root(
            provider="pan115", local_mount_root=str(mount),
            import_family="anime", import_scope="",
        )
        catalog_store.set_baseline_target(root_id, 1)
        catalog_store.mark_baseline_completed(root_id, 1)
        rev1 = self._root_with_unit(unit_id, root_id=root_id)
        self._mirror_job(rev1, unit_id, status="succeeded")
        self._scrape_job(rev1, unit_id, status="succeeded")
        self._library_job(unit_id, status="succeeded")
        return root_id

    def test_v2_draft_overrides_v1_published_pointer(self, tmp_path):
        """RWK-40（P0-A）：v1 已发布 + v2 新 draft → 来源卡不得沿 v1 旧指针显示 ready。

        media_units.current_revision_id 是上一代已确认指针（仍指 rev_v1=executed），
        v2 target generation 的新 draft 必须覆盖它：confirmation_ready=true +
        is_library_indexed=false + attention>0 → 重启后来源卡出现「继续确认」。
        """
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.media_presets.service import openlist_preset_state, preset_confirmation_state

        root_id = self._published_root("card-v2draft", tmp_path)
        # v2 更新：target=2，同 unit 生成新 draft revision（current_revision_id 仍 rev1）
        catalog_store.set_baseline_target(root_id, 2)
        catalog_store.mark_baseline_completed(root_id, 2)
        rev1 = get_connection().execute(
            "SELECT current_revision_id FROM media_units WHERE unit_id=?",
            ("card-v2draft",),
        ).fetchone()["current_revision_id"]
        # v2 内容有变化（新增一集）→ create_revision 不复用 v1，生成新 draft
        rev2 = revision_store.create_revision(
            unit_id="card-v2draft", source_generation=2,
            items=[_item("card-v2draft/Season 1/card-v2draft.mkv"),
                   _item("card-v2draft/Season 1/card-v2draft.EP02.mkv")],
            status="draft",
        )
        assert rev2["revision_id"] != rev1
        # current_revision_id 仍指 v1（DiscoveryEngine 复用时不清指针）
        row = get_connection().execute(
            "SELECT current_revision_id FROM media_units WHERE unit_id=?",
            ("card-v2draft",),
        ).fetchone()
        assert row["current_revision_id"] == rev1
        # 投影：target generation 有 draft → 必须 attention / not indexed
        state = openlist_preset_state(root_id, require_all=True)
        assert state["is_library_indexed"] is False
        assert state["attention_count"] == 1
        # confirmation identity：gen2 有 draft → ready
        gen, ready = preset_confirmation_state(root_id)
        assert gen == 2 and ready is True

    def test_v2_needs_review_overrides_v1_published_and_resolve_succeeds(self, client, tmp_path):
        """RWK-40（P0-B）：v1 已发布 + v2 needs_review → attention 可见 + resolve 不被旧指针拒绝。

        current_revision_id 仍指 rev_v1（executed/published），unit.status=needs_review：
        来源卡必须显示 attention（needs_review_units 含该 unit），且 resolve gen=2
        不得因旧 published 指针 409（老 rev_v1 继续作为已发布事实存在）。
        """
        from app.catalog import store as catalog_store
        from app.db.database import get_connection
        from app.media_presets.service import openlist_preset_state

        root_id = self._published_root("card-v2nr", tmp_path)
        catalog_store.set_baseline_target(root_id, 2)
        catalog_store.mark_baseline_completed(root_id, 2)
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET status='needs_review', boundary='/动画/未识别作品' "
            "WHERE unit_id=?",
            ("card-v2nr",),
        )
        conn.commit()
        # 投影：needs_review → attention 可见（不被 rev_v1 published 掩盖）
        state = openlist_preset_state(root_id, require_all=True)
        assert state["attention_count"] == 1
        assert state["is_library_indexed"] is False
        assert any(u["unit_id"] == "card-v2nr" for u in state["needs_review_units"])
        # source_nodes：resolve 从既有节点重建 snapshot 需要
        node_locator = str(tmp_path / "mount" / "未识别作品.S01E01.mkv")
        conn.execute(
            """
            INSERT INTO source_nodes (
                root_id, remote_path, parent_path, name, kind, size, mtime, etag,
                content_hash, remote_id, logical_locator, provider_id, route_id,
                tombstone
            ) VALUES (?, '/动画/未识别作品/未识别作品.S01E01.mkv', '/动画/未识别作品',
                '未识别作品.S01E01.mkv', 'file', 100, 1700000000, '', '', '',
                ?, '', '', '')
            """,
            (root_id, node_locator),
        )
        conn.commit()
        # resolve gen=2 → 成功（不因 rev1 confirmed/executed 而 409）
        resp = client.post(
            "/api/imports/pan115/needs-review/resolve",
            json={"root_id": root_id, "generation": 2, "unit_id": "card-v2nr", "work_title": "未识别作品"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["revision_id"]

    def test_unchanged_v2_no_draft_keeps_published(self, tmp_path):
        """RWK-40（P0-C）：v2 无变化（无新 draft，复用 v1 已确认）→ 不误报 attention。

        不得为修 A/B 把所有 plan_ready 都算 attention：target generation 无 draft、
        status != needs_review 时，旧 published revision 继续作为有效当前结果。
        """
        from app.catalog import store as catalog_store
        from app.media_presets.service import openlist_preset_state, preset_confirmation_state

        root_id = self._published_root("card-v2same", tmp_path)
        # v2 unchanged：target=2 但无新 draft（复用 v1）
        catalog_store.set_baseline_target(root_id, 2)
        catalog_store.mark_baseline_completed(root_id, 2)
        state = openlist_preset_state(root_id, require_all=True)
        assert state["is_library_indexed"] is True
        assert state["attention_count"] == 0
        gen, ready = preset_confirmation_state(root_id)
        assert gen == 2 and ready is False


class TestTerminalRetryBarrierAndNoopSemantics:
    """CP9：terminal retry 的维护屏障与无操作语义。

    - 维护屏障激活 → 不得重新入队（409，旧 job 保持终态）；
    - failed exact stage → 真正 rerun（同一行 attempt+1）；
    - 重复点击 → 不新增 job 行（同 resource_key 仍一行）；
    - 无失败阶段 → 409「当前没有可重试的失败阶段」，不伪报 retried_stages。"""

    def _make_batch_with_unit(self, client, tmp_path) -> tuple[str, str]:
        """单 unit 批次 + confirmed revision。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store

        _make_local_mount(tmp_path)
        _save_config(client, tmp_path)
        _save_routes(client)
        resp = client.post(
            "/api/openlist/import-batch",
            json={"remote_paths": [REMOTE_ROOT + "/动画/冰菓"], "import_family": "anime"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        batch_id = data["batch_id"]
        root_id = data["roots"][0]["root_id"]

        conn = get_connection()
        now = revision_store.now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, ?, ?, '', '/动画/冰菓', 'w', 'confirmed', 0, '', ?, ?)
            """,
            ("cp9-unit", batch_id, root_id, now, now),
        )
        conn.commit()
        rev = revision_store.create_revision(
            unit_id="cp9-unit", source_generation=1,
            items=[_item("cp9-unit/Season 1/cp9-unit.mkv")],
            status="confirmed",
        )
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (rev["revision_id"], "cp9-unit"),
        )
        conn.commit()
        return batch_id, rev["revision_id"]

    def test_barrier_active_blocks_requeue(self, client, tmp_path):
        """维护屏障激活 → 409，旧 failed job 保持终态不被恢复。"""
        from app.catalog import maintenance_guard
        from app.db.database import get_connection
        from app.jobs import store as job_store

        batch_id, rev_id = self._make_batch_with_unit(client, tmp_path)
        mirror = job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:{rev_id}",
            payload={"revision_id": rev_id, "unit_id": "cp9-unit"},
        )
        conn = get_connection()
        conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (mirror.job_id,))
        conn.commit()

        with maintenance_guard.hold():
            resp = client.post(
                f"/api/openlist/import-batches/{batch_id}/units/cp9-unit/retry", json={}
            )
        assert resp.status_code == 409
        row = conn.execute(
            "SELECT status, attempt FROM jobs WHERE job_id=?", (mirror.job_id,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["attempt"] == 0

    def test_failed_exact_stage_reruns_same_row(self, client, tmp_path):
        """failed mirror → 200，同一行 attempt+1 且回到 queued。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        batch_id, rev_id = self._make_batch_with_unit(client, tmp_path)
        mirror = job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:{rev_id}",
            payload={"revision_id": rev_id, "unit_id": "cp9-unit"},
        )
        conn = get_connection()
        conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (mirror.job_id,))
        conn.commit()

        resp = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/cp9-unit/retry", json={}
        )
        assert resp.status_code == 200
        assert resp.json()["retried_stages"]["mirror"] == mirror.job_id
        row = conn.execute(
            "SELECT status, attempt FROM jobs WHERE job_id=?", (mirror.job_id,)
        ).fetchone()
        assert row["status"] == "queued"
        assert row["attempt"] == 1

    def test_repeated_click_no_second_job(self, client, tmp_path):
        """重复点击：第一次 rerun，第二次 already_active，同 resource_key 仍一行。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        batch_id, rev_id = self._make_batch_with_unit(client, tmp_path)
        mirror = job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:{rev_id}",
            payload={"revision_id": rev_id, "unit_id": "cp9-unit"},
        )
        conn = get_connection()
        conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (mirror.job_id,))
        conn.commit()

        r1 = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/cp9-unit/retry", json={}
        )
        assert r1.status_code == 200
        assert r1.json()["retried_stages"]["mirror"] == mirror.job_id

        r2 = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/cp9-unit/retry", json={}
        )
        assert r2.status_code == 200
        data2 = r2.json()
        assert data2["already_active_stages"]["mirror"] == mirror.job_id
        assert "mirror" not in (data2.get("retried_stages") or {})
        n = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE job_type='mirror_revision' AND resource_key=?",
            (f"mirror:{rev_id}",),
        ).fetchone()[0]
        assert n == 1

    def test_all_succeeded_returns_409_without_fake_retried(self, client, tmp_path):
        """全链路 succeeded → 409，不把 succeeded job 伪报进 retried_stages。"""
        from app.db.database import get_connection
        from app.jobs import store as job_store

        batch_id, rev_id = self._make_batch_with_unit(client, tmp_path)
        mirror = job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:{rev_id}",
            payload={"revision_id": rev_id, "unit_id": "cp9-unit"},
        )
        conn = get_connection()
        conn.execute("UPDATE jobs SET status='succeeded' WHERE job_id=?", (mirror.job_id,))
        conn.commit()

        resp = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/cp9-unit/retry", json={}
        )
        assert resp.status_code == 409
        assert "当前没有可重试的失败阶段" in resp.json()["detail"]

    def test_queued_stage_reports_already_active(self, client, tmp_path):
        """已有 queued stage → 200 + already_active_stages，不伪报 retried。"""
        from app.jobs import store as job_store

        batch_id, rev_id = self._make_batch_with_unit(client, tmp_path)
        mirror = job_store.create_job(
            job_type="mirror_revision", resource_key=f"mirror:{rev_id}",
            payload={"revision_id": rev_id, "unit_id": "cp9-unit"},
        )
        resp = client.post(
            f"/api/openlist/import-batches/{batch_id}/units/cp9-unit/retry", json={}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["already_active_stages"]["mirror"] == mirror.job_id
        assert "mirror" not in (data.get("retried_stages") or {})


class TestCanonicalSourceCardEndToEnd:
    """CP9 联测：真实 library_rebuild handler 发布 canonical works 后，
    来源卡 is_library_indexed=true（来源卡状态投影与 canonical 投影一致）。"""

    def test_published_canonical_units_mark_source_card_indexed(self, tmp_path):
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.jobs import store as job_store
        from app.media_presets.service import openlist_preset_state
        from app.pipeline.artifacts import upsert_artifact
        from app.pipeline.library_handler import handle_library_rebuild

        # 两个 current unit：raw work_id 相同、canonical 不同
        _make_unit("e2e-a", root_id="root-x")
        _make_unit("e2e-b", root_id="root-x")
        strm_a = tmp_path / "mirror" / "e2e-a" / "Season 1" / "a.strm"
        strm_b = tmp_path / "mirror" / "e2e-b" / "Season 1" / "b.strm"
        strm_a.parent.mkdir(parents=True)
        strm_b.parent.mkdir(parents=True)
        strm_a.write_text("H:/open/e2e-a.mkv", encoding="utf-8")
        strm_b.write_text("H:/open/e2e-b.mkv", encoding="utf-8")

        revs = {}
        for unit_id, canonical, strm in (
            ("e2e-a", "unit:e2e-a:main", strm_a),
            ("e2e-b", "unit:e2e-b:main", strm_b),
        ):
            rev = revision_store.create_revision(
                unit_id=unit_id, source_generation=1,
                items=[_item(
                    f"{unit_id}/Season 1/{unit_id}.mkv", canonical_work_id=canonical, work_id="w",
                    target_strm_path=str(strm), target_dir=str(strm.parent),
                )],
                status="confirmed",
            )
            upsert_artifact(kind="strm", path=str(strm), revision_id=rev["revision_id"], work_id="w")
            conn = get_connection()
            conn.execute(
                "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
                (rev["revision_id"], unit_id),
            )
            conn.commit()
            revs[unit_id] = rev["revision_id"]

        # mirror / scrape 已成功（scrape result 精确链到 library job）
        lib = job_store.create_job(
            job_type="library_rebuild", resource_key="library:global",
            payload={"unit_id": "e2e-a"},
        )
        conn = get_connection()
        for unit_id in ("e2e-a", "e2e-b"):
            mirror = job_store.create_job(
                job_type="mirror_revision", resource_key=f"mirror:{revs[unit_id]}",
                payload={"revision_id": revs[unit_id], "unit_id": unit_id},
            )
            conn.execute("UPDATE jobs SET status='succeeded' WHERE job_id=?", (mirror.job_id,))
            scrape = job_store.create_job(
                job_type="scrape_revision", resource_key="scrape:global",
                payload={"revision_id": revs[unit_id], "unit_id": unit_id},
            )
            import json

            conn.execute(
                "UPDATE jobs SET status='succeeded', payload=? WHERE job_id=?",
                (
                    json.dumps(
                        {"revision_id": revs[unit_id], "unit_id": unit_id,
                         "result": {"library_rebuild_job": lib.job_id}},
                        ensure_ascii=False,
                    ),
                    scrape.job_id,
                ),
            )
        conn.commit()

        # 真实 library_rebuild handler 执行发布（LibraryIndex 物化）
        result = handle_library_rebuild(
            {"unit_id": "e2e-a"},
            progress_callback=lambda *a, **k: None,
        )
        assert result["status"] == "succeeded"
        conn = get_connection()
        conn.execute("UPDATE jobs SET status='succeeded' WHERE job_id=?", (lib.job_id,))
        conn.commit()

        # media_libraries：canonical 两行，不因 raw work_id 相同互相覆盖
        rows = conn.execute(
            "SELECT library_id FROM media_libraries WHERE current_revision_id IN (?, ?) ORDER BY library_id",
            (revs["e2e-a"], revs["e2e-b"]),
        ).fetchall()
        assert [row["library_id"] for row in rows] == ["unit:e2e-a:main", "unit:e2e-b:main"]

        # 来源卡：library rebuild 已发布 → indexed=true，无 attention
        state = openlist_preset_state("root-x")
        assert state["unit_count"] == 2
        assert state["attention_count"] == 0
        assert state["is_library_indexed"] is True


class TestLegacyProjectionSelfHeal:
    """CP9-B：旧版本按 raw work_id 写入的 media_libraries 投影在 rebuild 后自愈，
    且重复 rebuild 幂等（行数与内容完全不变）。"""

    def _insert_legacy_row(self, library_id: str, revision_id: str, root_id: str = "root-x") -> None:
        from app.db.database import get_connection
        from app.import_plan import revision_store

        ts = revision_store.now_iso()
        conn = get_connection()
        conn.execute(
            """
            INSERT INTO media_libraries (
                library_id, name, root_id, remote_locator, import_family, import_scope,
                current_revision_id, lifecycle_status, created_at, updated_at
            ) VALUES (?, '作品', ?, '', 'anime', '', ?, 'draft', ?, ?)
            """,
            (library_id, root_id, revision_id, ts, ts),
        )
        conn.commit()

    def _library_ids(self) -> list[str]:
        from app.db.database import get_connection

        conn = get_connection()
        return [
            row["library_id"]
            for row in conn.execute(
                "SELECT library_id FROM media_libraries ORDER BY library_id"
            ).fetchall()
        ]

    def test_rebuild_removes_legacy_raw_projection_and_is_idempotent(self):
        """预存旧行 w→rev；rebuild 后 w 删除、canonical 存在；再 rebuild 完全不变。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.pipeline.library_handler import handle_library_rebuild

        _make_unit("unit-heal-a")
        rev = revision_store.create_revision(
            unit_id="unit-heal-a", source_generation=1,
            items=[_item("A/Season 1/a.mkv", canonical_work_id="unit:unit-heal-a:main", work_id="w")],
            status="confirmed",
        )
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (rev["revision_id"], "unit-heal-a"),
        )
        conn.commit()
        # cd502c4 时代旧版本写入的错误 projection（raw work_id 作 library_id）
        self._insert_legacy_row("w", rev["revision_id"])

        handle_library_rebuild(
            {"unit_id": "unit-heal-a"},
            progress_callback=lambda *a, **k: None,
        )
        ids = self._library_ids()
        assert ids == ["unit:unit-heal-a:main"]
        assert "w" not in ids

        # 再 rebuild 一次：行数和内容完全不变（幂等收敛）
        handle_library_rebuild(
            {"unit_id": "unit-heal-a"},
            progress_callback=lambda *a, **k: None,
        )
        assert self._library_ids() == ["unit:unit-heal-a:main"]

    def test_rebuild_two_standalone_with_legacy_row_keeps_only_canonicals(self):
        """同 revision 两 standalone canonical + 预存旧 raw 行 → 恰好两条 canonical。"""
        from app.db.database import get_connection
        from app.import_plan import revision_store
        from app.pipeline.library_handler import handle_library_rebuild

        _make_unit("unit-heal-solo")
        rev = revision_store.create_revision(
            unit_id="unit-heal-solo", source_generation=1,
            items=[
                _item("剧集/剧场版A/a.mkv", canonical_work_id="unit:unit-heal-solo:sub:A", work_id="w"),
                _item("剧集/剧场版B/b.mkv", canonical_work_id="unit:unit-heal-solo:sub:B", work_id="w"),
            ],
            status="confirmed",
        )
        conn = get_connection()
        conn.execute(
            "UPDATE media_units SET current_revision_id = ? WHERE unit_id = ?",
            (rev["revision_id"], "unit-heal-solo"),
        )
        conn.commit()
        self._insert_legacy_row("w", rev["revision_id"])

        handle_library_rebuild(
            {"unit_id": "unit-heal-solo"},
            progress_callback=lambda *a, **k: None,
        )
        assert self._library_ids() == [
            "unit:unit-heal-solo:sub:A",
            "unit:unit-heal-solo:sub:B",
        ]
