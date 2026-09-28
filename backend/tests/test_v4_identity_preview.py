"""STEP-006 / CHECK-006：历史错误 confirmed 的只读重建预览。

红线：预览不得改写任何事实行、绑定、镜像或观看记录；真实库修复需要单独授权。
"""

from __future__ import annotations

TITLE = "上伊那牡丹醉姿如百合"
NAMES = (
    [f"上伊那牡丹S01E{index}.mkv" for index in range(1, 8)]
    + ["8.mkv", "9.mkv", f"{TITLE}11.mp4", f"{TITLE}12.mp4"]
)
LEGACY_SECOND_WORK = "work-legacy-second"
REVISION_ID = "rev-old"


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "identity-preview.db")
    database.initialize()
    return database


def _legacy_confirmed_revision(database) -> list[str]:
    """手工搭建“历史错误形状”的 confirmed revision（不经过当前 Resolver）。

    形状：前 7 集属于 work-primary 且各有集号与 Asset；后 4 个文件属于
    第二个 Work，既无集号也无 Asset —— 正是 parser-3 时期的拆卡结果。
    """

    from app.media_v4.domain.models import ParsedFacts
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    evidence_items = [
        to_source_evidence(SourceEntry(
            root_id="root-old", scan_id="scan-old", provider="local", ingest_method="local_scan",
            relative_path=f"{TITLE}/{name}", source_key=f"{TITLE}/{name}",
            source_locator=f"local://{TITLE}/{name}", playback_locator=f"local://{TITLE}/{name}",
        ))
        for name in NAMES
    ]
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-old', 'local', 'local_scan', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO source_scans(scan_id, root_id, generation, status, stage, heartbeat_at, started_at) "
            "VALUES ('scan-old', 'root-old', 1, 'completed', 'ready', 'now', 'now')"
        )
    repository = V4Repository(database)
    repository.save_scan_evidence_bulk(evidence_items)
    repository.save_parsed_facts_bulk([
        ParsedFacts(
            parsed_fact_id=f"facts-old-{index}",
            evidence_id=item.evidence_id,
            parser_version="v4-parser-3",
        )
        for index, item in enumerate(evidence_items)
    ])

    primary_files = evidence_items[:7]
    legacy_files = evidence_items[7:]
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
            "VALUES ('work-primary', 'title:primary', 'series', '上伊那牡丹', 'now', 'now')"
        )
        conn.execute(
            "INSERT INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
            "VALUES (?, 'title:legacy-second', 'series', ?, 'now', 'now')",
            (LEGACY_SECOND_WORK, TITLE),
        )
        conn.execute(
            "INSERT INTO seasons(season_id, work_id, local_season_number, season_kind, identity_key) "
            "VALUES ('season-1', 'work-primary', 1, 'regular', '[\"local\",1]')"
        )
        for index, item in enumerate(primary_files, start=1):
            conn.execute(
                "INSERT INTO episodes(episode_id, work_id, season_id, local_episode_number, episode_kind, identity_key) "
                "VALUES (?, 'work-primary', 'season-1', ?, 'regular', ?)",
                (f"ep-{index}", index, f'["local",1,{index}]'),
            )
            conn.execute(
                "INSERT INTO assets(asset_id, evidence_id, root_id, source_locator, playback_locator) "
                "VALUES (?, ?, 'root-old', ?, ?)",
                (f"asset-{index}", item.evidence_id, f"local://{TITLE}/{NAMES[index - 1]}",
                 f"local://{TITLE}/{NAMES[index - 1]}"),
            )
        conn.execute(
            "INSERT INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, created_at) "
            "VALUES (?, 'root-old', 'scan-old', 'fixture', 'draft', 'now')",
            (REVISION_ID,),
        )
        for item in evidence_items:
            conn.execute(
                "INSERT INTO revision_evidence(revision_id, evidence_id, parsed_fact_id) VALUES (?, ?, ?)",
                (REVISION_ID, item.evidence_id, f"facts-old-{evidence_items.index(item)}"),
            )
        for index, item in enumerate(primary_files, start=1):
            conn.execute(
                "INSERT INTO revision_bindings(binding_id, revision_id, evidence_id, work_id, season_id, episode_id, asset_id) "
                "VALUES (?, ?, ?, 'work-primary', 'season-1', ?, ?)",
                (f"bind-{index}", REVISION_ID, item.evidence_id, f"ep-{index}", f"asset-{index}"),
            )
        for index, item in enumerate(legacy_files, start=11):
            conn.execute(
                "INSERT INTO revision_bindings(binding_id, revision_id, evidence_id, work_id) "
                "VALUES (?, ?, ?, ?)",
                (f"bind-legacy-{index}", REVISION_ID, item.evidence_id, LEGACY_SECOND_WORK),
            )
        conn.execute(
            "INSERT INTO playback_progress(episode_id, asset_id, work_id, position, duration, completed, updated_at) "
            "VALUES ('ep-1', 'asset-1', 'work-primary', 10.0, 100.0, 0, 'now')"
        )
        conn.execute(
            "INSERT INTO playback_history(event_id, work_id, episode_id, asset_id, played_at) "
            "VALUES ('evt-1', 'work-primary', 'ep-1', 'asset-1', 'now')"
        )
        conn.execute(
            "UPDATE import_revisions SET status = 'confirmed', confirmed_at = 'now' WHERE revision_id = ?",
            (REVISION_ID,),
        )
    return [item.evidence_id for item in evidence_items]


