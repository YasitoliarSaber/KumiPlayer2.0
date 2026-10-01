"""P-005：来源卡只读 read model。

聚合活动来源根的权威事实：首次/最近确认时间、作品规模与
P-003 作品级进度。所有 membership 只来自最新活动 confirmed revision 的
revision_bindings，不按标题/路径猜测；退役来源统一过滤。
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from sqlite3 import Row

from app.media_v4.domain.identity import NON_IMPORTABLE_CONTENT_CLASSES
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService, _lookup_work_by_key
from app.media_v4.sources.scan_state import scan_is_stale


def list_source_cards(database: V4Database) -> list[dict]:
    """读取来源根级卡片，并把扫描生命周期与已确认导入合并成一个入口。

    来源卡不能以 confirmed revision 为存在条件：扫描开始时 revision 尚未
    创建，但用户必须能够离开导入页后从卡片重新进入任务。这里仍以
    source_roots 为唯一来源身份，分别选择最新 confirmed revision、最新
    draft revision 和最新 SourceScan，避免把扫描中的临时事实伪装成媒体库。
    """

    with database.connect() as conn:
        roots = conn.execute(
            """
            SELECT * FROM source_roots
            WHERE enabled = 1
              AND (
                  retired_at = ''
                  OR EXISTS (
                      SELECT 1
                      FROM import_revisions draft_revision
                      WHERE draft_revision.root_id = source_roots.root_id
                        AND draft_revision.status = 'draft'
                        AND julianday(draft_revision.created_at) > julianday(source_roots.retired_at)
                  )
              )
            ORDER BY updated_at DESC, root_id
            """
        ).fetchall()
        retired_cleanup_rows = conn.execute(
            """
            SELECT * FROM (
                SELECT sr.*, ir.revision_id AS cleanup_revision_id,
                       j.status AS cleanup_status,
                       ROW_NUMBER() OVER (
                           PARTITION BY sr.root_id ORDER BY j.created_at DESC, j.job_id DESC
                       ) AS cleanup_rank
                FROM source_roots sr
                JOIN import_revisions ir ON ir.root_id = sr.root_id
                JOIN jobs j ON j.revision_id = ir.revision_id
                WHERE sr.retired_at != '' AND j.job_type = 'delete_source_library'
            ) WHERE cleanup_rank = 1 AND cleanup_status IN ('queued', 'running', 'failed', 'cancelled')
            """
        ).fetchall()
        revisions = conn.execute(
            """
            SELECT ir.* FROM import_revisions ir
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE ir.status IN ('confirmed', 'draft')
              AND (sr.retired_at = '' OR (
                  ir.status = 'draft' AND julianday(ir.created_at) > julianday(sr.retired_at)
              ))
            ORDER BY ir.created_at DESC, ir.revision_id DESC
            """
        ).fetchall()
        # 每个 root 只取"最新一次扫描"：source_scans 每次扫描 +1 行且从不清理，
        # 全表扫描 + Python 端分组会随扫描次数线性变差；这里只针对**本次要出卡的
        # 活跃 root** 过滤（注意要按 roots 而不是 revisions：有扫描但还没 revision
        # 的来源同样要出卡，否则"正在扫描"的来源会丢失扫描状态），并用
        # UNIQUE(root_id, generation) 索引配合窗口函数直接选出最新一行。
        scan_root_ids = sorted({str(row["root_id"]) for row in roots})
        if scan_root_ids:
            scan_placeholders = ",".join("?" for _ in scan_root_ids)
            scans = conn.execute(
                f"""
                SELECT * FROM (
                    SELECT ss.*, ssr.request_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY ss.root_id
                               ORDER BY ss.generation DESC, ss.started_at DESC, ss.scan_id DESC
                           ) AS scan_rank
                    FROM source_scans ss
                    LEFT JOIN source_scan_requests ssr ON ssr.scan_id = ss.scan_id
                    WHERE ss.root_id IN ({scan_placeholders})
                ) WHERE scan_rank = 1
                """,
                scan_root_ids,
            ).fetchall()
        else:
            scans = []

        revision_ids = [str(row["revision_id"]) for row in revisions]
        revision_evidence: dict[str, int] = defaultdict(int)
        bound_works = defaultdict(set)
        bound_assets = defaultdict(set)
        bound_episodes = defaultdict(set)
        bound_evidence_files = defaultdict(set)
        pending_relations: dict[str, int] = defaultdict(int)
        job_rows = []
        if revision_ids:
            placeholders = ",".join("?" for _ in revision_ids)
            revision_evidence.update({
                str(row["revision_id"]): int(row["total"] or 0)
                for row in conn.execute(
                    f"SELECT revision_id, COUNT(*) AS total FROM revision_evidence WHERE revision_id IN ({placeholders}) GROUP BY revision_id",
                    revision_ids,
                ).fetchall()
            })
            for row in conn.execute(
                f"SELECT revision_id, work_id, episode_id, asset_id, evidence_id FROM revision_bindings WHERE revision_id IN ({placeholders}) AND work_id != ''",
                revision_ids,
            ).fetchall():
                revision_key = str(row["revision_id"])
                bound_works[revision_key].add(str(row["work_id"]))
                if row["episode_id"] is not None:
                    bound_episodes[revision_key].add(str(row["episode_id"]))
                if row["asset_id"] is not None:
                    bound_assets[revision_key].add(str(row["asset_id"]))
                # 一个物理视频可以覆盖多集，也可以只有电影直属 Asset；按 evidence 槽
                # 去重才是「已入库的视频文件数」，不能用 asset_id 或 bindings 行数代替。
                if row["asset_id"] is not None:
                    bound_evidence_files[revision_key].add(str(row["evidence_id"]))
            for row in conn.execute(
                f"SELECT revision_id, evidence_id, message FROM revision_issues WHERE revision_id IN ({placeholders}) "
                "AND resolved = 0 AND code = 'unresolved_parent_relation'",
                revision_ids,
            ).fetchall():
                if _relation_is_pending(conn, row):
                    pending_relations[str(row["revision_id"])] += 1
            job_rows = conn.execute(
                f"""
                SELECT revision_id, job_id, work_id, job_type, status, cancel_requested, heartbeat_at, created_at, finished_at
                FROM jobs WHERE revision_id IN ({placeholders})
                ORDER BY revision_id, CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, job_id
                """,
                revision_ids,
            ).fetchall()
    revisions_by_root: dict[str, dict[str, Row]] = {}
    for row in revisions:
        root_id = str(row["root_id"])
        status = str(row["status"])
        by_status = revisions_by_root.setdefault(root_id, {})
        by_status.setdefault(status, row)
    scans_by_root: dict[str, Row] = {}
    for row in scans:
        scans_by_root.setdefault(str(row["root_id"]), row)

    summaries: dict[str, dict] = defaultdict(lambda: {
        "total": 0, "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
    })
    jobs_by_revision: dict[str, list] = defaultdict(list)
    for row in job_rows:
        revision_id = str(row["revision_id"])
        status = str(row["status"])
        if status not in summaries[revision_id]:
            continue
        summaries[revision_id]["total"] += 1
        summaries[revision_id][status] += 1
        jobs_by_revision[revision_id].append(row)

    cards = []
    progress_service = V4RevisionService(database)
    for root in roots:
        root_id = str(root["root_id"])
        by_status = revisions_by_root.get(root_id, {})
        confirmed = by_status.get("confirmed")
        draft_candidate = by_status.get("draft")
        scan = scans_by_root.get(root_id)
        # 草稿是某一次扫描的不可变快照。来源卡只允许把它和同一 scan
        # 代次配对；新的扫描一开始，旧草稿必须退出当前入口，防止用户在
        # “检查识别”页面误确认过期的目录结果。
        draft = draft_candidate if (
            draft_candidate is not None
            and (scan is None or str(draft_candidate["scan_id"]) == str(scan["scan_id"]))
        ) else None
        # 新扫描完成后，draft 是用户当前需要继续复核的工作集；即使该来源
        # 已经有旧的 confirmed revision，也不能把来源卡重新指向旧 revision。
        # draft 被确认后会消失，届时自然回落到最新 confirmed revision。
        active_revision = draft or confirmed
        revision_id = str(active_revision["revision_id"]) if active_revision is not None else ""
        scan_status = str(scan["status"]) if scan is not None else ""
        # 失联判定：running/cancelling 但心跳已过期 = 执行进程已消失。
        # 旧版本可能没有 heartbeat_at，此时用同一行的 started_at 作为
        # 保守回退：刚启动的任务仍显示活动，真正陈旧的旧行仍可被回收。
        # 僵尸行不得被渲染成仍在处理，也不得永久阻止卡片操作。
        scan_heartbeat = str(scan["heartbeat_at"] or scan["started_at"] or "") if scan is not None else ""
        scan_interrupted = (
            scan is not None
            and scan_status in {"running", "cancelling"}
            and scan_is_stale(scan_heartbeat)
        )
        scan_active = scan_status in {"running", "queued", "cancelling"} and not scan_interrupted
        scan_failed = scan_status in {"failed", "cancelled"}
        # 达到请求预算的扫描：进度保留、可继续，界面上按"需要处理"呈现，
        # 但不能算完整成功。
        scan_paused = scan_status == "paused"
        has_confirmed = confirmed is not None
        job_summary = dict(summaries.get(revision_id, {
            "total": 0, "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
        }))
        active_job = next(
            (row for row in jobs_by_revision.get(revision_id, [])
             if str(row["status"]) in {"running", "queued"}
             and str(row["job_type"]) != "cleanup_superseded_artifacts"),
            None,
        )
        deletion_jobs = [row for row in jobs_by_revision.get(revision_id, []) if str(row["job_type"]) == "delete_source_library"]
        last_deletion = max(deletion_jobs, key=lambda row: (str(row["created_at"]), str(row["job_id"]))) if deletion_jobs else None
        deletion_retry_required = last_deletion is not None and str(last_deletion["status"]) in {"failed", "cancelled"}

        progress = _safe_progress(progress_service, revision_id) if draft is None and confirmed is not None else {}
        draft_graph = None
        if draft is not None:
            try:
                draft_graph = progress_service.load_draft_graph(revision_id, refresh_snapshot=False)
            except (KeyError, RuntimeError):
                # 并发确认会使已读取的 draft 退出审核态；下一次轮询获取新状态。
                # 仍是 draft 时代表真正的读取/评估故障，不能静默隐藏来源卡。
                try:
                    still_draft = progress_service.get_status(revision_id) == "draft"
                except KeyError:
                    still_draft = False
                if still_draft:
                    raise
                continue
        if scan_active:
            overall_status = "queued" if scan_status == "queued" else "running"
            phase = "scan"
        elif scan_interrupted:
            overall_status = "needs_attention"
            phase = "scan"
        elif scan_failed:
            # 用户主动取消不是故障；来源卡要保留恢复入口，但不能把它渲染成
            # 含糊的“需要处理”，否则用户无法判断是否需要重新扫描。
            overall_status = "cancelled" if scan_status == "cancelled" else "needs_attention"
            phase = "scan"
        elif draft is not None:
            overall_status = "needs_attention"
            phase = "review"
        else:
            overall_status = progress.get("overall_status") or "completed"
            phase = "execute" if has_confirmed else "scan"

        if scan is not None:
            scan_progress = _scan_progress(scan)
            scan_payload = {
                "scan_id": str(scan["scan_id"]),
                "status": scan_status,
                "stage": str(scan["stage"] or "queued"),
                "stage_label": _scan_stage_label(scan),
                "processed_count": int(scan["processed_count"] or 0),
                "total_count": int(scan["total_count"] or 0),
                "progress": scan_progress,
                "heartbeat_at": str(scan["heartbeat_at"] or ""),
            }
        else:
            scan_progress = None
            scan_payload = None

        revision_evidence_count = revision_evidence.get(revision_id, 0)
        evidence_count = revision_evidence_count
        if evidence_count == 0 and scan is not None:
            # 这里必须用**权威计数**（库内证据行数）：扫描行的 processed_count/total_count
            # 是进度值，读取期由内存计数推进、可能领先于真实落库行数，拿它当证据数会让
            # 来源卡显示比实际更多的文件（既有测试 test_zombie_scan_is_not_projected_as_active
            # 等正是在断言这一点）。每张卡片一次 COUNT 走索引，代价远小于报错数。
            evidence_count = _scan_evidence_count(database, str(scan["scan_id"]))

        # 阶段化计量（STEP-001）：同一张卡上的作品数、视频文件数、剧集数各自只属于
        # 一个生命周期。草稿的作品预览与 confirmed 的绑定集合不能拼成一组无阶段说明的
        # “规模”，也不能用「作品数 - 问题数」推算“已刮削成功”。
        if draft_graph is not None:
            counts_scope = "draft"
        elif draft is None and confirmed is not None:
            counts_scope = "confirmed"
        else:
            counts_scope = "scan"
        scan_counts = _scan_scope_counts(database, str(scan["scan_id"])) if scan is not None else None
        observed_entry_count = scan_counts["observed_entry_count"] if scan_counts else None
        observed_video_count = scan_counts["observed_video_count"] if scan_counts else None
        excluded_video_count = scan_counts["excluded_video_count"] if scan_counts else None
        if counts_scope == "draft" and draft_graph is not None:
            admitted_ids: set[str] = set()
            for episode in draft_graph.episodes:
                admitted_ids.update(str(item) for item in episode.asset_evidence_ids)
            for asset in draft_graph.work_assets:
                admitted_ids.update(str(item) for item in asset.asset_evidence_ids)
            work_count_value: int | None = len(draft_graph.works)
            admitted_video_file_count: int | None = len(admitted_ids)
            asset_count = len(admitted_ids)
            episode_count = len(draft_graph.episodes)
        elif counts_scope == "confirmed":
            work_count_value = len(bound_works.get(revision_id, set()))
            admitted_video_file_count = len(bound_evidence_files.get(revision_id, set()))
            asset_count = len(bound_assets.get(revision_id, set()))
            episode_count = len(bound_episodes.get(revision_id, set()))
        else:
            # 扫描阶段还没有作品归属，works/assets 是未知而不是 0；
            # 把“还没算”写成 0 会让用户以为来源里没有内容。
            work_count_value = None
            admitted_video_file_count = None
            asset_count = None
            episode_count = None
        work_count = work_count_value
        if counts_scope == "confirmed":
            metadata_ready_work_count = _metadata_ready_count(progress)
            metadata_ready_current_count = _metadata_ready_count(progress, source="current")
            metadata_ready_retained_count = _metadata_ready_count(progress, source="retained")
        else:
            metadata_ready_work_count = None
            metadata_ready_current_count = None
            metadata_ready_retained_count = None
        if scan_active:
            message = _scan_message(scan, scan_progress)
            card_progress = {
                "state": overall_status,
                "stage": "scan",
                "current_work_id": "",
                "current_work_title": "",
                "completed_work_count": 0,
                "total_work_count": 0,
                "percent": scan_progress,
                "message": message,
            }
        elif scan_status == "cancelled":
            card_progress = {
                "state": "cancelled",
                "stage": "scan",
                "current_work_id": "",
                "current_work_title": "",
                "completed_work_count": 0,
                "total_work_count": 0,
                "percent": None,
                "message": "扫描已取消",
            }
        elif scan_interrupted:
            card_progress = {
                "state": "needs_attention",
                "stage": "scan",
                "current_work_id": "",
                "current_work_title": "",
                "completed_work_count": 0,
                "total_work_count": 0,
                "percent": None,
                "message": "上次扫描意外中断，请重新扫描",
            }
        elif draft is None and confirmed is not None:
            card_progress = {
                "state": progress.get("overall_status") or "completed",
                "stage": _progress_stage(progress),
                "current_work_id": _current_work_id(progress),
                "current_work_title": _current_work_title(progress),
                "completed_work_count": _completed_count(progress),
                "total_work_count": len(progress.get("work_units") or []),
                "percent": _progress_percent(progress),
                "message": _progress_message(progress),
            }
        else:
            card_progress = {
                "state": overall_status,
                "stage": "review",
                "current_work_id": "",
                "current_work_title": "",
                "completed_work_count": 0,
                "total_work_count": work_count or 0,
                "percent": None,
                "message": "识别结果待确认",
            }
        last_error = _friendly_error(
            str(scan["error"] or "") if (scan_failed or scan_paused) and scan is not None else _first_error(progress)
        )
        if deletion_retry_required:
            overall_status = "needs_attention"
            phase = "execute"
            last_error = "上次清理未完成，请重新核对范围并重试。"
            card_progress.update(state=overall_status, message="清理未完成", percent=None)
        started_at = str(scan["started_at"] or "") if scan is not None else ""
        confirmed_at = str(confirmed["confirmed_at"] or "") if confirmed is not None else ""
        draft_created_at = str(draft["created_at"] or "") if draft is not None else ""
        added_at = confirmed_at or draft_created_at or started_at or str(root["created_at"] or "")
        updated_at = (
            str(scan["heartbeat_at"] or scan["finished_at"] or started_at)
            if scan is not None
            else confirmed_at or draft_created_at or str(root["updated_at"] or "")
        )
        resume_by_scan = scan_active or scan_failed or scan_interrupted or scan_paused
        can_resume = resume_by_scan or _can_resume(progress) or deletion_retry_required or draft is not None
        active_task = None if scan_interrupted else _active_task(scan, active_job, revision_id, card_progress)
        if phase == "scan":
            attention_count = int(scan_status == "failed" or scan_interrupted or scan_paused)
        elif draft_graph is not None:
            attention_count = len(draft_graph.issues)
        else:
            attention_count = _execution_attention_count(progress)
        if overall_status == "completed" and attention_count:
            overall_status = "needs_attention"
            card_progress.update(state=overall_status, message="有任务需要处理")
        import_jobs = [job for job in jobs_by_revision.get(revision_id, [])
                       if job["job_type"] in {"materialize_mirror", "scrape_work", "recover_work_aliases", "refresh_projection"}]
        import_completed_at = max((str(job["finished_at"] or "") for job in import_jobs), default="") if (
            import_jobs and all(job["status"] == "succeeded" and job["finished_at"] for job in import_jobs)
        ) else ""
        try:
            scan_request = json.loads(scan["request_json"] or "{}") if scan is not None else {}
        except (TypeError, ValueError):
            scan_request = {}
        tree_file_path = str(scan_request.get("tree_file_path") or "") if isinstance(scan_request, dict) else ""
        cards.append({
            "root_id": root_id,
            "provider": str(root["provider"] or ""),
            "ingest_method": str(root["ingest_method"] or ""),
            "source_mode": str(root["source_mode"] or ""),
            "last_scan_mode": str(root["last_scan_mode"] or ""),
            "has_confirmed_baseline": has_confirmed,
            "source_locator": str(root["source_locator"] or ""),
            "display_path": _display_path(str(root["source_locator"] or ""), str(root["provider"] or "")),
            "tree_file_path": tree_file_path,
            "import_completed_at": import_completed_at,
            "playback_locator": str(root["playback_locator"] or ""),
            "route_id": str(root["route_id"] or ""),
            "display_name": _display_name(
                str(root["display_name"] or ""),
                str(root["source_locator"] or ""),
                str(root["provider"] or ""),
            ),
            "enabled": int(root["enabled"]),
            "added_at": added_at,
            "updated_at": updated_at,
            "revision_id": revision_id,
            "latest_revision_id": revision_id,
            "revision_state": str(active_revision["status"]) if active_revision is not None else (scan_status or ""),
            "phase": phase,
            "overall_status": overall_status,
            "work_count": work_count,
            "asset_count": asset_count,
            "evidence_count": evidence_count,
            "attention_count": attention_count,
            # 阶段化计量（STEP-001）：每个数字都带 scope 和唯一单位；
            # 无法求解的项返回 null（未知），不把计算失败转 0。
            "counts_scope": counts_scope,
            "observed_entry_count": observed_entry_count,
            "observed_video_count": observed_video_count,
            "admitted_video_file_count": admitted_video_file_count,
            "episode_count": episode_count,
            "excluded_video_count": excluded_video_count,
            "metadata_ready_work_count": metadata_ready_work_count,
            "metadata_ready_current_count": metadata_ready_current_count,
            "metadata_ready_retained_count": metadata_ready_retained_count,
            "relation_pending_count": pending_relations.get(revision_id, 0) if phase == "execute" else 0,
            "last_error": last_error,
            "deletion_retry_required": deletion_retry_required,
            "scan": scan_payload,
            "progress": card_progress,
            "active_task": active_task,
            "available_actions": ["inspect", "resume"] if can_resume else ["inspect", "update"],
            "can_resume": can_resume,
            "job_summary": job_summary,
        })
    # 退役来源不再贡献媒体墙作品，但最后一次文件回收失败时仍须留下一个明确的
    # 维护入口；否则用户看不到残留产物，也无从重试。此卡不报告旧作品数。
    active_root_ids = {str(row["root_id"]) for row in roots}
    for root in retired_cleanup_rows:
        root_id = str(root["root_id"])
        if root_id in active_root_ids:
            continue
        revision_id = str(root["cleanup_revision_id"])
        cleanup_status = str(root["cleanup_status"])
        cleanup_active = cleanup_status in {"queued", "running"}
        cards.append({
            "root_id": root_id,
            "provider": str(root["provider"] or ""),
            "ingest_method": str(root["ingest_method"] or ""),
            "source_mode": str(root["source_mode"] or ""),
            "last_scan_mode": str(root["last_scan_mode"] or ""),
            "has_confirmed_baseline": False,
            "source_locator": str(root["source_locator"] or ""),
            "display_path": _display_path(str(root["source_locator"] or ""), str(root["provider"] or "")),
            "playback_locator": str(root["playback_locator"] or ""),
            "route_id": str(root["route_id"] or ""),
            "display_name": _display_name(str(root["display_name"] or ""), str(root["source_locator"] or ""), str(root["provider"] or "")),
            "enabled": 0,
            "added_at": str(root["created_at"] or ""),
            "updated_at": str(root["updated_at"] or ""),
            "revision_id": revision_id,
            "latest_revision_id": revision_id,
            "revision_state": "retired",
            "phase": "cleanup",
            "overall_status": "running" if cleanup_active else "needs_attention",
            "work_count": None,
            "asset_count": None,
            "evidence_count": 0,
            "attention_count": 0,
            "counts_scope": "cleanup",
            "observed_entry_count": None,
            "observed_video_count": None,
            "admitted_video_file_count": None,
            "episode_count": None,
            "excluded_video_count": None,
            "metadata_ready_work_count": None,
            "metadata_ready_current_count": None,
            "metadata_ready_retained_count": None,
            "relation_pending_count": 0,
            "last_error": "" if cleanup_active else "生成文件清理未完成，请重新核对范围并重试。",
            "deletion_retry_required": not cleanup_active,
            "scan": None,
            "progress": {"state": "running" if cleanup_active else "needs_attention", "stage": "cleanup", "current_work_id": "", "current_work_title": "", "completed_work_count": 0, "total_work_count": 0, "percent": None, "message": "正在清理生成文件" if cleanup_active else "清理未完成"},
            "active_task": {"kind": "execution", "revision_id": revision_id, "status": cleanup_status, "stage": "delete_source_library", "label": "正在清理生成文件", "percent": None, "can_cancel": False, "cancel_requested": False} if cleanup_active else None,
            "available_actions": [] if cleanup_active else ["retry_deletion"],
            "can_resume": False,
            "job_summary": {"total": 1, "queued": int(cleanup_status == "queued"), "running": int(cleanup_status == "running"), "succeeded": 0, "failed": int(cleanup_status == "failed"), "cancelled": int(cleanup_status == "cancelled")},
        })
    return cards


def _execution_attention_count(progress: dict) -> int:
    """只统计执行详情可见的作品故障与投影故障；历史清理任务不算待处理。"""
    affected = {
        str(unit["work_id"]) for unit in progress.get("work_units") or []
        if unit.get("overall_status") in {"failed", "needs_attention"}
    }
    projection = (progress.get("stage_summary") or {}).get("projection") or {}
    return len(affected) + int(int(projection.get("failed") or 0) > 0)


def _relation_parent_is_self(conn, parent_key: str, child_id: str) -> bool:
    """父键的标题部分是否就是子作品自己的标题。

    历史记录里存在 ``series:<作品自身标题>:tv`` 这类伪父系列（由已被修掉的
    "先强制 movie→tv、再判自名"顺序产生）。它的父键永远不会解析到任何 Work，
    于是永远 pending。这里只读地把它判定为"不是当前缺项"，**不修改真实数据**。
    """

    row = conn.execute(
        "SELECT identity_key FROM works WHERE work_id = ?", (child_id,)
    ).fetchone()
    if row is None:
        return False
    identity_key = str(row["identity_key"] or "")
    if not identity_key.startswith("title:"):
        return False
    # 键形如 series:<归一化标题>:<media_type>；标题本身可能含冒号，从右侧拆类型。
    parts = str(parent_key or "").split(":")
    if len(parts) < 3 or parts[0] != "series":
        return False
    parent_title = ":".join(parts[1:-1]).strip()
    if not parent_title:
        return False
    return identity_key.startswith(f"title:{parent_title}:")


def _relation_is_pending(conn, issue) -> bool:
    """只读重估历史关系提示；保留原记录，不把伪自关联当成当前缺项。"""
    match = re.fullmatch(r"父系列 (series:.+) 尚未导入，无法建立作品关系", issue["message"])
    if match is None:
        return True
    parent_key = match.group(1)
    parent_id = _lookup_work_by_key(conn, parent_key)
    child_id = str(issue["evidence_id"])
    if parent_id == child_id:
        return False
    if _relation_parent_is_self(conn, parent_key, child_id):
        return False
    return not (parent_id and conn.execute(
        "SELECT 1 FROM work_relations WHERE parent_work_id=? AND child_work_id=? LIMIT 1",
        (parent_id, child_id),
    ).fetchone())


def hide_source_card(database: V4Database, root_id: str) -> dict[str, object]:
    """从来源卡列表移除一个已停止的来源，而不退役媒体库。

    ``retired_at`` 属于“按来源清理媒体库”的语义，会让作品、播放和追更
    查询退出活动库；卡片删除只复用 root 的 ``enabled`` 可见性位。再次对
    同一来源开始扫描时，入口会显式恢复该位。
    """

    normalized_root_id = str(root_id or "").strip()
    if not normalized_root_id:
        raise KeyError("来源卡不存在或已移除")
    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        # DELETE 是卡片入口的幂等隐藏操作。列表轮询与用户点击之间可能
        # 已经把卡片隐藏或退役，但只要来源根仍在，就应完成同一个目标状态，
        # 而不是把一个可恢复的操作误报成 404。
        root = _source_root_for_management(conn, normalized_root_id)
        if root is None:
            raise KeyError("来源卡不存在或已移除")
        active_scan_rows = conn.execute(
            "SELECT status, heartbeat_at, started_at FROM source_scans WHERE root_id = ?",
            (normalized_root_id,),
        ).fetchall()
        # queued 一定算活动（下一个 worker 会领取）；running/cancelling 只有
        # 心跳仍新鲜才算活动，失联的僵尸行不得永久阻止卡片删除。
        has_active_scan = any(
            str(row["status"]) == "queued"
            or (
                str(row["status"]) in {"running", "cancelling"}
                and not scan_is_stale(str(row["heartbeat_at"] or row["started_at"] or ""))
            )
            for row in active_scan_rows
        )
        active_job = conn.execute(
            """
            SELECT 1
            FROM jobs job
            JOIN import_revisions revision ON revision.revision_id = job.revision_id
            WHERE revision.root_id = ? AND job.status IN ('queued', 'running')
            LIMIT 1
            """,
            (normalized_root_id,),
        ).fetchone()
        if has_active_scan or active_job is not None:
            raise ValueError("该来源仍有后台任务，请先终止任务并等待其结束")
        conn.execute(
            "UPDATE source_roots SET enabled = 0, updated_at = ? WHERE root_id = ?",
            (now, normalized_root_id),
        )
    return {"root_id": normalized_root_id, "hidden": True}


def rename_source_card(database: V4Database, root_id: str, display_name: str) -> dict[str, str]:
    """更新可见来源卡的用户名称，不修改来源路径或媒体事实。"""

    normalized_root_id = str(root_id or "").strip()
    normalized_name = str(display_name or "").strip()
    if not normalized_root_id:
        raise KeyError("来源卡不存在或已移除")
    if not normalized_name:
        raise ValueError("请填写来源名称")

    now = datetime.now(UTC).isoformat()
    with database.connect() as conn:
        if _source_root_for_management(conn, normalized_root_id) is None:
            raise KeyError("来源卡不存在或已移除")
        conn.execute(
            "UPDATE source_roots SET display_name = ?, updated_at = ? WHERE root_id = ?",
            (normalized_name, now, normalized_root_id),
        )
    return {"root_id": normalized_root_id, "display_name": normalized_name}


def _source_root_for_management(conn, root_id: str):
    """返回仍在数据库中的来源根，供卡片操作处理轮询竞态。

    “列表中可见”是投影条件，不是删除请求的存在条件。来源卡在请求发出
    后可能被另一轮刷新隐藏，管理操作仍应以根记录的最终状态为准；真正不
    存在的 root_id 才返回 404。
    """

    return conn.execute(
        "SELECT enabled, retired_at FROM source_roots WHERE root_id = ?",
        (root_id,),
    ).fetchone()


def _active_task(scan, job, revision_id: str, progress: dict) -> dict | None:
    """来源卡只投影一条当前任务，不暴露内部 job_id 或 outbox 细节。"""

    if scan is not None and str(scan["status"]) in {"queued", "running", "cancelling"}:
        status = "cancelling" if bool(scan["cancel_requested"]) or str(scan["status"]) == "cancelling" else str(scan["status"])
        return {
            "kind": "scan",
            "revision_id": "",
            "status": status,
            "stage": str(scan["stage"] or "queued"),
            "label": "正在终止扫描" if status == "cancelling" else _scan_stage_label(scan),
            "percent": _scan_progress(scan),
            "can_cancel": status in {"queued", "running"},
            "cancel_requested": status == "cancelling",
        }
    if job is None:
        return None
    job_type = str(job["job_type"])
    status = "cancelling" if bool(job["cancel_requested"]) else str(job["status"])
    labels = {
        "materialize_mirror": "正在准备播放文件",
        "scrape_work": "正在获取媒体信息",
        "recover_work_aliases": "正在核对作品名称",
        "refresh_projection": "正在更新媒体库",
        "cleanup_superseded_artifacts": "正在整理旧文件",
        "delete_source_library": "正在删除该来源的媒体库",
    }
    return {
        "kind": "execution",
        "revision_id": revision_id,
        "status": status,
        "stage": job_type,
        "label": "正在终止任务" if status == "cancelling" else labels.get(job_type, "正在处理媒体库任务"),
        # 删除作业不能使用上一次导入的百分比；退役后已进入不可中断的回收阶段。
        "percent": None if job_type == "delete_source_library" else progress.get("percent"),
        "can_cancel": status == "queued" if job_type == "delete_source_library" else status in {"queued", "running"},
        "cancel_requested": status == "cancelling",
    }


def _display_path(locator: str, provider: str) -> str:
    """来源卡使用紧凑、去重后的路径摘要；原始 locator 仍保留在 API 字段。"""

    raw = (locator or "").strip()
    if not raw:
        return ""
    segments = [segment for segment in raw.replace("\\", "/").split("/") if segment]
    if segments and segments[0].endswith(":"):
        segments.pop(0)
    provider_segments = {
        "local": {"本地", "local"},
        "pan115": {"115", "115网盘", "115 网盘"},
        "baidu": {"百度", "百度网盘"},
        "quark": {"夸克", "夸克网盘"},
    }.get(provider, set())
    while segments and segments[0].casefold() in {item.casefold() for item in provider_segments}:
        segments.pop(0)
    return " › ".join(segments)


def _display_name(name: str, locator: str, provider: str) -> str:
    """压缩来源标题，避免与卡片头部的提供商标识重复。"""

    explicit_name = (name or "").strip()
    if not explicit_name:
        compact_path = _display_path(locator, provider)
        if compact_path:
            return compact_path.rsplit(" › ", 1)[-1]
        return f"{provider} 媒体库"
    raw = explicit_name
    provider_labels = {
        "local": ("本地", "local"),
        "pan115": ("115 网盘", "115网盘", "115"),
        "baidu": ("百度网盘", "百度"),
        "quark": ("夸克网盘", "夸克"),
    }.get(provider, ())
    folded = raw.casefold()
    if folded in {label.casefold() for label in provider_labels}:
        compact_path = _display_path(locator, provider)
        return compact_path.rsplit(" › ", 1)[-1] if compact_path else raw
    for label in provider_labels:
        if not folded.startswith(label.casefold()):
            continue
        compact = raw[len(label):]
        while compact and compact[0] in " ·›:：/\\\\-":
            compact = compact[1:]
        compact = compact.strip()
        if compact:
            return compact
    return raw


def _safe_progress(progress_service: V4RevisionService, revision_id: str) -> dict:
    if not revision_id:
        return {}
    try:
        return progress_service.get_execution_progress(revision_id)
    except KeyError:
        return {}


def _scan_evidence_count(database: V4Database, scan_id: str) -> int:
    with database.connect() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM source_evidence WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()[0]
        )


def _scan_scope_counts(database: V4Database, scan_id: str) -> dict[str, int]:
    """扫描阶段的已观察量：总条目、视频条目、按 content_class 明确排除的视频。

    只读来源观察与已落库的 ParsedFacts；不把“已观察”当成“已完成”，也不
    把未解析的条目当成已排除。parsed_facts 尚未生成时排除量为 0。
    """

    with database.connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS observed_entry_count,
                SUM(CASE WHEN entry_kind = 'video' THEN 1 ELSE 0 END) AS observed_video_count
            FROM source_evidence WHERE scan_id = ?
            """,
            (scan_id,),
        ).fetchone()
        excluded_placeholders = ",".join("?" for _ in NON_IMPORTABLE_CONTENT_CLASSES)
        excluded = conn.execute(
            f"""
            SELECT COUNT(*) FROM source_evidence se
            JOIN parsed_facts pf ON pf.evidence_id = se.evidence_id
            WHERE se.scan_id = ? AND se.entry_kind = 'video'
              AND pf.content_class IN ({excluded_placeholders})
            """,
            (scan_id, *sorted(NON_IMPORTABLE_CONTENT_CLASSES)),
        ).fetchone()[0]
    return {
        "observed_entry_count": int(row["observed_entry_count"] or 0),
        "observed_video_count": int(row["observed_video_count"] or 0),
        "excluded_video_count": int(excluded or 0),
    }


