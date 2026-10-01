"""STEP-001：来源卡阶段化计量（F-001 / F-002）。

- 草稿的作品预览与 confirmed 的绑定集合不能拼成一组无阶段说明的“规模”。
- 「已刮削成功」不能由 `work_count - attention_count` 推算；在线资料就绪量只来自
  confirmed 当前作品的 `metadata_state`，并区分本次刷新成功与沿用旧快照。
"""

from __future__ import annotations


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "card-scope.db")
    database.initialize()
    return database


def _pair(prefix: str, name: str, *, content_class: str = "regular", media_type: str = "tv",
          season: int | None = 1, episode: int | None = 1, episode_range: tuple[int, int] | None = None):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=f"ev-{prefix}",
        scan_id="scan-scope",
        root_id="root-scope",
        source_key=name,
        relative_path=f"Show/{name}",
        entry_kind="video",
        source_locator=f"local://show/{name}",
        playback_locator=f"local://show/{name}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{prefix}",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type=media_type,
        group_type="season",
        season_candidate=season,
        episode_candidate=episode,
        episode_range=episode_range,
        content_class=content_class,
    )
    return evidence, facts


def _draft_card(tmp_path, pairs):
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.revisions.service import V4RevisionService

    database = _database(tmp_path)
    V4RevisionService(database).create_draft("rev-scope", pairs, root_id="root-scope", scan_id="scan-scope")
    return database, list_source_cards(database)[0]


def test_draft_scope_reports_admitted_files_not_scan_scale(tmp_path):
    pairs = [
        _pair("e1", "Show.S01E01.mkv", episode=1),
        _pair("e2", "Show.S01E02.mkv", episode=2),
        _pair("e3", "Show.S01E03.mkv", episode=3),
    ]
    _, card = _draft_card(tmp_path, pairs)

    assert card["counts_scope"] == "draft"
    assert card["work_count"] == 1
    assert card["admitted_video_file_count"] == 3
    assert card["asset_count"] == 3
    assert card["episode_count"] == 3
    # 草稿阶段没有“在线资料已就绪”这种事实，必须是未知而不是 0 或推算值。
    assert card["metadata_ready_work_count"] is None
    assert card["metadata_ready_current_count"] is None
    assert card["metadata_ready_retained_count"] is None


def test_multiple_episodes_in_one_file_count_once_as_video(tmp_path):
    pairs = [_pair("range", "Show.S01E01-E02.mkv", episode=1, episode_range=(1, 2))]
    _, card = _draft_card(tmp_path, pairs)

    assert card["admitted_video_file_count"] == 1, "一个物理文件只算一个视频文件"
    assert card["episode_count"] == 2
    assert card["asset_count"] == 1


def test_two_editions_of_one_episode_count_two_assets_one_episode(tmp_path):
    pairs = [
        _pair("ed-a", "Show.S01E01.1080p.mkv", episode=1),
        _pair("ed-b", "Show.S01E01.720p.mkv", episode=1),
    ]
    _, card = _draft_card(tmp_path, pairs)

    assert card["admitted_video_file_count"] == 2
    assert card["asset_count"] == 2
    assert card["episode_count"] == 1


def test_excluded_specials_are_counted_separately_and_not_admitted(tmp_path):
    pairs = [
        _pair("e1", "Show.S01E01.mkv", episode=1),
        _pair("sp", "Show.SP01.mkv", content_class="attached_special", episode=None, season=None),
    ]
    _, card = _draft_card(tmp_path, pairs)

    assert card["admitted_video_file_count"] == 1, "特别篇不进入正片准入集合"
    assert card["excluded_video_count"] == 1
    assert card["observed_video_count"] == 2


def test_confirmed_scope_reads_metadata_state_and_separates_retained(tmp_path):
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.revisions.service import V4RevisionService

    database = _database(tmp_path)
    service = V4RevisionService(database)
    pairs = [
        _pair("w1", "A/A.S01E01.mkv"),
        _pair("w2", "B/B.S01E01.mkv"),
        _pair("w3", "C/C.S01E01.mkv"),
    ]
    service.create_draft("rev-scope", pairs, root_id="root-scope", scan_id="scan-scope")
    service.confirm("rev-scope")
    with database.connect() as conn:
        conn.execute("UPDATE jobs SET status = 'succeeded' WHERE revision_id = 'rev-scope'")
        work_ids = [str(row["work_id"]) for row in conn.execute(
            "SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id = 'rev-scope' ORDER BY work_id"
        ).fetchall()]
        assert len(work_ids) >= 2, "夹具需要多个作品才能验证就绪比例"
        states = ["ready", "waiting_metadata"] + ["ready"] * (len(work_ids) - 2)
        for index, work_id in enumerate(work_ids):
            metadata = {"metadata_state": states[index]}
            if states[index] == 'ready':
                # 计数夹具也须满足分集资料门控，不能仅声明 ready。
                metadata['episode_mappings'] = [
                    {'episode_id': row['episode_id'], 'provider_episode_id': 'fixture-' + row['episode_id'],
                     'title': '完整分集标题', 'still_url': 'https://image.tmdb.org/t/p/w500/fixture.jpg'}
                    for row in conn.execute('SELECT episode_id FROM episodes WHERE work_id=?', (work_id,))
                ]
            if index == 0:
                metadata["metadata_source"] = "current"
            if index == 1:
                metadata["metadata_source"] = "retained"
            conn.execute(
                "INSERT INTO scrape_bindings(binding_id, revision_id, work_id, provider, provider_id, "
                "metadata_json, status, created_at, updated_at) VALUES (?, 'rev-scope', ?, 'tmdb', ?, ?, 'confirmed', 'now', 'now')",
                (f"sb-{index}", work_id, str(index), __import__("json").dumps(metadata)),
            )

    card = list_source_cards(database)[0]
    assert card["counts_scope"] == "confirmed"
    assert card["work_count"] == len(work_ids)
    assert card["admitted_video_file_count"] == len(work_ids), "每个作品一个物理文件"
    assert card["metadata_ready_work_count"] == len(work_ids) - 1
    assert card["metadata_ready_current_count"] + card["metadata_ready_retained_count"] == card["metadata_ready_work_count"]


def test_scan_scope_leaves_works_unknown_instead_of_zero(tmp_path):
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    database = _database(tmp_path)
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-scan', 'local', 'local_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-only', 'root-scan', 1, 'completed', 'ready', 'now', 'now')"
        )
    V4Repository(database).save_scan_evidence_bulk([
        to_source_evidence(SourceEntry(
            root_id="root-scan", scan_id="scan-only", provider="local", ingest_method="local_scan",
            relative_path=f"Show/F{index}.mkv", source_key=f"Show/F{index}.mkv",
        ))
        for index in range(2)
    ])

    card = list_source_cards(database)[0]
    assert card["counts_scope"] == "scan"
    assert card["work_count"] is None, "扫描阶段还没有作品归属，未知不是 0"
    assert card["admitted_video_file_count"] is None
    assert card["observed_video_count"] == 2
    assert card["observed_entry_count"] == 2
