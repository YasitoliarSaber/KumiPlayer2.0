"""V4 导入 API 契约：revision 是唯一计划身份。"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    from app.api import media_v4

    _patch_database(tmp_path, monkeypatch)
    application = FastAPI()
    application.include_router(media_v4.router)
    return TestClient(application)


def _patch_database(tmp_path, monkeypatch):
    """给直接调用 scan_source 的测试提供隔离数据库，避免写入真实 data/。"""

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "api.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    return database


def _payload(revision_id: str = "rev-api"):
    return {
        "revision_id": revision_id,
        "root_id": "root-api",
        "scan_id": "scan-api",
        "entries": [
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": "Show/Show.S01E01.mkv",
                "source_locator": "local://show/Show.S01E01.mkv",
                "playback_locator": "local://show/Show.S01E01.mkv",
            }
        ],
    }


def test_confirmed_revision_can_request_termination_while_a_job_is_running(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.revisions.service import V4RevisionService

    evidence = SourceEvidence(
        evidence_id="ev-cancel", scan_id="scan-cancel", root_id="root-cancel",
        source_key="cancel", relative_path="Cancel/Cancel.S01E01.mkv", entry_kind="video",
        source_locator="local://cancel/Cancel.S01E01.mkv",
        playback_locator="local://cancel/Cancel.S01E01.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-cancel", evidence_id=evidence.evidence_id, parser_version="fixture",
        work_title="Cancel", title_candidates=("Cancel",), media_type="tv", group_type="season",
        season_candidate=1, episode_candidate=1,
    )
    service = V4RevisionService(media_v4.get_database())
    service.create_draft("rev-cancel-api", [(evidence, facts)])
    service.confirm("rev-cancel-api")
    with media_v4.get_database().connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'running' WHERE revision_id = ? AND job_type = 'materialize_mirror'",
            ("rev-cancel-api",),
        )

    response = client.post("/api/v4/imports/rev-cancel-api/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["running"] == 1
    assert response.json()["cancelled"] == 2


def test_source_card_delete_hides_only_the_card_and_keeps_confirmed_media(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.revisions.service import V4RevisionService

    evidence = SourceEvidence(
        evidence_id="ev-delete-card", scan_id="scan-delete-card", root_id="root-delete-card",
        source_key="delete-card", relative_path="DeleteCard/DeleteCard.S01E01.mkv", entry_kind="video",
        source_locator="local://delete-card/DeleteCard.S01E01.mkv",
        playback_locator="local://delete-card/DeleteCard.S01E01.mkv",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-delete-card", evidence_id=evidence.evidence_id, parser_version="fixture",
        work_title="Delete Card", title_candidates=("Delete Card",), media_type="tv", group_type="season",
        season_candidate=1, episode_candidate=1,
    )
    database = media_v4.get_database()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-delete-card", [(evidence, facts)])
    revisions.confirm("rev-delete-card")
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'succeeded' WHERE revision_id = ?", ("rev-delete-card",))

    response = client.delete("/api/v4/sources/libraries/root-delete-card")

    assert response.status_code == 200, response.text
    assert response.json() == {"root_id": "root-delete-card", "hidden": True}
    assert list_source_cards(database) == []
    with database.connect() as conn:
        root = conn.execute(
            "SELECT enabled, retired_at FROM source_roots WHERE root_id = ?", ("root-delete-card",)
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM revision_bindings").fetchone()[0] == 1
    assert root["enabled"] == 0
    assert root["retired_at"] == ""

    # 列表轮询可能在第一次请求前就已把卡片标记为隐藏；重复 DELETE
    # 仍应保持幂等成功，不能把用户卡在“来源卡不存在”错误上。
    repeated = client.delete("/api/v4/sources/libraries/root-delete-card")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"root_id": "root-delete-card", "hidden": True}


def test_visible_post_retirement_source_card_can_be_renamed_and_hidden(tmp_path, monkeypatch):
    """A draft created after retirement is still a visible card and must stay manageable."""

    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.revisions.service import V4RevisionService

    def pair(revision: str, scan: str, episode: int):
        evidence = SourceEvidence(
            evidence_id=f"ev-{revision}", scan_id=scan, root_id="root-visible-retired",
            source_key=f"Visible/Visible.S01E{episode:02d}.mkv",
            relative_path=f"Visible/Visible.S01E{episode:02d}.mkv", entry_kind="video",
            source_locator="local://visible/Visible.S01E01.mkv",
            playback_locator="local://visible/Visible.S01E01.mkv",
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{revision}", evidence_id=evidence.evidence_id, parser_version="fixture",
            work_title="Visible", title_candidates=("Visible",), media_type="tv", group_type="season",
            season_candidate=1, episode_candidate=episode,
        )
        return evidence, facts

    database = media_v4.get_database()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-visible-old", [pair("old", "scan-visible-old", 1)])
    revisions.confirm("rev-visible-old")
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_roots SET retired_at = ?, retired_reason = ? WHERE root_id = ?",
            (datetime.now(UTC).isoformat(), "fixture", "root-visible-retired"),
        )
    revisions.create_draft("rev-visible-new", [pair("new", "scan-visible-new", 2)])
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'succeeded' WHERE revision_id = ?", ("rev-visible-old",))

    cards = list_source_cards(database)
    assert [card["root_id"] for card in cards] == ["root-visible-retired"]

    renamed = client.patch(
        "/api/v4/sources/libraries/root-visible-retired",
        json={"display_name": "Renamed source"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json() == {"root_id": "root-visible-retired", "display_name": "Renamed source"}

    hidden = client.delete("/api/v4/sources/libraries/root-visible-retired")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"root_id": "root-visible-retired", "hidden": True}
    assert list_source_cards(database) == []


def test_drafts_endpoint_keeps_new_reimport_draft_after_source_retirement(tmp_path, monkeypatch):
    """退役来源重新扫描后的 draft 仍要进入可恢复的审核队列。"""

    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.revisions.service import V4RevisionService

    def pair(evidence_id: str, scan_id: str, episode: int):
        evidence = SourceEvidence(
            evidence_id=evidence_id,
            scan_id=scan_id,
            root_id="root-draft-after-retirement",
            source_key=f"Mini/Mini.S01E{episode:02d}.mkv",
            relative_path=f"Mini/Mini.S01E{episode:02d}.mkv",
            entry_kind="video",
            provider="local",
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{evidence_id}",
            evidence_id=evidence_id,
            parser_version="fixture",
            work_title="Mini",
            title_candidates=("Mini",),
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=episode,
        )
        return evidence, facts

    database = media_v4.get_database()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-before-retirement-api", [pair("ev-api-old", "scan-api-old", 1)])
    revisions.confirm("rev-before-retirement-api")
    with database.connect() as conn:
        conn.execute(
            "UPDATE source_roots SET retired_at = ?, retired_reason = ? WHERE root_id = ?",
            (datetime.now(UTC).isoformat(), "fixture", "root-draft-after-retirement"),
        )
    revisions.create_draft("rev-after-retirement-api", [pair("ev-api-new", "scan-api-new", 2)])

    response = client.get("/api/v4/sources/drafts")

    assert response.status_code == 200, response.text
    assert [item["revision_id"] for item in response.json()["drafts"]] == [
        "rev-after-retirement-api"
    ]


def test_tree_scan_accepts_utf16_tree_through_both_entries(tmp_path, monkeypatch):
    _patch_database(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.integrations.openlist.providers import OpenListRouteConfig

    mount = tmp_path / "百度网盘"
    media_file = mount / "01动画" / "Show" / "Show.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-16")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(mount),
        openlist_server_url="https://openlist.example.test",
        openlist_mount_root=str(mount),
        openlist_remote_root="/",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/01动画",
            provider_id="baidu",
        )],
    ))
    monkeypatch.setattr(
        media_v4,
        "resolve_openlist_credentials",
        lambda: ("kumi", "secret", "available"),
    )
    monkeypatch.setattr(media_v4, "stage_scan_state", lambda _scan_id, _state: None)

    tree_result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
    ))
    hybrid_result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="hybrid",
        root_path="/01动画",
        tree_file=str(tree),
        provider="baidu",
    ))

    assert tree_result["entries"][0]["playback_locator"] == str(media_file)
    assert tree_result["path_validation"]["ok"] is True
    assert hybrid_result["entries"][0]["playback_locator"] == str(media_file)
    assert hybrid_result["path_validation"]["ok"] is True


def test_tree_scan_confirm_succeeds_with_source_disk_offline(tmp_path, monkeypatch):
    """TXT 确认不要求源盘在线：词法映射成立即可确认，不探测挂载盘。"""

    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    mount = tmp_path / "百度网盘"
    (mount / "01动画").mkdir(parents=True)  # 子库目录存在但视频不存在（源盘离线）
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(mount),
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))

    scan = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
    ))
    assert scan["path_validation"]["ok"] is True
    assert scan["effective_playback_root"] == str(mount / "01动画")

    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-offline-confirm",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [],
    })
    assert preview.status_code == 200, preview.text

    confirmed = client.post("/api/v4/imports/rev-offline-confirm/confirm")
    assert confirmed.status_code == 200, confirmed.text


def test_preview_uses_the_backend_effective_root_over_a_stale_frontend_copy(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    effective_root = tmp_path / "百度网盘" / "01动画"
    effective_root.mkdir(parents=True)
    media_file = effective_root / "Show" / "Show.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(tmp_path / "百度网盘"),
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))

    scan = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
    ))
    assert scan["path_validation"]["ok"] is True

    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-authoritative",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [{
            "provider": "baidu",
            "ingest_method": "directory_tree",
            "relative_path": "Show/Show.S01E01.mkv",
            "source_locator": "Z:\\stale\\Show\\Show.S01E01.mkv",
            "playback_locator": "Z:\\stale\\Show\\Show.S01E01.mkv",
        }],
        "source_locator": "Z:\\stale",
        "playback_locator": "Z:\\stale",
    })
    assert preview.status_code == 200, preview.text

    with media_v4._database.connect() as conn:
        row = conn.execute(
            "SELECT source_locator, playback_locator FROM source_roots WHERE root_id = ?",
            (scan["root_id"],),
        ).fetchone()
    assert row["playback_locator"] == str(effective_root)
    assert row["source_locator"] == str(effective_root)


def test_playback_refuses_unreachable_asset_without_launching_mpv(tmp_path, monkeypatch):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.playback.session import V4PlaybackManager
    from app.media_v4.revisions.service import V4RevisionService

    missing = tmp_path / "missing.mkv"  # 不创建文件
    evidence = SourceEvidence(
        evidence_id="ev-unreachable",
        scan_id="scan-unreachable",
        root_id="root-unreachable",
        source_key="Show/Show.S01E01.mkv",
        relative_path="Show/Show.S01E01.mkv",
        entry_kind="video",
        provider="local",
        source_locator=str(missing),
        playback_locator=str(missing),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-unreachable",
        evidence_id="ev-unreachable",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    database = V4Database(tmp_path / "unreachable.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-unreachable", [(evidence, facts)])
    revisions.confirm("rev-unreachable")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT work_id, episode_id, asset_id FROM revision_bindings LIMIT 1"
        ).fetchone()

    monkeypatch.setattr(
        "app.media_v4.playback.session.start_mpv",
        lambda *_args, **_kwargs: pytest.fail("不可达 Asset 不得启动 MPV"),
    )
    manager = V4PlaybackManager(database, session_dir=tmp_path / "sessions")

    with pytest.raises(ValueError) as exc_info:
        manager.play(row["work_id"], row["episode_id"], row["asset_id"])

    assert "挂载路径不可访问" in str(exc_info.value)


def test_mirror_failure_keeps_scrape_and_projection_queued(tmp_path):
    from app.media_v4.jobs.runner import V4JobRunner
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    media = tmp_path / "missing.mkv"  # 不创建文件
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id="ev-gate",
        scan_id="scan-gate",
        root_id="root-gate",
        source_key="Show/Show.S01E01.mkv",
        relative_path="Show/Show.S01E01.mkv",
        entry_kind="video",
        provider="local",
        source_locator=str(media),
        playback_locator=str(media),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-gate",
        evidence_id="ev-gate",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    database = V4Database(tmp_path / "gate.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-gate", [(evidence, facts)])
    revisions.confirm("rev-gate")
    jobs = revisions.list_jobs("rev-gate")
    mirror_job = next(item for item in jobs if item["job_type"] == "materialize_mirror")

    runner = V4JobRunner(database)
    with pytest.raises(RuntimeError, match="挂载路径不可访问"):
        runner.process_job(mirror_job["job_id"], mirror_root=tmp_path / "mirror")

    with database.connect() as conn:
        statuses = {
            str(row["job_type"]): str(row["status"])
            for row in conn.execute(
                "SELECT job_type, status FROM jobs WHERE revision_id = 'rev-gate'"
            ).fetchall()
        }
    assert statuses["materialize_mirror"] == "failed"
    assert statuses["scrape_work"] == "queued"
    assert statuses["refresh_projection"] == "queued"


def test_preview_confirm_and_library_use_revision_work_and_job_identities(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    activated_scan_ids = []
    monkeypatch.setattr(media_v4, "activate_scan_state", activated_scan_ids.append)

    preview = client.post("/api/v4/imports/preview", json=_payload())
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["revision_id"] == "rev-api"
    assert body["status"] == "draft"
    assert body["works"][0]["work_key"]
    assert "plan_id" not in body

    confirmed = client.post("/api/v4/imports/rev-api/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    assert all("job_id" in job for job in confirmed.json()["jobs"])
    assert activated_scan_ids == ["scan-api"]

    confirmed_evidence = media_v4._confirmed_source_evidence("root-api")
    assert [item.relative_path for item in confirmed_evidence] == ["Show/Show.S01E01.mkv"]

    library = client.get("/api/v4/library")
    assert library.status_code == 200, library.text
    assert library.json()["cards"][0]["title"] == "Show"


def test_confirmed_source_has_a_reopenable_card_with_live_job_summary(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload("rev-source-card")
    payload.update({
        "source_display_name": "本地动画库",
        "source_locator": "D:\\Media\\Anime",
        "playback_locator": "D:\\Media\\Anime",
    })

    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-source-card/confirm").status_code == 200

    response = client.get("/api/v4/sources/libraries")
    assert response.status_code == 200, response.text
    cards = response.json()["cards"]
    assert len(cards) == 1
    card = cards[0]
    assert card["root_id"] == "root-api"
    assert card["revision_id"] == "rev-source-card"
    assert card["display_name"] == "动画库"
    assert card["source_locator"] == "D:\\Media\\Anime"
    assert card["evidence_count"] == 1
    assert card["work_count"] == 1
    assert card["job_summary"]["total"] > 0
    assert card["job_summary"]["queued"] > 0
    assert card["can_resume"] is True
    # 来源卡是来源管理入口，不携带作品名预览，避免重复投影和无关查询。
    assert "work_previews" not in card


def test_source_card_strips_provider_prefix_from_legacy_display_name(tmp_path, monkeypatch):
    """卡片头部已显示来源，不得在标题内重复同一个提供商名称。"""

    client = _client(tmp_path, monkeypatch)
    payload = _payload("rev-source-display-name")
    payload["entries"][0]["provider"] = "baidu"
    payload.update({
        "source_display_name": "百度网盘 · 01动画",
        "source_locator": "K:\\百度网盘\\01动画",
        "playback_locator": "K:\\百度网盘\\01动画",
    })

    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-source-display-name/confirm").status_code == 200

    cards = client.get("/api/v4/sources/libraries").json()["cards"]

    assert cards[0]["provider"] == "baidu"
    assert cards[0]["display_name"] == "01动画"
    assert cards[0]["display_path"] == "01动画"


def test_source_card_uses_the_final_path_segment_when_legacy_name_is_empty():
    """更早的空标题记录也不能把完整挂载路径当作卡片标题。"""

    from app.media_v4.projection.source_libraries import _display_name

    assert _display_name("", "K:\\百度网盘\\01动画", "baidu") == "01动画"


def test_source_card_hides_unknown_technical_error_details():
    """来源卡不得把未枚举的内部异常直接回传给用户。"""

    from app.media_v4.projection.source_libraries import _friendly_error

    detail = "requests.exceptions.ConnectionError: provider_bindings internal state"

    assert _friendly_error(detail) == "任务未能完成，请查看执行结果后重试"


def test_source_card_list_exposes_unconfirmed_draft_as_recoverable_recognition(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.post("/api/v4/imports/preview", json=_payload("rev-draft-only")).status_code == 200

    response = client.get("/api/v4/sources/libraries")
    assert response.status_code == 200
    cards = response.json()["cards"]
    assert len(cards) == 1
    assert cards[0]["revision_id"] == "rev-draft-only"
    assert cards[0]["phase"] == "review"
    assert cards[0]["overall_status"] == "needs_attention"
    assert cards[0]["can_resume"] is True


def test_preview_with_unknown_title_returns_review_issue_and_confirm_conflict(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload("rev-review")
    payload["entries"][0]["relative_path"] = "unknown/0001.mkv"

    preview = client.post("/api/v4/imports/preview", json=payload)
    assert preview.status_code == 200
    assert preview.json()["issues"]

    confirmed = client.post("/api/v4/imports/rev-review/confirm")
    assert confirmed.status_code == 409


def test_hybrid_tree_scan_reuses_the_openlist_root_identity(tmp_path, monkeypatch):
    _patch_database(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.integrations.openlist.providers import OpenListRouteConfig
    from app.media_v4.sources.scanner import openlist_root_id

    mount = tmp_path / "OpenList"
    media_file = mount / "Anime" / "TV" / "Show" / "Show.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = tmp_path / "anime-tree.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root=str(mount),
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            label="115 动画",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(
        media_v4,
        "resolve_openlist_credentials",
        lambda: ("kumi", "secret", "available"),
    )
    monkeypatch.setattr(media_v4, "stage_scan_state", lambda _scan_id, _state: None)

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="hybrid",
        root_path="/Anime/TV",
        tree_file=str(tree),
        provider="baidu",
        source_root="Z:\\stale-frontend-path",
    ))

    assert result["root_id"] == openlist_root_id(
        config.openlist_server_url,
        "kumi",
        "/Anime/TV",
    )
    assert result["entries"][0]["ingest_method"] == "directory_tree"
    assert result["entries"][0]["provider"] == "pan115"
    assert result["entries"][0]["source_route_id"] == "route-anime"
    assert result["entries"][0]["relative_path"] == "Show/Show.S01E01.mkv"
    assert result["entries"][0]["playback_locator"] == str(media_file)
    assert result["effective_playback_root"] == str(mount / "Anime" / "TV")
    assert result["path_validation"]["ok"] is True

    preview = media_v4.preview(media_v4.PreviewRequest(
        revision_id="rev-hybrid-locators",
        root_id=result["root_id"],
        scan_id=result["scan_id"],
        entries=[],
    ))
    assert preview["issues"] == []
    with media_v4.get_database().connect() as conn:
        root = conn.execute(
            "SELECT source_locator, playback_locator FROM source_roots WHERE root_id = ?",
            (result["root_id"],),
        ).fetchone()
    assert tuple(root) == ("/Anime/TV", str(mount / "Anime" / "TV"))


def test_tree_scan_preserves_quark_as_the_content_provider(tmp_path, monkeypatch):
    _patch_database(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.integrations.openlist.providers import OpenListRouteConfig

    mount = tmp_path / "OpenList"
    media_file = mount / "Quark" / "Show" / "Show.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = tmp_path / "quark-tree.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root="",
        openlist_mount_root=str(mount),
        openlist_remote_root="/",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-quark",
            remote_prefix="/Quark",
            provider_id="quark",
        )],
    ))

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="quark",
        source_root=str(tmp_path / "quark-mount"),
    ))

    assert result["entries"][0]["provider"] == "quark"
    assert result["entries"][0]["playback_locator"] == str(media_file)
    assert result["effective_playback_root"] == str(mount / "Quark")


def test_tree_scan_resolves_the_precise_sub_library_root_from_the_tree_location(tmp_path, monkeypatch):
    _patch_database(tmp_path, monkeypatch)
    from app.api import media_v4

    baidu_root = tmp_path / "百度网盘"
    library = baidu_root / "01动画"
    media_file = library / "Show" / "Show.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = library / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(baidu_root),
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
        source_root="Z:\\stale-frontend-copy",
    ))

    # 总根 / Show 不存在；TXT 父目录（媒体子库）是唯一全部命中候选。
    assert result["effective_playback_root"] == str(library)
    assert result["entries"][0]["playback_locator"] == str(media_file)
    assert result["path_validation"]["ok"] is True
    assert result["path_validation"]["hits"] == result["path_validation"]["total"]


def test_remote_tree_scan_requires_a_playback_mapping(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from app.api import media_v4

    tree = tmp_path / "tree.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root="",
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))

    with pytest.raises(HTTPException) as exc_info:
        media_v4.scan_source(media_v4.SourceScanRequest(
            source="tree",
            tree_file=str(tree),
            provider="baidu",
        ))

    assert exc_info.value.status_code == 409
    assert "挂载路径" in exc_info.value.detail


def test_local_scan_receives_all_configured_cloud_mount_roots(monkeypatch):
    from app.api import media_v4

    captured = {}
    config = SimpleNamespace(
        pan115_root="K:\\115网盘",
        baidu_root="K:\\百度网盘",
        openlist_mount_root="K:\\",
        openlist_routes=[{"local_path": "L:\\夸克网盘"}],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)

    def fake_scan(root_path, *, excluded_roots):
        captured["root_path"] = root_path
        captured["excluded_roots"] = excluded_roots
        return "root-local", "scan-local", []

    monkeypatch.setattr(media_v4, "scan_local_directory", fake_scan)

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="local",
        root_path="D:\\Media",
    ))

    assert result["root_id"] == "root-local"
    assert captured == {
        "root_path": "D:\\Media",
        "excluded_roots": ["K:\\115网盘", "K:\\百度网盘", "K:\\", "L:\\夸克网盘"],
    }


def test_explicit_openlist_incremental_without_baseline_returns_actionable_409(monkeypatch):
    from fastapi import HTTPException

    from app.api import media_v4
    from app.integrations.openlist.providers import OpenListRouteConfig

    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda _root_id: [])

    with pytest.raises(HTTPException) as exc_info:
        media_v4.scan_source(media_v4.SourceScanRequest(
            source="openlist",
            root_path="/Anime",
            provider="pan115",
            scan_mode="incremental",
        ))

    assert exc_info.value.status_code == 409
    # 增量属于 OpenList 共同能力：错误必须指向“已确认基线”，而不是强制要求 TXT。
    assert "已确认基线" in exc_info.value.detail
    assert "首次完整扫描" in exc_info.value.detail
    assert "必须先建立并确认 TXT 基线" not in exc_info.value.detail


def test_openlist_auto_scan_rebuilds_missing_checkpoint_from_confirmed_revision(monkeypatch):
    from app.api import media_v4, openlist_v4
    from app.integrations.openlist.providers import OpenListRouteConfig
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.scanner import openlist_root_id

    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    root_id = openlist_root_id(config.openlist_server_url, "kumi", "/Anime")
    baseline = [to_source_evidence(SourceEntry(
        root_id=root_id,
        scan_id="scan-confirmed",
        provider="pan115",
        ingest_method="directory_tree",
        relative_path="Show/Show.S01E01.mkv",
    ))]
    captured = {}

    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(media_v4, "_confirmed_source_evidence", lambda _root_id: baseline)
    monkeypatch.setattr(media_v4, "load_active_state", lambda _root_id: None)
    monkeypatch.setattr(openlist_v4, "_client", lambda _config: object())
    monkeypatch.setattr(media_v4, "scan_openlist_directory", lambda *_args, **_kwargs: pytest.fail("不应退回全量扫描"))

    def fake_incremental(_client, **kwargs):
        captured["state"] = kwargs["state"]
        return "scan-incremental", baseline, kwargs["state"], {"requested_directories": 1}

    monkeypatch.setattr(media_v4, "scan_openlist_incremental", fake_incremental)
    monkeypatch.setattr(media_v4, "stage_scan_state", lambda scan_id, _state: captured.setdefault("scan_id", scan_id))

    result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="openlist",
        root_path="/Anime",
        provider="pan115",
    ))

    assert result["scan_mode"] == "incremental"
    assert captured["scan_id"] == "scan-incremental"
    assert captured["state"]["remote_verified"] is False
    assert captured["state"]["root_id"] == root_id


def test_plain_openlist_full_scan_confirm_enables_incremental_and_keeps_mode(tmp_path, monkeypatch):
    """普通 OpenList 完整扫描确认后即可增量；来源卡模式保持 openlist_full。"""

    from fastapi import HTTPException

    from app.api import media_v4, openlist_v4
    from app.integrations.openlist.providers import OpenListRouteConfig
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(openlist_v4, "_client", lambda _config: object())
    monkeypatch.setattr(media_v4, "stage_scan_state", lambda _scan_id, _state: None)
    monkeypatch.setattr(media_v4, "activate_scan_state", lambda _scan_id: None)
    monkeypatch.setattr(media_v4, "load_active_state", lambda _root_id: None)

    def fake_full_scan(_client, **kwargs):
        evidence = [
            to_source_evidence(SourceEntry(
                root_id=kwargs["root_id"],
                scan_id="scan-full-openlist",
                provider="pan115",
                ingest_method="openlist_api",
                relative_path="Show/Show.S01E01.mkv",
                source_key="Show/Show.S01E01.mkv",
                source_locator="Show/Show.S01E01.mkv",
                playback_locator="X:\\OpenList\\Anime\\Show\\Show.S01E01.mkv",
            ))
        ]
        return "scan-full-openlist", evidence

    monkeypatch.setattr(media_v4, "scan_openlist_directory", fake_full_scan)

    full = media_v4.scan_source(media_v4.SourceScanRequest(
        source="openlist",
        root_path="/Anime",
        provider="pan115",
        scan_mode="full",
    ))
    assert full["scan_mode"] == "full"
    assert full["source_mode"] == "openlist_full"
    assert full["effective_playback_root"] == r"X:\OpenList\Anime"
    with media_v4.get_database().connect() as conn:
        root = conn.execute(
            "SELECT source_locator, playback_locator FROM source_roots WHERE root_id = ?",
            (full["root_id"],),
        ).fetchone()
    assert tuple(root) == ("/Anime", r"X:\OpenList\Anime")

    # draft 未确认时显式 incremental 仍然拒绝，不悄悄退化为全量。
    with pytest.raises(HTTPException) as exc_info:
        media_v4.scan_source(media_v4.SourceScanRequest(
            source="openlist",
            root_path="/Anime",
            provider="pan115",
            scan_mode="incremental",
        ))
    assert exc_info.value.status_code == 409

    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-openlist",
        "root_id": full["root_id"],
        "scan_id": full["scan_id"],
        "entries": full["entries"],
    })
    assert preview.status_code == 200, preview.text
    assert client.post("/api/v4/imports/rev-openlist/confirm").status_code == 200

    captured: dict = {}

    def fake_incremental(_client, **kwargs):
        captured["baseline"] = kwargs["baseline"]
        return "scan-incr", kwargs["baseline"], kwargs["state"], {"requested_directories": 1}

    monkeypatch.setattr(media_v4, "scan_openlist_incremental", fake_incremental)

    incr = media_v4.scan_source(media_v4.SourceScanRequest(
        source="openlist",
        root_path="/Anime",
        provider="pan115",
        scan_mode="incremental",
    ))
    assert incr["scan_mode"] == "incremental"
    assert incr["source_mode"] == "openlist_full"
    assert captured["baseline"][0].ingest_method == "openlist_api"

    cards = client.get("/api/v4/sources/libraries").json()["cards"]
    card = next(item for item in cards if item["root_id"] == full["root_id"])
    assert card["source_mode"] == "openlist_full"
    assert card["has_confirmed_baseline"] is True

    status = client.get("/api/v4/sources/openlist/status", params={"remote_root": "/Anime"})
    assert status.status_code == 200, status.text
    assert status.json()["has_confirmed_baseline"] is True
    assert status.json()["source_mode"] == "openlist_full"


def test_source_card_source_mode_is_stable_across_mixed_evidence_order(tmp_path, monkeypatch):
    """来源卡模式由来源根级 contract 决定，不随排序首条文件证据反推。"""

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "mode.db")
    database.initialize()
    revisions = V4RevisionService(database)

    def make_entry(folder: str, ingest: str):
        relative = f"{folder}/Show.S01E01.mkv"
        evidence = SourceEvidence(
            evidence_id=f"ev-{folder}",
            scan_id="scan-mode",
            root_id="root-mode",
            source_key=relative,
            relative_path=relative,
            entry_kind="video",
            provider="pan115",
            source_locator=relative,
            playback_locator=f"X:\\{relative}",
            ingest_method=ingest,
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{folder}",
            evidence_id=evidence.evidence_id,
            parser_version="fixture",
            work_title="Show",
            title_candidates=("Show",),
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=1,
            confidence="high",
        )
        return evidence, facts

    def no_search(*_args, **_kwargs):
        return []

    # 场景 A：排序首条是 OpenList 已核对证据，但来源根模式由显式 contract 决定。
    revisions.create_draft(
        "rev-a",
        [
            make_entry("A", "openlist_api"),
            make_entry("B", "directory_tree"),
        ],
        root_id="root-mode",
        scan_id="scan-mode",
        source_mode="tree_openlist",
        candidate_search=no_search,
    )
    revisions.confirm("rev-a")
    # 场景 B：排序首条换成目录树证据，模式不变，且同 root_id 不生成重复来源卡。
    revisions.create_draft(
        "rev-b",
        [
            make_entry("C", "directory_tree"),
            make_entry("D", "openlist_api"),
        ],
        root_id="root-mode",
        scan_id="scan-mode",
        source_mode="tree_openlist",
        candidate_search=no_search,
    )
    revisions.confirm("rev-b")
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT root_id, source_mode, ingest_method FROM source_roots WHERE root_id = 'root-mode'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["source_mode"] == "tree_openlist"
    assert rows[0]["ingest_method"] == "directory_tree"


def test_openlist_status_endpoint_reports_no_baseline_for_unconfirmed_root(tmp_path, monkeypatch):
    from app.api import media_v4
    from app.integrations.openlist.providers import OpenListRouteConfig
    from app.media_v4.sources.scanner import openlist_root_id

    client = _client(tmp_path, monkeypatch)
    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[OpenListRouteConfig(
            route_id="route-anime",
            remote_prefix="/Anime",
            provider_id="pan115",
        )],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))

    response = client.get("/api/v4/sources/openlist/status", params={"remote_root": "/Anime"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["root_id"] == openlist_root_id(config.openlist_server_url, "kumi", "/Anime")
    assert body["remote_root"] == "/Anime"
    assert body["has_confirmed_baseline"] is False
    assert body["source_mode"] == ""
    assert body["last_scan_mode"] == ""


def test_openlist_scan_rejects_an_unmapped_remote_root_before_network(monkeypatch):
    from fastapi import HTTPException

    from app.api import media_v4, openlist_v4

    config = SimpleNamespace(
        openlist_server_url="https://openlist.example.test",
        openlist_remote_root="/",
        openlist_mount_root="X:\\OpenList",
        openlist_routes=[],
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: config)
    monkeypatch.setattr(media_v4, "resolve_openlist_credentials", lambda: ("kumi", "secret", "available"))
    monkeypatch.setattr(openlist_v4, "_client", lambda _config: pytest.fail("未映射目录不应发起网络请求"))

    with pytest.raises(HTTPException) as exc_info:
        media_v4.scan_source(media_v4.SourceScanRequest(
            source="openlist",
            root_path="/Unmapped",
            provider="pan115",
            scan_mode="full",
        ))

    assert exc_info.value.status_code == 409
    assert "内容来源路由" in exc_info.value.detail


def test_playback_and_tracking_are_user_state_endpoints(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    preview = client.post("/api/v4/imports/preview", json=_payload("rev-state"))
    assert preview.status_code == 200
    client.post("/api/v4/imports/rev-state/confirm")

    with media_v4._database.connect() as conn:
        row = conn.execute(
            """
            SELECT w.work_id, e.episode_id, a.asset_id
            FROM works w JOIN episodes e ON e.work_id = w.work_id
            JOIN episode_assets ea ON ea.episode_id = e.episode_id
            JOIN assets a ON a.asset_id = ea.asset_id
            LIMIT 1
            """
        ).fetchone()
    assert row is not None

    progress = client.post(
        "/api/v4/playback/progress",
        json={
            "work_id": row["work_id"],
            "episode_id": row["episode_id"],
            "asset_id": row["asset_id"],
            "position": 12.5,
            "duration": 100,
            "completed": False,
        },
    )
    assert progress.status_code == 200
    assert progress.json()["position"] == 12.5

    tracking = client.post(
        "/api/v4/tracking/state",
        json={
            "work_id": row["work_id"],
            "provider": "bangumi",
            "provider_id": "subject-1",
            "last_watched_episode": 1,
        },
    )
    assert tracking.status_code == 200
    assert tracking.json()["provider_id"] == "subject-1"


def _tree_scan_fixture(tmp_path, monkeypatch):
    """构造一个可达的子库目录树扫描，返回 (mount, scan, client)。"""

    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    media_file = library / "Show" / "Show.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(mount),
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))
    scan = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
    ))
    assert scan["path_validation"]["ok"] is True
    return mount, scan, client


def test_tree_scan_opens_the_txt_exactly_once_end_to_end(tmp_path, monkeypatch):
    import builtins

    from app.media_v4.sources import scanner

    _patch_database(tmp_path, monkeypatch)
    mount = tmp_path / "百度网盘"
    library = mount / "01动画"
    media_file = library / "Show" / "Show.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")

    from app.api import media_v4

    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(mount),
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))
    counter = {"opens": 0}
    real_open = builtins.open

    def counting_open(*args, **kwargs):
        counter["opens"] += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(scanner.builtins, "open", counting_open)

    media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
    ))

    assert counter["opens"] == 1


def test_preview_ignores_tampered_entry_locators_for_tree_scan(tmp_path, monkeypatch):
    mount, scan, client = _tree_scan_fixture(tmp_path, monkeypatch)
    tampered = r"C:\tampered\Show\Show.S01E01.mkv"

    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-tamper",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        # 前端篡改逐条 locator：这些值绝不能被写入权威证据/Asset。
        "entries": [{
            "provider": "baidu",
            "ingest_method": "directory_tree",
            "relative_path": "Show/Show.S01E01.mkv",
            "source_locator": tampered,
            "playback_locator": tampered,
        }],
        "source_locator": r"C:\tampered",
        "playback_locator": r"C:\tampered",
        "source_route_id": "route-tampered",
    })
    assert preview.status_code == 200, preview.text

    from app.api import media_v4

    # 后端持久化的证据（preview 的真正输入）必须是权威路径。
    with media_v4._database.connect() as conn:
        evidence_rows = conn.execute(
            "SELECT playback_locator, source_locator FROM source_evidence WHERE scan_id = ?",
            (scan["scan_id"],),
        ).fetchall()
    assert len(evidence_rows) == 1
    expected = str(mount / "01动画" / "Show" / "Show.S01E01.mkv")
    assert evidence_rows[0]["playback_locator"] == expected
    assert evidence_rows[0]["playback_locator"] != tampered
    with media_v4._database.connect() as conn:
        source_root = conn.execute(
            "SELECT route_id FROM source_roots WHERE root_id = ?",
            (scan["root_id"],),
        ).fetchone()
    assert source_root["route_id"] == ""

    # 确认后 Asset 也必须来自权威证据，而不是前端改写值。
    confirmed = client.post("/api/v4/imports/rev-tamper/confirm")
    assert confirmed.status_code == 200, confirmed.text
    with media_v4._database.connect() as conn:
        asset_rows = conn.execute(
            """
            SELECT a.playback_locator, a.source_locator
            FROM revision_bindings rb
            JOIN assets a ON a.asset_id = rb.asset_id
            WHERE rb.revision_id = 'rev-tamper'
            """
        ).fetchall()
    assert len(asset_rows) == 1
    assert asset_rows[0]["playback_locator"] == expected
    assert asset_rows[0]["playback_locator"] != tampered


def test_confirm_requires_validation_record_for_tree_revision(tmp_path, monkeypatch):
    _mount, scan, client = _tree_scan_fixture(tmp_path, monkeypatch)
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-lost-validation",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [],
    })
    assert preview.status_code == 200, preview.text

    from app.api import media_v4

    with media_v4._database.connect() as conn:
        conn.execute("DELETE FROM tree_scan_validation WHERE scan_id = ?", (scan["scan_id"],))

    confirmed = client.post("/api/v4/imports/rev-lost-validation/confirm")
    assert confirmed.status_code == 409
    assert "缺少验证记录" in confirmed.json()["detail"]


def test_confirm_rechecks_sample_reachability_after_scan(tmp_path, monkeypatch):
    mount, scan, client = _tree_scan_fixture(tmp_path, monkeypatch)
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-offline-after-scan",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [],
    })
    assert preview.status_code == 200, preview.text

    # scan 后源盘离线：真实视频文件被移除，confirm 仍应成功（词法映射不依赖可达性）。
    (mount / "01动画" / "Show" / "Show.S01E01.mkv").unlink()

    confirmed = client.post("/api/v4/imports/rev-offline-after-scan/confirm")
    assert confirmed.status_code == 200, confirmed.text


def test_confirm_rejects_validation_record_with_wrong_root(tmp_path, monkeypatch):
    """映射记录 root 不匹配仍 409：不能拿其他 scan 的映射冒用。"""

    _mount, scan, client = _tree_scan_fixture(tmp_path, monkeypatch)
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-root-mismatch",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [],
    })
    assert preview.status_code == 200, preview.text

    from app.api import media_v4

    with media_v4._database.connect() as conn:
        conn.execute(
            "UPDATE tree_scan_validation SET root_id = 'root-other' WHERE scan_id = ?",
            (scan["scan_id"],),
        )

    confirmed = client.post("/api/v4/imports/rev-root-mismatch/confirm")
    assert confirmed.status_code == 409
    assert "不一致" in confirmed.json()["detail"]


def test_confirm_allows_old_validation_with_unchanged_identity(tmp_path, monkeypatch):
    """过 24 小时但身份和映射未变仍允许：不可变映射不因隔天失效。"""

    _mount, scan, client = _tree_scan_fixture(tmp_path, monkeypatch)
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-old-validation",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [],
    })
    assert preview.status_code == 200, preview.text

    from datetime import UTC, datetime, timedelta

    from app.api import media_v4

    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    with media_v4._database.connect() as conn:
        conn.execute(
            "UPDATE tree_scan_validation SET validated_at = ? WHERE scan_id = ?",
            (stale, scan["scan_id"]),
        )

    confirmed = client.post("/api/v4/imports/rev-old-validation/confirm")
    assert confirmed.status_code == 200, confirmed.text


def test_confirmed_tree_revision_confirm_is_idempotent_after_validation_cleanup(tmp_path, monkeypatch):
    _mount, scan, client = _tree_scan_fixture(tmp_path, monkeypatch)
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-idempotent",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [],
    })
    assert preview.status_code == 200, preview.text

    first = client.post("/api/v4/imports/rev-idempotent/confirm")
    assert first.status_code == 200, first.text
    second = client.post("/api/v4/imports/rev-idempotent/confirm")
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "confirmed"


def test_tree_scan_wires_root_container_into_preview_identity(tmp_path, monkeypatch):
    """扫描根即作品目录时，Season 1 首层不得再成为 Work（P-001 7.2.3）。"""

    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4

    mount = tmp_path / "百度网盘"
    library = mount / "古诺希亚"
    media_file = library / "Season 1" / "古诺希亚.S01E01.mkv"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"video")
    tree = library / "古诺希亚_文件目录.txt"
    tree.write_text("Season 1/古诺希亚.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(mount),
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))

    scan = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
    ))
    assert scan["path_validation"]["ok"] is True
    assert scan["root_container"] == "古诺希亚"

    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-root-container",
        "root_id": scan["root_id"],
        "scan_id": scan["scan_id"],
        "entries": [],
    })
    assert preview.status_code == 200, preview.text
    body = preview.json()
    titles = {work["preferred_title"] for work in body["works"]}
    assert titles == {"古诺希亚"}
    assert not any(work["preferred_title"] == "Season 1" for work in body["works"])


def test_metadata_manual_confirm_requeues_scrape_and_refreshes_projection(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    media = tmp_path / "Show.S01E01.mkv"
    media.write_bytes(b"video")
    evidence = SourceEvidence(
        evidence_id="ev-manual",
        scan_id="scan-manual",
        root_id="root-manual",
        source_key="ev-manual",
        relative_path="Show/Show.S01E01.mkv",
        entry_kind="video",
        provider="local",
        source_locator=str(media),
        playback_locator=str(media),
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-manual",
        evidence_id="ev-manual",
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=1,
        episode_candidate=1,
    )
    from app.media_v4.revisions.service import V4RevisionService

    V4RevisionService(media_v4._database).create_draft("rev-manual", [(evidence, facts)])
    assert client.post("/api/v4/imports/rev-manual/confirm").status_code == 200

    provider_calls = []

    def fake_provider(target):
        has_confirmed_binding = any(
            b.get("provider") == "tmdb" and str(b.get("provider_id")) == "12345"
            for b in (target.get("provider_bindings") or [])
        )
        provider_calls.append(has_confirmed_binding)
        if not has_confirmed_binding:
            return {
                "provider": "local",
                "provider_id": "",
                "media_type": "tv",
                "metadata_state": "waiting_review",
                "reason": "没有唯一匹配的在线作品，需要人工确认后再继续",
            }
        return {
            "provider": "tmdb",
            "provider_id": "12345",
            "media_type": "tv",
            "title": "Show",
            "metadata_state": "ready",
            "poster_url": "https://image.tmdb.org/t/p/w780/p.jpg",
            "fanart_url": "https://image.tmdb.org/t/p/original/f.jpg",
        }

    monkeypatch.setattr("app.media_v4.jobs.metadata.default_metadata_provider", fake_provider)
    from app.media_v4.jobs import completeness as completeness_module
    from app.media_v4.jobs import metadata_artifacts as artifacts_module

    monkeypatch.setattr(artifacts_module, "load_config", lambda: SimpleNamespace(
        artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))
    monkeypatch.setattr(completeness_module, "load_config", lambda: SimpleNamespace(
        artwork_storage_mode="remote", tmdb_timeout=5, proxy_url=None,
    ))

    with media_v4._database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]

    # 真实人工恢复起点：首次 metadata job 已把无法唯一匹配的作品写为
    # waiting_review，但 job 本身已经正常结束。旧实现会在 confirm 后复用这个
    # succeeded job，导致 process() 直接返回，用户选完候选也不会继续。
    from app.media_v4.jobs.scrape import V4ScrapeService

    initial_job = V4ScrapeService(media_v4._database).enqueue_for_revision("rev-manual")[0]
    V4ScrapeService(media_v4._database).process(initial_job["job_id"], fake_provider)
    with media_v4._database.connect() as conn:
        initial_binding = conn.execute(
            "SELECT status FROM scrape_bindings WHERE revision_id = 'rev-manual' AND work_id = ?",
            (work_id,),
        ).fetchone()
        assert initial_binding["status"] == "waiting_review"

    # 搜索端创建服务端候选，确认端只接受 candidate_id。
    monkeypatch.setattr(
        "app.media_v4.jobs.metadata.search_tmdb_candidates",
        lambda *a, **k: [{
            "provider": "tmdb",
            "provider_id": "12345",
            "media_type": "tv",
            "title": "Show",
            "year": 2024,
        }],
    )
    searched = client.post("/api/v4/metadata/search", json={"work_id": work_id, "query": "Show"})
    assert searched.status_code == 200, searched.text
    candidate_id = searched.json()["candidates"][0]["candidate_id"]

    result = client.post("/api/v4/metadata/confirm", json={
        "work_id": work_id,
        "candidate_id": candidate_id,
    })
    assert result.status_code == 200, result.text

    with media_v4._database.connect() as conn:
        binding = conn.execute(
            "SELECT provider, provider_id, status FROM scrape_bindings WHERE work_id = ? AND provider = 'tmdb'",
            (work_id,),
        ).fetchone()
        candidates = conn.execute(
            "SELECT provider, provider_id, status FROM revision_work_candidates WHERE work_id = ?",
            (work_id,),
        ).fetchall()
        jobs = conn.execute(
            "SELECT job_id, status, attempts FROM jobs WHERE revision_id = 'rev-manual' AND work_id = ? AND job_type = 'scrape_work'",
            (work_id,),
        ).fetchall()
    assert binding is not None
    assert binding["status"] == "confirmed"
    assert binding["provider_id"] == "12345"
    assert any(
        row["provider"] == "tmdb" and row["provider_id"] == "12345" and row["status"] == "confirmed"
        for row in candidates
    )
    assert provider_calls == [False, True]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "succeeded"
    assert jobs[0]["attempts"] == 2


def test_metadata_retry_requeues_recoverable_failure_but_blocks_identity_conflict(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.media_v4.jobs.scrape import V4ScrapeService

    preview = client.post("/api/v4/imports/preview", json=_payload("rev-metadata-retry"))
    assert preview.status_code == 200, preview.text
    confirmed = client.post("/api/v4/imports/rev-metadata-retry/confirm")
    assert confirmed.status_code == 200, confirmed.text

    with media_v4._database.connect() as conn:
        work_id = str(conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"])
    job = V4ScrapeService(media_v4._database).enqueue_for_revision("rev-metadata-retry")[0]
    V4ScrapeService(media_v4._database).process(
        job["job_id"],
        lambda _target: {
            "provider": "tmdb",
            "provider_id": "42",
            "media_type": "tv",
            "title": "Show",
            "metadata_state": "source_unavailable",
            "reason": "TMDB 暂时限流",
            "reason_code": "provider_rate_limited",
            "failure_stage": "search",
            "retryable": True,
        },
    )

    queued = client.post("/api/v4/metadata/retry", json={"work_id": work_id})
    assert queued.status_code == 200, queued.text
    assert queued.json()["metadata_recovery_action"] == "retry_metadata"
    with media_v4._database.connect() as conn:
        refreshed = conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE scrape_bindings SET status = 'waiting_review', metadata_json = ? "
            "WHERE revision_id = 'rev-metadata-retry' AND work_id = ?",
            (
                json.dumps({
                    "metadata_state": "waiting_review",
                    "reason_code": "provider_identity_conflict",
                    "reason": "该在线作品已关联到另一部作品",
                }, ensure_ascii=False),
                work_id,
            ),
        )
    assert refreshed["status"] == "queued"

    blocked = client.post("/api/v4/metadata/retry", json={"work_id": work_id})
    assert blocked.status_code == 409, blocked.text


def test_metadata_retry_uses_nested_season_auth_policy(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    from app.api import media_v4
    from app.media_v4.jobs.scrape import V4ScrapeService

    preview = client.post("/api/v4/imports/preview", json=_payload("season-auth"))
    assert preview.status_code == 200, preview.text
    confirmed = client.post("/api/v4/imports/season-auth/confirm")
    assert confirmed.status_code == 200, confirmed.text

    with media_v4._database.connect() as conn:
        work_id = str(conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"])
    job = V4ScrapeService(media_v4._database).enqueue_for_revision("season-auth")[0]
    V4ScrapeService(media_v4._database).process(
        job["job_id"],
        lambda _target: {
            "provider": "tmdb",
            "provider_id": "42",
            "media_type": "tv",
            "title": "Show",
            "metadata_state": "source_unavailable",
            "reason": "部分剧集资料暂不可用",
            "reason_code": "episode_mapping_incomplete",
            "failure_stage": "season_detail",
            "retryable": False,
            "season_results": [{
                "local_season_number": 2,
                "provider_season_number": 2,
                "status": "source_unavailable",
                "reason_code": "provider_auth_required",
                "failure_stage": "season_detail",
                "retryable": False,
            }],
        },
    )

    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(tmdb_bearer_token=""))
    blocked = client.post("/api/v4/metadata/retry", json={"work_id": work_id})
    assert blocked.status_code == 409
    assert "Token" in blocked.json()["detail"]

    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="configured"))
    queued = client.post("/api/v4/metadata/retry", json={"work_id": work_id})
    assert queued.status_code == 200, queued.text
    assert queued.json()["metadata_recovery_action"] == "check_settings"


def test_sync_and_durable_tree_entries_share_one_root_identity(tmp_path, monkeypatch):
    """同一 provider、配置总根与 TXT：同步与 durable 入口必须得到一致身份。

    durable 入口在创建 root/scan 身份前先读取 TXT 并完成纯词法解析；
    不允许同步入口用解析后身份、durable 入口用 configured_roots[0] 造成
    同一逻辑来源分裂成两张来源卡。
    """

    _patch_database(tmp_path, monkeypatch)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = media_v4.get_database()
    mount = tmp_path / "百度网盘"
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text("Show/Show.S01E01.mkv\n", encoding="utf-8")
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=str(mount),
        openlist_server_url="",
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))

    sync_result = media_v4.scan_source(media_v4.SourceScanRequest(
        source="tree",
        tree_file=str(tree),
        provider="baidu",
    ))

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)
    durable_response = client.post("/api/v4/sources/scans", json={
        "source": "tree",
        "tree_file": str(tree),
        "provider": "baidu",
    })
    assert durable_response.status_code == 200, durable_response.text
    durable_payload = durable_response.json()
    scan_id = durable_payload["scan_id"]

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if get_durable_scan(database, scan_id)["status"] not in {"queued", "running"}:
            break
        time.sleep(0.02)
    final_state = get_durable_scan(database, scan_id)
    assert final_state["status"] == "completed", final_state

    assert durable_payload["root_id"] == sync_result["root_id"]
    assert durable_payload["effective_playback_root"] == sync_result["effective_playback_root"]
    assert durable_payload["effective_playback_root"] == str(mount / "01动画")

    with database.connect() as conn:
        validation = conn.execute(
            "SELECT root_id, effective_root FROM tree_scan_validation WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
    assert validation["root_id"] == sync_result["root_id"]
    assert validation["effective_root"] == sync_result["effective_playback_root"]