def _metadata_ready_count(progress: dict, source: str | None = None) -> int | None:
    """当前 confirmed 作品集中在线资料已就绪的数量。

    只对当前 revision 的 work_units 求值：``metadata_state=ready`` 且
    （可选）指定 ``metadata_source``。``metadata_source=retained`` 表示沿用
    上一次成功资料快照，也是用户可见的“已就绪”，但必须能与本次刷新成功区分。
    缺失或不可读时返回 ``None``，由界面显示为暂不可用，不用差值推算。
    """

    units = progress.get("work_units") if isinstance(progress, dict) else None
    if not units:
        return None if not progress else 0
    count = 0
    for unit in units:
        if str(unit.get("metadata_state") or "") != "ready":
            continue
        if source is None:
            count += 1
            continue
        observed = str(unit.get("metadata_source") or "current")
        if observed == source:
            count += 1
    return count


def _scan_progress(scan) -> int | None:
    total = int(scan["total_count"] or 0)
    if total <= 0:
        return None
    processed = max(0, int(scan["processed_count"] or 0))
    return min(100, round(processed / total * 100))


def _scan_stage_label(scan) -> str:
    labels = {
        "queued": "准备读取媒体来源",
        "reading_source": "读取媒体来源",
        "recognizing": "离线识别与整理",
        "parsing": "解析媒体条目",
        "normalizing": "整理识别结果",
        "preparing_preview": "生成识别预览",
        "ready": "识别结果已就绪",
        "failed": "扫描失败",
        "cancelled": "扫描已取消",
    }
    return labels.get(str(scan["stage"] or "queued"), "正在处理")


