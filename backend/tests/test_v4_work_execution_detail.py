"""作品级执行详情接口合同（3.1 P0）。

展开第三步的已完成作品必须能看到与数据库事实一致的结构化结果：
作品身份 / 镜像结果 / 剧集结果。详情只读现有投影表，不得启动任务、
不得写库、不得越权读取其他 revision 的作品。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "detail.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(media_v4.router)
    return TestClient(application), database


def _seed_confirmed_work(database, *, revision_id: str = "rev-detail", work_id: str = "w-detail"):
    """通过真实 draft/confirm 管线建立 2 集作品，再直接落执行结果事实。"""

    from app.media_v4.domain.models import ParsedFacts, SourceEvidence
    from app.media_v4.revisions.service import V4RevisionService

    pairs = []
    for index in (1, 2):
        locator = f"K:\\挂载\\Show\\Show.S01E0{index}.mkv"
        evidence = SourceEvidence(
            evidence_id=f"ev-{index}",
            scan_id=f"scan-{revision_id}",
            root_id=f"root-{revision_id}",
            source_key=f"Show/Show.S01E0{index}.mkv",
            relative_path=f"Show/Show.S01E0{index}.mkv",
            entry_kind="video",
            provider="local",
            ingest_method="local_scan",
            source_locator=locator,
            playback_locator=locator,
            fingerprint=f"fp-{index}",
        )
        facts = ParsedFacts(
            parsed_fact_id=f"facts-{index}",
            evidence_id=evidence.evidence_id,
            parser_version="fixture",
            work_title="Show",
            title_candidates=("Show",),
            media_type="tv",
            group_type="season",
            season_candidate=1,
            episode_candidate=index,
        )
        pairs.append((evidence, facts))

    service = V4RevisionService(database)
    service.create_draft(revision_id, pairs, root_id=f"root-{revision_id}", scan_id=f"scan-{revision_id}")
    service.confirm(revision_id)

    with database.connect() as conn:
        work_id = str(conn.execute("SELECT work_id FROM works WHERE preferred_title = 'Show'").fetchone()[0])
        local_episode_ids = {
            int(row["local_episode_number"]): str(row["episode_id"])
            for row in conn.execute(
                "SELECT episode_id, local_episode_number FROM episodes WHERE work_id = ?", (work_id,)
            ).fetchall()
        }
        # 执行结果事实：job 全部成功、镜像产物、刮削结果与剧集映射。
        for job_type in ("materialize_mirror", "scrape_work"):
            conn.execute(
                "UPDATE jobs SET status = 'succeeded', finished_at = 'now' "
                "WHERE revision_id = ? AND work_id = ? AND job_type = ?",
                (revision_id, work_id, job_type),
            )
        conn.execute(
            "INSERT INTO artifacts(artifact_id, revision_id, work_id, artifact_type, target_path, status, created_at, updated_at) "
            "VALUES ('art-1', ?, ?, 'mirror', 'K:\\镜像\\Show\\Season 01\\S01E01-abc.strm', 'published', 'now', 'now')",
            (revision_id, work_id),
        )
        metadata = {
            "provider": "tmdb",
            "provider_id": "12345",
            "media_type": "tv",
            "title": "Show",
            "metadata_state": "ready",
            "episode_mappings": [
                {
                    "episode_id": local_episode_ids[index],
                    "provider_season_number": 1,
                    "provider_episode_number": index,
                    "provider_episode_id": f"90{index}",
                    "title": f"远程第{index}集全名",
                    "plot": "",
                    "runtime": 24,
                    "still_url": "",
                }
                for index in (1, 2)
            ],
        }
        conn.execute(
            "INSERT INTO scrape_bindings(binding_id, revision_id, work_id, provider, provider_id, metadata_json, status, created_at, updated_at) "
            "VALUES ('sb-1', ?, ?, 'tmdb', '12345', ?, 'confirmed', 'now', 'now')",
            (revision_id, work_id, json.dumps(metadata, ensure_ascii=False)),
        )
        for index in (1, 2):
            conn.execute(
                "INSERT INTO episode_provider_mappings(episode_id, provider, provider_season_number, provider_episode_number, provider_episode_id) "
                "VALUES (?, 'tmdb', 1, ?, ?)",
                (local_episode_ids[index], index, f"90{index}"),
            )
    return work_id


@pytest.mark.parametrize(
    ("metadata", "action", "reason_fragments"),
    [
        (
            {
                "metadata_state": "source_unavailable",
                "reason_code": "episode_mapping_incomplete",
                "failure_stage": "season_detail",
                "season_results": [{
                    "local_season_number": 2,
                    "reason_code": "provider_auth_required",
                    "status": "source_unavailable",
                    "retryable": False,
                }],
            },
            "check_settings",
            ("第 2 季", "授权"),
        ),
        (
            {
                "metadata_state": "source_unavailable",
                "reason_code": "provider_resource_missing",
                "failure_stage": "work_detail",
            },
            "choose_candidate",
            ("在线作品", "不可用"),
        ),
        (
            {
                "metadata_state": "source_unavailable",
                "reason_code": "episode_mapping_incomplete",
                "failure_stage": "season_detail",
                "season_results": [{
                    "local_season_number": 0,
                    "reason_code": "episode_not_found",
                    "status": "partial",
                    "retryable": False,
                }],
            },
            "retry_metadata",
            ("特别篇", "集数"),
        ),
        (
            {
                "metadata_state": "source_unavailable",
                "reason_code": "episode_mapping_incomplete",
                "failure_stage": "season_detail",
                "season_results": [{
                    "local_season_number": 3,
                    "reason_code": "provider_resource_missing",
                    "status": "source_unavailable",
                    "retryable": False,
                }],
            },
            "retry_metadata",
            ("第 3 季", "不可用"),
        ),
        (
            {
                "metadata_state": "source_unavailable",
                "reason_code": "episode_mapping_incomplete",
                "failure_stage": "season_detail",
                "season_results": [
                    {"local_season_number": 1, "reason_code": "provider_rate_limited"},
                    {"local_season_number": 2, "reason_code": "episode_not_found"},
                ],
            },
            "retry_metadata",
            ("请求过于频繁", "集数未匹配"),
        ),
        (
            {"metadata_state": "failed", "reason_code": "invalid_response"},
            "retry_metadata",
            ("响应",),
        ),
        (
            {"metadata_state": "source_unavailable", "reason_code": "provider_resource_missing"},
            "retry_metadata",
            ("资料不可用",),
        ),
    ],
)
def test_metadata_recovery_policy_uses_nested_failure_context(metadata, action, reason_fragments):
    from app.media_v4.revisions.service import metadata_recovery_policy

    policy = metadata_recovery_policy(metadata, binding_status="confirmed")

    assert policy["action"] == action
    assert all(fragment in policy["reason"] for fragment in reason_fragments)
    assert policy["hint"]


def test_detail_projects_one_recovery_policy_and_special_season_failure(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)
    with database.connect() as conn:
        metadata = json.loads(conn.execute(
            "SELECT metadata_json FROM scrape_bindings WHERE binding_id = 'sb-1'"
        ).fetchone()[0])
        metadata.update({
            "metadata_state": "source_unavailable",
            "reason": "部分剧集资料暂不可用",
            "reason_code": "episode_mapping_incomplete",
            "failure_stage": "season_detail",
            "season_results": [{
                "local_season_number": 0,
                "provider_season_number": 0,
                "status": "partial",
                "reason_code": "episode_not_found",
                "failure_stage": "season_detail",
                "retryable": False,
            }],
        })
        conn.execute(
            "UPDATE scrape_bindings SET status = 'confirmed', metadata_json = ? WHERE binding_id = 'sb-1'",
            (json.dumps(metadata, ensure_ascii=False),),
        )

    response = client.get(f"/api/v4/revisions/rev-detail/works/{work_id}/execution-detail")

    assert response.status_code == 200, response.text
    body = response.json()
    from app.media_v4.revisions.service import V4RevisionService

    progress_unit = V4RevisionService(database).get_execution_progress("rev-detail")["work_units"][0]
    assert progress_unit["metadata_recovery_action"] == "retry_metadata"
    assert progress_unit["metadata_reason"] == body["work"]["metadata_reason"]
    assert body["work"]["metadata_recovery_action"] == "retry_metadata"
    assert "特别篇" in body["work"]["metadata_reason"]
    assert "核对" in body["work"]["metadata_recovery_hint"]
    assert body["scrape"]["metadata_recovery_action"] == body["work"]["metadata_recovery_action"]
    assert body["scrape"]["season_results"][0]["local_season_number"] == 0


def test_completed_work_returns_work_level_and_episode_results(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)

    response = client.get(f"/api/v4/revisions/rev-detail/works/{work_id}/execution-detail")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["work"]["title"] == "Show"
    assert body["work"]["provider"] == "tmdb"
    assert body["work"]["provider_id"] == "12345"
    assert body["work"]["metadata_state"] == "ready"
    # 镜像阶段结果与产物摘要（只给文件名，不暴露完整本地绝对路径）。
    assert body["mirror"]["status"] == "succeeded"
    assert body["mirror"]["artifact_count"] == 1
    assert body["mirror"]["artifacts"][0]["file_name"].endswith(".strm")
    assert "\\" not in body["mirror"]["artifacts"][0]["file_name"]
    # 剧集结果：本地季集号 + 刮削后完整集名 + 映射状态 + 播放定位生成。
    episodes = body["episodes"]
    assert len(episodes) == 2
    first = next(item for item in episodes if item["episode_number"] == 1)
    assert first["season_number"] == 1
    assert first["scraped_title"] == "远程第1集全名"
    assert first["scraped_plot"] == ""
    assert first["runtime"] == 24
    assert first["provider_episode_id"] == "901"
    assert first["mapped"] is True
    assert first["playback_ready"] is True
    assert first["file_name"].endswith(".mkv")
    assert body["seasons"] == [{"season_number": 1, "season_kind": "regular", "title": "", "episode_count": 2}]
    assert body["scrape"]["title"] == "Show"
    assert body["scrape"]["metadata_state"] == "ready"
    assert body["scrape"]["candidate_decision"] is None
    assert body["has_detail"] is True


def test_detail_only_returns_episodes_bound_to_requested_revision(tmp_path, monkeypatch):
    """同一 Work 的其他导入批次不能泄漏到当前展开详情。"""

    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)

    with database.connect() as conn:
        season_id = str(conn.execute(
            "SELECT season_id FROM seasons WHERE work_id = ? AND local_season_number = 1",
            (work_id,),
        ).fetchone()[0])
        conn.execute(
            """
            INSERT INTO import_revisions(
                revision_id, root_id, scan_id, resolver_version, status, created_at
            ) VALUES ('rev-other', 'root-rev-detail', 'scan-rev-detail', 'fixture', 'draft', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO episodes(
                episode_id, work_id, season_id, local_episode_number, episode_kind, display_title
            ) VALUES ('episode-from-other-revision', ?, ?, 3, 'regular', '只属于另一批次')
            """,
            (work_id, season_id),
        )
        conn.execute(
            """
            INSERT INTO revision_bindings(
                binding_id, revision_id, evidence_id, work_id, season_id, episode_id, confidence
            ) VALUES ('binding-other-revision', 'rev-other', 'ev-1', ?, ?,
                      'episode-from-other-revision', 'medium')
            """,
            (work_id, season_id),
        )

    response = client.get(f"/api/v4/revisions/rev-detail/works/{work_id}/execution-detail")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["episode_number"] for item in body["episodes"]] == [1, 2]
    assert all(item["episode_id"] != "episode-from-other-revision" for item in body["episodes"])


def test_detail_pages_episodes_and_never_uses_global_mapping_from_another_revision(tmp_path, monkeypatch):
    """详情页的集映射必须是本次导入快照，且大列表按页读取。"""

    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)
    with database.connect() as conn:
        episode_id = str(conn.execute(
            "SELECT episode_id FROM episodes WHERE work_id = ? AND local_episode_number = 1", (work_id,)
        ).fetchone()[0])
        # 这是后续某次导入更新的全局映射，不能污染 rev-detail 的执行证据。
        conn.execute(
            "UPDATE episode_provider_mappings SET provider_episode_id = 'leaked-global-id' "
            "WHERE episode_id = ? AND provider = 'tmdb'",
            (episode_id,),
        )

    response = client.get(f"/api/v4/revisions/rev-detail/works/{work_id}/execution-detail?episode_limit=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["episode_total"] == 2
    assert len(body["episodes"]) == 1
    assert body["episodes_truncated"] is True
    assert body["next_episode_offset"] == 1
    assert body["episodes"][0]["provider_episode_id"] == "901"
    assert body["episodes"][0]["provider_episode_number"] == 1


def test_detail_season_counts_are_not_limited_by_episode_page(tmp_path, monkeypatch):
    """季度统计必须覆盖整批 revision，不能只统计当前分页返回的剧集。"""

    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO seasons(season_id, work_id, local_season_number, season_kind) "
            "VALUES ('season-2', ?, 2, 'regular')",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO episodes(episode_id, work_id, season_id, local_episode_number, episode_kind, display_title) "
            "VALUES ('episode-3', ?, 'season-2', 1, 'regular', '第二季第一集')",
            (work_id,),
        )
        conn.execute(
            "INSERT INTO revision_bindings(binding_id, revision_id, evidence_id, work_id, season_id, episode_id, confidence) "
            "VALUES ('binding-season-2', 'rev-detail', 'ev-1', ?, 'season-2', 'episode-3', 'medium')",
            (work_id,),
        )

    response = client.get(f"/api/v4/revisions/rev-detail/works/{work_id}/execution-detail?episode_limit=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["episode_total"] == 3
    assert len(body["episodes"]) == 1
    assert {item["season_number"]: item["episode_count"] for item in body["seasons"]} == {1: 2, 2: 1}


def test_detail_returns_404_for_missing_work(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    _seed_confirmed_work(database)

    response = client.get("/api/v4/revisions/rev-detail/works/work-not-there/execution-detail")

    assert response.status_code == 404


def test_detail_rejects_work_from_other_revision(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database, revision_id="rev-a")

    response = client.get(f"/api/v4/revisions/rev-other/works/{work_id}/execution-detail")

    assert response.status_code == 404


def test_detail_read_does_not_write_database(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)

    def _counts():
        with database.connect() as conn:
            return {
                "jobs": int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]),
                "artifacts": int(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]),
                "scrape_bindings": int(conn.execute("SELECT COUNT(*) FROM scrape_bindings").fetchone()[0]),
                "revision_bindings": int(conn.execute("SELECT COUNT(*) FROM revision_bindings").fetchone()[0]),
                "episodes": int(conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]),
            }

    before = _counts()
    response = client.get(f"/api/v4/revisions/rev-detail/works/{work_id}/execution-detail")
    assert response.status_code == 200
    assert _counts() == before


def test_failed_work_reports_failed_stage(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)
    with database.connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'failed', last_error = '镜像目标已存在且内容不一致' "
            "WHERE revision_id = 'rev-detail' AND work_id = ? AND job_type = 'materialize_mirror'",
            (work_id,),
        )

    response = client.get(f"/api/v4/revisions/rev-detail/works/{work_id}/execution-detail")

    assert response.status_code == 200
    body = response.json()
    assert body["mirror"]["status"] == "failed"
    assert body["mirror"]["error"] == "任务未能完成，请重试"
    assert body["has_detail"] is True


def test_identity_conflict_is_not_reported_as_search_ambiguity(tmp_path, monkeypatch):
    from app.media_v4.revisions.service import V4RevisionService

    client, database = _client(tmp_path, monkeypatch)
    work_id = _seed_confirmed_work(database)
    with database.connect() as conn:
        meta = json.loads(conn.execute("SELECT metadata_json FROM scrape_bindings WHERE binding_id='sb-1'").fetchone()[0])
        meta.update(metadata_state='waiting_review', reason='该在线作品已关联到另一部作品，请返回检查识别结果或选择正确候选')
        conn.execute("UPDATE scrape_bindings SET status='waiting_review', metadata_json=? WHERE binding_id='sb-1'", (json.dumps(meta),))
    progress = V4RevisionService(database).get_execution_progress('rev-detail')
    reason = progress['work_units'][0]['metadata_reason']
    assert '关联' in reason
    assert '唯一匹配' not in reason
    response = client.get(f'/api/v4/revisions/rev-detail/works/{work_id}/execution-detail')
    assert response.json()['work']['metadata_reason'] == reason