def _snapshot(database) -> dict:
    tables = (
        "works", "revision_bindings", "parsed_facts", "source_evidence",
        "playback_progress", "playback_history", "episodes", "assets",
    )
    with database.connect() as conn:
        return {table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}


def _revision_status(database, revision_id: str) -> str:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
    return str(row["status"] if row else "")


def test_preview_reports_legacy_split_and_merges_without_writing(tmp_path):
    from app.media_v4.maintenance.identity_preview import preview_revision_reparse

    database = _database(tmp_path)
    _legacy_confirmed_revision(database)
    before = _snapshot(database)
    status_before = _revision_status(database, REVISION_ID)

    preview = preview_revision_reparse(database, REVISION_ID)

    assert preview["read_only"] is True
    assert preview["writes_performed"] is False
    assert preview["requires_explicit_authorization"] is True
    assert preview["old"]["work_count"] == 2, preview["old"]
    assert preview["new"]["work_count"] == 1, preview["new"]
    assert preview["new"]["parser_version"].startswith("v4-parser-")
    assert preview["new"]["episode_count"] == 11
    assert preview["new"]["issue_codes"] == [], preview["new"]

    merges = preview["work_merges"]
    assert len(merges) == 1, merges
    assert sorted(merges[0]["old_work_ids"]) == ["work-legacy-second", "work-primary"]
    assert merges[0]["new_work_title"] == TITLE

    legacy_files = [row for row in preview["files"] if row["old_work_id"] == LEGACY_SECOND_WORK]
    assert len(legacy_files) == 4
    assert sorted(row["new_local_episode"] for row in legacy_files) == [8, 9, 11, 12]
    assert all(row["new_work_key"].startswith("title:") for row in legacy_files)
    assert all(not row["placement_lost"] for row in preview["files"])

    preservation = preview["preservation"]
    assert preservation["playback_progress_rows"] == 1
    assert preservation["playback_history_rows"] == 1
    assert preservation["provider_binding_rows"] == 0
    assert preservation["other_source_reference_rows"] == 11

    # 只读：事实行、观看记录与 revision 状态都没变。
    assert _snapshot(database) == before
    assert _revision_status(database, REVISION_ID) == status_before

    # 重复预览结果一致，且仍无副作用。
    assert preview_revision_reparse(database, REVISION_ID) == preview
    assert _snapshot(database) == before


def test_preview_on_missing_revision_raises_key_error(tmp_path):
    from app.media_v4.maintenance.identity_preview import preview_revision_reparse

    database = _database(tmp_path)
    try:
        preview_revision_reparse(database, "rev-does-not-exist")
    except KeyError:
        return
    raise AssertionError("缺失 revision 必须抛 KeyError")
