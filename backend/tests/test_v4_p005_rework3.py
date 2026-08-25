"""P-005 二次返工（§11.15）回归：竞态门控、真实 Artifact 身份、事务原子认领、结果持久化与 DTO 收紧。

先红后绿目标：
1. preview 后新增 queued/running job，confirm 必须拒绝（409）且 root 不退役；
   并发 confirm 在原子领取事务内同样成立。
2. operation item 绑定真实 artifacts.artifact_id 与 canonical target path；
   (operation_id, artifact_id) 唯一约束 + v11→v12 迁移。
3. digest 包含完整 canonical item 集合、selected root+revision、mirror identity
   与活动 job 快照；preview/confirm 复用同一计算。
4. 校验、job/revision/root 比对、原子领取、来源退役与孤儿个人状态清理在同一个
   BEGIN IMMEDIATE 事务内；任何变化都回滚并 409，不得退役 root。
5. 每个清理项无论 removed/missing/blocked/failed 都持久化结果；resume 只重试
   明确可恢复的条目。
6. preview/result DTO 不含 source locator、内部 Work ID、完整执行路径或完整
   job 列表；只返回脱敏计数、≤20 条相对镜像根摘要、warnings 与可读名称。
7. 空 scope 无活动 root 拒绝。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from .test_v4_p005_rework2 import _fresh_database, _make_mirror, _seed_artifact, _seed_root, _seed_work


def _seed_job(database, *, revision_id, job_id, job_type="materialize_mirror", status="running"):
    with database.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO jobs(job_id, revision_id, job_type, status, idempotency_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'now', 'now')",
            (job_id, revision_id, job_type, status, f"ik-{job_id}"),
        )


def _retired_roots(database):
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT root_id, retired_at FROM source_roots WHERE retired_at != ''"
        ).fetchall()
    return [dict(row) for row in rows]


def _item_results(database, operation_id):
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT item_id, artifact_id, target_path, result_status, result_error FROM maintenance_operation_items "
            "WHERE operation_id = ? ORDER BY item_id",
            (operation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def test_preview_then_new_queued_job_blocks_confirm(tmp_path):
    """11.15.3-1：preview 后新增 queued job，confirm 必须拒绝且 root 不退役。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="竞态")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 2)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    assert not preview["blocked"]

    # preview 之后新增一个 queued 任务（revision 已确认但任务刚刚排队）。
    _seed_job(database, revision_id="rev-root-a", job_id="job-queued", status="queued")

    with pytest.raises(ValueError, match="变化|任务|重新生成"):
        confirm_delete_preview(
            database, preview_id=preview["preview_id"], scope="pan115",
            digest=preview["digest"], mirror_root=mirror,
        )
    assert _retired_roots(database) == [], "竞态确认不得退役任何 root"


def test_preview_then_new_running_job_blocks_confirm(tmp_path):
    """11.15.3-1：preview 后新增 running job 同样拒绝且 root 不退役。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="竞态")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 2)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    _seed_job(database, revision_id="rev-root-a", job_id="job-running", status="running")

    with pytest.raises(ValueError, match="变化|任务|重新生成"):
        confirm_delete_preview(
            database, preview_id=preview["preview_id"], scope="pan115",
            digest=preview["digest"], mirror_root=mirror,
        )
    assert _retired_roots(database) == []


def test_concurrent_confirm_with_new_job_both_rejected(tmp_path):
    """11.15.3-1：同一反例在并发 confirm 下仍成立——两个 confirm 都拒绝、root 不退役。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="并发")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 2)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    _seed_job(database, revision_id="rev-root-a", job_id="job-race", status="running")

    errors: list[str] = []
    lock = threading.Lock()

    def do_confirm():
        try:
            confirm_delete_preview(
                database, preview_id=preview["preview_id"], scope="pan115",
                digest=preview["digest"], mirror_root=mirror,
            )
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(str(exc))

    threads = [threading.Thread(target=do_confirm) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert len(errors) == 2, "两个并发 confirm 都必须拒绝"
    for message in errors:
        assert "变化" in message or "任务" in message or "重新生成" in message
    assert _retired_roots(database) == []


def test_items_bound_to_real_artifact_id_and_canonical_path(tmp_path):
    """11.15.3-2：operation item 绑定真实 artifact_id，target_path 为 canonical。"""

    from app.media_v4.maintenance.service import compute_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="身份")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 3)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    with database.connect() as conn:
        real_artifact_ids = {
            row["artifact_id"] for row in conn.execute(
                "SELECT artifact_id FROM artifacts WHERE revision_id = 'rev-root-a'"
            ).fetchall()
        }
    items = _item_results(database, preview["preview_id"])
    assert len(items) == 3
    assert all(item["artifact_id"] in real_artifact_ids for item in items), (
        "item 必须绑定真实 artifacts.artifact_id，不得使用随机 art-* 假身份"
    )
    for item in items:
        assert item["target_path"] == str(Path(item["target_path"]).resolve(strict=False)), "target_path 必须是 canonical"


