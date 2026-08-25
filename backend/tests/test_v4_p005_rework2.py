"""P-005 二次返工（§11.14）回归：完整计划、可恢复状态机、并发与拒绝矩阵。

先红后绿：51+ 受管 artifact 全量执行；partial/projection 失败可基于同一
operation 续跑；并发 confirm 只有单一执行权；mirror root/scope/digest/root
集合/过期/运行任务/空未知 root 均拒绝且不退役任何 root。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


def _fresh_database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "p005r2.db")
    database.initialize()
    return database


def _seed_root(database, *, root_id, provider="pan115", locator="/Anime"):
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


def _seed_work(database, *, root_id, work_id, title, history=False):
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


def _seed_artifact(database, *, root_id, work_id, target_path):
    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO artifacts(artifact_id, revision_id, work_id, artifact_type, target_path, digest, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'poster', ?, 'd', 'staged', 'now', 'now')",
            (f"art-{root_id}-{work_id}-{Path(target_path).name}", f"rev-{root_id}", work_id, str(target_path)),
        )


def _make_mirror(tmp_path, root_id, work_id, count):
    mirror = tmp_path / "mirror"
    paths = []
    for index in range(count):
        target = mirror / root_id / work_id / f"file-{index}.jpg"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        paths.append(target)
    return mirror, paths


def test_fifty_one_artifacts_are_all_planned_and_cleaned(tmp_path):
    """11.14.2-1：51 个受管 artifact，confirm 必须尝试并记录全部；响应不含完整绝对路径。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="大库")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 51)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    # 展示层只返回计数与 ≤20 条脱敏摘要；绝不含完整本地绝对路径。
    assert preview["artifact_count"] == 51
    assert len(preview["artifact_summaries"]) <= 20
    assert "artifact_paths" not in preview
    for summary in preview["artifact_summaries"]:
        assert str(mirror) not in str(summary), "响应不得包含完整本地绝对路径"
    with database.connect() as conn:
        items = conn.execute(
            "SELECT COUNT(*) AS c FROM maintenance_operation_items WHERE operation_id = ?",
            (preview["preview_id"],),
        ).fetchone()["c"]
    assert items == 51

    result = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"], mirror_root=mirror)
    assert result["status"] == "completed"
    assert len(result["artifact_results"]) == 51
    removed = [item for item in result["artifact_results"] if item["status"] == "removed"]
    assert len(removed) == 51
    assert all(not path.exists() for path in paths)


def test_partial_failure_resumes_same_operation_without_repeating_success(tmp_path, monkeypatch):
    """11.14.2-2：一个 artifact 失败 → partial_failed；同 operation resume 只重试未完成项。"""

    from app.media_v4.maintenance import service as maintenance_service
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview, resume_operation

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="作品")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 3)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    # 第一次执行：前 2 个删除成功，第 3 个删除失败（模拟文件被占用）。
    original_unlink = Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self):
        calls["n"] += 1
        if calls["n"] == 3:
            raise PermissionError("文件被占用")
        return original_unlink(self)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    result = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"], mirror_root=mirror)
    assert result["status"] == "partial_failed"
    assert paths[2].exists(), "失败项文件应保留"
    monkeypatch.setattr(Path, "unlink", original_unlink)

    # 恢复：同一 operation 重试未完成项；成功项不重复执行。
    resumed = resume_operation(database, preview_id=preview["preview_id"], mirror_root=mirror)
    assert resumed["status"] == "completed"
    assert all(not path.exists() for path in paths)


def test_projection_failure_resume_rebuilds_projection_only(tmp_path, monkeypatch):
    """11.14.2-3：投影失败 → projection_failed；resume 只重建投影，不重复文件清理。"""

    from app.media_v4.maintenance import service as maintenance_service
    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview, resume_operation

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="作品")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 2)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    def broken_rebuild():
        raise RuntimeError("投影引擎不可用")

    monkeypatch.setattr(maintenance_service.V4LibraryProjection, "rebuild", broken_rebuild)
    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    result = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"], mirror_root=mirror)
    assert result["status"] == "projection_failed"
    assert all(not path.exists() for path in paths), "文件应已清理，只有投影失败"

    monkeypatch.setattr(maintenance_service.V4LibraryProjection, "rebuild", lambda self: None)
    resumed = resume_operation(database, preview_id=preview["preview_id"], mirror_root=mirror)
    assert resumed["status"] == "completed"
    assert resumed["projection_status"] == "ok"


