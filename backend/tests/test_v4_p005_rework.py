"""P-005 返工：维护领域合同（11.13 #3-#6）回归。

多 root 混合来源必须以 selected_root_ids 集合判定；preview 必须持久化并
校验 TTL/状态；孤儿 Work 的播放历史必须与 preview/confirm 一致；operation
状态机不得把失败伪装成 completed。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "p005r.db")
    database.initialize()
    return database


def _seed_root(database, *, root_id, provider, locator="/Anime"):
    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO source_roots(root_id, provider, ingest_method, source_locator, playback_locator, created_at, updated_at) "
            "VALUES (?, ?, 'openlist_scan', ?, ?, 'now', 'now')",
            (root_id, provider, locator, f"K:{locator}"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO source_scans(scan_id, root_id, generation, status) VALUES (?, ?, 1, 'completed')",
            (f"scan-{root_id}", root_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO import_revisions(revision_id, root_id, scan_id, resolver_version, status, graph_digest, created_at, confirmed_at) "
            "VALUES (?, ?, ?, 'v4', 'confirmed', '', 'now', 'now')",
            (f"rev-{root_id}", root_id, f"scan-{root_id}"),
        )


def _seed_work_in_root(database, *, root_id, work_id, title, history=False):
    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO works(work_id, identity_key, work_type, preferred_title, created_at, updated_at) "
            "VALUES (?, ?, 'series', ?, 'now', 'now')",
            (work_id, f"ik-{work_id}", title),
        )
        evidence_id = f"ev-{root_id}-{work_id}"
        conn.execute(
            "INSERT OR IGNORE INTO source_evidence(evidence_id, scan_id, root_id, provider, source_key, relative_path, entry_kind, source_locator, playback_locator, ingest_method, observed_at) "
            "VALUES (?, ?, ?, 'pan115', ?, ?, 'video', ?, ?, 'openlist_api', 'now')",
            (evidence_id, f"scan-{root_id}", root_id, f"rel-{root_id}-{work_id}", f"rel-{root_id}-{work_id}", f"K:{root_id}\\{work_id}.mkv", f"K:{root_id}\\{work_id}.mkv"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO parsed_facts(parsed_fact_id, evidence_id, parser_version, work_title, title_candidates_json, media_type, group_type) "
            "VALUES (?, ?, 'fixture', 'x', '[]', 'tv', 'season')",
            (f"facts-{evidence_id}", evidence_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO revision_evidence(revision_id, evidence_id, parsed_fact_id) VALUES (?, ?, ?)",
            (f"rev-{root_id}", evidence_id, f"facts-{evidence_id}"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO revision_bindings(binding_id, revision_id, evidence_id, work_id, confidence) "
            "VALUES (?, ?, ?, ?, 'high')",
            (f"b-{root_id}-{work_id}", f"rev-{root_id}", evidence_id, work_id),
        )
        if history:
            conn.execute(
                "INSERT OR IGNORE INTO playback_history(event_id, work_id, episode_id, asset_id, played_at, title_snapshot) "
                "VALUES (?, ?, '', '', 'now', ?)",
                (f"hist-{work_id}", work_id, title),
            )
            conn.execute(
                "INSERT OR IGNORE INTO playback_progress(episode_id, asset_id, work_id, position, duration, updated_at) "
                "VALUES ('ep', 'as', ?, 1, 10, 'now')",
                (work_id,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO tracking_states(work_id, provider, provider_id, metadata_json, updated_at) "
                "VALUES (?, 'local', '', '{}', 'now')",
                (work_id,),
            )


def _preview(database, **kwargs):
    from app.media_v4.maintenance.service import compute_delete_preview

    return compute_delete_preview(database, **kwargs)


def _confirm(database, **kwargs):
    from app.media_v4.maintenance.service import confirm_delete_preview

    return confirm_delete_preview(database, **kwargs)


def test_multi_root_mixed_work_is_orphan_when_all_selected(tmp_path):
    """11.13-3：同一 Work 属于两个被选 root（scope=all / 同 provider 多 root）→
    删除后无活动来源，必须标为 orphan（将退出），而不是 mixed/保留。"""

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a", provider="pan115", locator="/A")
    _seed_root(database, root_id="root-b", provider="pan115", locator="/B")
    _seed_work_in_root(database, root_id="root-a", work_id="w-both", title="双来源")
    _seed_work_in_root(database, root_id="root-b", work_id="w-both", title="双来源")
    _seed_work_in_root(database, root_id="root-a", work_id="w-only", title="独有")
    _seed_work_in_root(database, root_id="root-b", work_id="w-other", title="其它")

    preview = _preview(database, provider="all")
    assert "w-both" in preview["orphan_works"], "同时属于全部被选 root 的作品必须退出"
    assert "w-both" not in preview["mixed_works"]
    assert "w-only" in preview["orphan_works"]
    assert "w-other" in preview["orphan_works"]
    assert preview["mixed_work_count"] == 0


def test_same_provider_multi_root_with_unselected_third_root(tmp_path):
    """11.13-3b：同 provider 三个 root，只选两个时，同时属于第三个未选 root 的
    作品仍应保留为 mixed。"""

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a", provider="pan115", locator="/A")
    _seed_root(database, root_id="root-b", provider="pan115", locator="/B")
    _seed_root(database, root_id="root-c", provider="pan115", locator="/C")
    _seed_work_in_root(database, root_id="root-a", work_id="w-ab", title="AB")
    _seed_work_in_root(database, root_id="root-b", work_id="w-ab", title="AB")
    _seed_work_in_root(database, root_id="root-c", work_id="w-ab", title="AB")
    _seed_work_in_root(database, root_id="root-a", work_id="w-ac", title="AC")
    _seed_work_in_root(database, root_id="root-c", work_id="w-ac", title="AC")

    preview = _preview(database, provider="pan115", root_ids=["root-a", "root-b"])
    # w-ab：root-c 未选 → mixed 保留
    assert "w-ab" in preview["mixed_works"]
    assert "w-ab" not in preview["orphan_works"]
    # w-ac：仅 root-a 选中，root-c 未选 → mixed 保留
    assert "w-ac" in preview["mixed_works"]


def test_preview_is_persisted_and_confirm_validates_ttl(tmp_path):
    """11.13-4：preview 必须持久化；过期、篡改或状态变化必须 409。"""

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a", provider="pan115", locator="/A")
    _seed_work_in_root(database, root_id="root-a", work_id="w-only", title="独有")

    preview = _preview(database, provider="pan115")
    # 篡改 digest → 409
    with pytest.raises(ValueError, match="重新生成"):
        _confirm(database, preview_id=preview["preview_id"], scope="pan115", digest="0" * 64)
    # 过期（将存储记录时间改为过去）→ 409
    with database.connect() as conn:
        conn.execute(
            "UPDATE maintenance_operations SET created_at = ?, updated_at = ? WHERE operation_id = ?",
            ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), (datetime.now(UTC) - timedelta(hours=2)).isoformat(), preview["preview_id"]),
        )
    with pytest.raises(ValueError, match="过期"):
        _confirm(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"])


def test_orphan_history_and_progress_are_removed_and_counted(tmp_path):
    """11.13-5：孤儿 Work 的 playback_history/progress/tracking 随媒体库退出，
    preview 统计 history/progress 影响。"""

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a", provider="pan115", locator="/A")
    _seed_work_in_root(database, root_id="root-a", work_id="w-only", title="独有", history=True)

    preview = _preview(database, provider="pan115")
    assert preview["history_count"] == 1
    assert preview["progress_count"] == 1
    assert preview["tracking_count"] == 1

    _confirm(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"])
    with database.connect() as conn:
        hist = conn.execute("SELECT 1 FROM playback_history WHERE work_id = 'w-only'").fetchone()
        prog = conn.execute("SELECT 1 FROM playback_progress WHERE work_id = 'w-only'").fetchone()
        track = conn.execute("SELECT 1 FROM tracking_states WHERE work_id = 'w-only'").fetchone()
    assert hist is None
    assert prog is None
    assert track is None


def test_operation_state_machine_reports_partial_and_projection_failure(tmp_path, monkeypatch):
    """11.13-6：operation 状态机不得把失败伪装成 completed。"""

    from app.media_v4.maintenance import service as maintenance_service
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a", provider="pan115", locator="/A")
    _seed_work_in_root(database, root_id="root-a", work_id="w-only", title="独有")
    # 制造一个会失败的 artifact 文件
    mirror = tmp_path / "mirror"
    artifact = mirror / "root-a" / "w-only" / "poster.jpg"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("x", encoding="utf-8")
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO artifacts(artifact_id, revision_id, work_id, artifact_type, target_path, digest, status, created_at, updated_at) "
            "VALUES ('art-1', 'rev-root-a', 'w-only', 'poster', ?, 'd', 'staged', 'now', 'now')",
            (str(artifact),),
        )

    # 投影失败：patch rebuild
    def broken_rebuild():
        raise RuntimeError("投影引擎不可用")

    monkeypatch.setattr(maintenance_service.V4LibraryProjection, "rebuild", broken_rebuild)
    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    result = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"], mirror_root=mirror)
    assert result["status"] == "projection_failed"
    assert result["artifact_results"][0]["status"] == "removed"
    with database.connect() as conn:
        op = conn.execute(
            "SELECT status FROM maintenance_operations WHERE operation_id = ?",
            (preview["preview_id"],),
        ).fetchone()
    assert op["status"] == "projection_failed"


def test_confirm_unknown_preview_is_rejected(tmp_path):
    """11.13-4b：不存在的 preview_id 必须拒绝，不能凭空确认。"""

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a", provider="pan115", locator="/A")
    with pytest.raises(ValueError):
        _confirm(database, preview_id="prev-missing", scope="pan115", digest="0" * 64)