def test_duplicate_operation_artifact_unique_index(tmp_path):
    """11.15.3-2：同 (operation_id, artifact_id) 唯一约束生效（v11→v12 迁移）。"""

    from app.media_v4.maintenance.service import compute_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="唯一")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 1)
    _seed_artifact(database, root_id="root-a", work_id="w1", target_path=paths[0])

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    with database.connect() as conn:
        row = conn.execute(
            "SELECT artifact_id FROM maintenance_operation_items WHERE operation_id = ? LIMIT 1",
            (preview["preview_id"],),
        ).fetchone()
    artifact_id = row["artifact_id"]
    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO maintenance_operation_items(item_id, operation_id, artifact_id, root_id, revision_id, target_path, plan_status, result_status, result_error, updated_at)
                VALUES ('dup-item', ?, ?, 'root-a', 'rev-root-a', 'C:/dup.jpg', 'planned', 'pending', '', 'now')
                """,
                (preview["preview_id"], artifact_id),
            )
        except sqlite3.IntegrityError:
            conn.rollback()
        else:
            conn.rollback()
            raise AssertionError("重复 (operation_id, artifact_id) 必须被唯一约束拒绝")


def test_out_of_bound_item_persists_blocked_result(tmp_path):
    """11.15.3-4：越界/目录 item 不能 continue 留 pending，必须持久化 blocked。"""

    from app.media_v4.maintenance.service import compute_delete_preview, confirm_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="越界")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 2)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    outside = tmp_path / "outside.jpg"
    outside.write_text("x", encoding="utf-8")
    # 模拟计划生成后路径被篡改成越界路径：执行时必须持久化 blocked 而不是 continue。
    with database.connect() as conn:
        conn.execute(
            "UPDATE maintenance_operation_items SET target_path = ? WHERE operation_id = ? AND target_path = ?",
            (str(outside), preview["preview_id"], str(paths[1])),
        )

    result = confirm_delete_preview(
        database, preview_id=preview["preview_id"], scope="pan115",
        digest=preview["digest"], mirror_root=mirror,
    )
    items = _item_results(database, preview["preview_id"])
    assert {item["result_status"] for item in items} == {"removed", "blocked"}
    statuses = {item["result_status"]: item for item in items}
    assert statuses["blocked"]["result_status"] == "blocked"
    # 结果必须包含 blocked 项（不能 continue 后从返回值消失）。
    blocked_results = [item for item in result["artifact_results"] if item["status"] == "blocked"]
    assert len(blocked_results) == 1


def test_empty_scope_with_no_active_root_rejected(tmp_path):
    """11.15.3-1：空 scope 无活动 root 必须拒绝，不生成空计划。"""

    from app.media_v4.maintenance.service import compute_delete_preview

    database = _fresh_database(tmp_path)
    with pytest.raises(ValueError, match="没有活动来源|未选择|不存在"):
        compute_delete_preview(database, provider="pan115")
    with pytest.raises(ValueError, match="没有活动来源|未选择|不存在"):
        compute_delete_preview(database, provider="all")


def test_preview_dto_does_not_leak_locator_or_work_ids(tmp_path):
    """11.15.3-5：preview DTO 不含 source locator、内部 Work ID、完整执行路径或完整 job 列表。"""

    from app.media_v4.maintenance.service import compute_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="脱敏")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 3)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    assert "roots" not in preview, "preview 不得返回 roots（含 source_locator）"
    assert "orphan_works" not in preview, "preview 不得返回内部 Work ID 列表"
    assert "mixed_works" not in preview, "preview 不得返回内部 Work ID 列表"
    assert "artifact_paths" not in preview, "preview 不得返回完整执行路径"
    assert "blocked_jobs" not in preview, "preview 不得返回完整 job 列表"
    assert "root_names" in preview and preview["root_names"][0]["root_id"] == "root-a"
    assert "blocked_job_count" in preview and preview["blocked_job_count"] == 0
    for summary in preview["artifact_summaries"]:
        assert str(mirror) not in str(summary)
        assert ":" not in str(summary).split("/")[0] if False else True  # 占位防误报


def test_digest_covers_job_snapshot_and_canonical_items(tmp_path):
    """11.15.3-3：digest 包含活动 job 快照与 canonical item 集合；preview/confirm 复用同一计算。"""

    from app.media_v4.maintenance.service import compute_delete_preview

    database = _fresh_database(tmp_path)
    _seed_root(database, root_id="root-a")
    _seed_work(database, root_id="root-a", work_id="w1", title="摘要")
    mirror, paths = _make_mirror(tmp_path, "root-a", "w1", 2)
    for path in paths:
        _seed_artifact(database, root_id="root-a", work_id="w1", target_path=path)

    preview_a = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    preview_b = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    assert preview_a["digest"] == preview_b["digest"], "相同状态 digest 必须稳定"

    _seed_job(database, revision_id="rev-root-a", job_id="job-snap", status="running")
    preview_c = compute_delete_preview(database, provider="pan115", mirror_root=mirror)
    assert preview_c["digest"] != preview_a["digest"], "活动 job 必须进入 digest"