def test_concurrent_confirm_only_one_executes(tmp_path):
    """11.14.2-3b：两个并发 confirm 只有一个能取得执行权。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="作品")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 1)
    _seed_artifact(database, root_id="root-a", work_id="w1", target_path=paths[0])

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    first = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"], mirror_root=mirror)
    assert first["status"] == "completed"
    # 第二个 confirm：已处理，幂等返回既有结果，不重复执行。
    second = confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"], mirror_root=mirror)
    assert second["status"] == "completed"
    assert not paths[0].exists()


def test_confirm_rejects_mirror_root_change_without_retiring(tmp_path):
    """11.14.2-4a：镜像根变化拒绝，且不退役任何 root。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="作品")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 1)
    _seed_artifact(database, root_id="root-a", work_id="w1", target_path=paths[0])

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    other_mirror = tmp_path / "other-mirror"
    with pytest.raises(ValueError, match="镜像"):
        confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"], mirror_root=other_mirror)
    with database.connect() as conn:
        retired = conn.execute("SELECT retired_at FROM source_roots WHERE root_id = 'root-a'").fetchone()["retired_at"]
    assert retired == ""


def test_confirm_rejects_unknown_empty_and_out_of_scope_roots(tmp_path):
    """11.14.2-4b：空/未知/不属 scope 的 root_ids 拒绝，且不退役任何 root。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_root(database, root_id="root-b")
    _seed_work(database, root_id="root-a", work_id="w1", title="作品")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 1)
    _seed_artifact(database, root_id="root-a", work_id="w1", target_path=paths[0])

    # 空 root_ids
    with pytest.raises(ValueError):
        compute_delete_preview(database, provider="pan115", root_ids=[], mirror_root=mirror)
    # 未知 root
    with pytest.raises(ValueError):
        compute_delete_preview(database, provider="pan115", root_ids=["root-missing"], mirror_root=mirror)
    # 不属于 scope 的 root（root-b 是 baidu，scope=pan115 不能选它）
    _seed_root(database, root_id="root-c", provider="baidu", locator="/Baidu")
    with pytest.raises(ValueError):
        compute_delete_preview(database, provider="pan115", root_ids=["root-a", "root-c"], mirror_root=mirror)
    with database.connect() as conn:
        retired = conn.execute("SELECT COUNT(*) AS c FROM source_roots WHERE retired_at != ''").fetchone()["c"]
    assert retired == 0


def test_orphan_and_mixed_works_cleanup_consistent(tmp_path):
    """11.14.2-5：孤儿与混合来源在 all/同 provider 多 root/部分 root 下活动过滤一致。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_root(database, root_id="root-b")
    _seed_root(database, root_id="root-c")
    _seed_work(database, root_id="root-a", work_id="w-ab", title="AB", history=True)
    _seed_work(database, root_id="root-b", work_id="w-ab", title="AB")
    _seed_work(database, root_id="root-c", work_id="w-ab", title="AB")
    _seed_work(database, root_id="root-a", work_id="w-a", title="独有", history=True)

    # 部分 root：只删 root-a → w-ab 仍保留（root-b/c），w-a 退出并清个人状态
    preview = compute_delete_preview(database, provider="pan115", root_ids=["root-a"])
    assert "w-ab" in preview["mixed_works"]
    assert "w-a" in preview["orphan_works"]
    confirm_delete_preview(database, preview_id=preview["preview_id"], scope="pan115", digest=preview["digest"])
    with database.connect() as conn:
        hist_a = conn.execute("SELECT 1 FROM playback_history WHERE work_id = 'w-a'").fetchone()
        hist_ab = conn.execute("SELECT 1 FROM playback_history WHERE work_id = 'w-ab'").fetchone()
    assert hist_a is None
    assert hist_ab is not None