def _scan_message(scan, percent: int | None) -> str:
    label = _scan_stage_label(scan)
    return f"{label} · {percent}%" if percent is not None else label


def _friendly_error(value: str) -> str:
    """来源卡只显示可行动的人话，不把 SQLite/内部字段泄漏到 UI。"""

    text = (value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if "unique constraint failed" in lowered or "integrityerror" in lowered:
        return "媒体身份与已有记录冲突，请检查识别结果后重试"
    if "no such table" in lowered or "no such column" in lowered:
        return "媒体库结构不完整，请重试；如果仍失败，请检查数据库状态"
    if "timed out" in lowered or "timeout" in lowered:
        return "来源响应超时，请检查网络或 OpenList 连接后重试"
    if "取消" in text or "cancel" in lowered:
        return "扫描已取消"
    return "任务未能完成，请查看执行结果后重试"


def _first_error(progress: dict) -> str:
    for unit in progress.get("work_units") or []:
        for slot in ("mirror", "metadata"):
            job = unit.get(slot) or {}
            if job.get("last_error"):
                return str(job["last_error"])
    return ""


def _progress_stage(progress: dict) -> str:
    stage = (progress.get("stage_summary") or {}).get("mirror") or {}
    if stage.get("status") == "running":
        return "mirror"
    metadata = (progress.get("stage_summary") or {}).get("metadata") or {}
    if metadata.get("status") == "running":
        return "metadata"
    projection = (progress.get("stage_summary") or {}).get("projection") or {}
    if projection.get("status") in {"queued", "running"}:
        return "projection"
    return "idle"


def _current_work_id(progress: dict) -> str:
    for unit in progress.get("work_units") or []:
        if unit.get("overall_status") in {"running_mirror", "running_metadata"}:
            return str(unit["work_id"])
    return ""


def _current_work_title(progress: dict) -> str:
    for unit in progress.get("work_units") or []:
        if unit.get("overall_status") in {"running_mirror", "running_metadata"}:
            return str(unit.get("title") or "")
    return ""


def _completed_count(progress: dict) -> int:
    return sum(1 for unit in progress.get("work_units") or [] if unit.get("overall_status") == "completed")


def _progress_percent(progress: dict) -> int | None:
    units = progress.get("work_units") or []
    if not units:
        return None
    return round(_completed_count(progress) / len(units) * 100)


def _progress_message(progress: dict) -> str:
    overall = progress.get("overall_status")
    if overall == "running":
        stage = _progress_stage(progress)
        label = {"mirror": "正在生成镜像", "metadata": "正在获取媒体信息", "projection": "正在更新媒体库"}.get(stage, "处理中")
        return label
    if overall == "needs_attention":
        return "有任务需要处理"
    if overall == "queued":
        return "正在准备任务"
    if overall == "cancelled":
        return "任务已终止"
    return "上次导入已处理完毕"


def _available_actions(progress: dict) -> list[str]:
    actions = ["inspect"]
    if progress.get("overall_status") in {"running", "queued"}:
        actions.append("resume")
    else:
        actions.append("update")
    return actions


def _can_resume(progress: dict) -> bool:
    overall = progress.get("overall_status")
    return overall in {"running", "queued", "needs_attention"}
