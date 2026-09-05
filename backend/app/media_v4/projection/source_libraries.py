"""P-005：来源卡只读 read model。

聚合活动来源根的权威事实：首次/最近确认时间、作品规模与
P-003 作品级进度。所有 membership 只来自最新活动 confirmed revision 的
revision_bindings，不按标题/路径猜测；退役来源统一过滤。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService
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
            "SELECT * FROM source_roots WHERE retired_at = '' AND enabled = 1 ORDER BY updated_at DESC, root_id"
        ).fetchall()
        revisions = conn.execute(
            """
            SELECT * FROM import_revisions
            WHERE status IN ('confirmed', 'draft')
            ORDER BY created_at DESC, revision_id DESC
            """
        ).fetchall()
        scans = conn.execute(
            """
            SELECT * FROM source_scans
            ORDER BY generation DESC, started_at DESC, scan_id DESC
            """
        ).fetchall()

        revision_ids = [str(row["revision_id"]) for row in revisions]
        revision_evidence = defaultdict(int)
        candidate_works = defaultdict(set)
        bound_works = defaultdict(set)
        bound_assets = defaultdict(set)
        unresolved_issues = defaultdict(int)
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
                f"SELECT revision_id, work_id FROM revision_work_candidates WHERE revision_id IN ({placeholders}) AND work_id != ''",
                revision_ids,
            ).fetchall():
                candidate_works[str(row["revision_id"])].add(str(row["work_id"]))
            for row in conn.execute(
                f"SELECT revision_id, work_id, asset_id FROM revision_bindings WHERE revision_id IN ({placeholders}) AND work_id != ''",
                revision_ids,
            ).fetchall():
                bound_works[str(row["revision_id"])].add(str(row["work_id"]))
                if row["asset_id"] is not None:
                    bound_assets[str(row["revision_id"])].add(str(row["asset_id"]))
            for row in conn.execute(
                f"SELECT revision_id, COUNT(*) AS total FROM revision_issues WHERE revision_id IN ({placeholders}) AND resolved = 0 GROUP BY revision_id",
                revision_ids,
            ).fetchall():
                unresolved_issues[str(row["revision_id"])] = int(row["total"] or 0)
            job_rows = conn.execute(
                f"""
                SELECT revision_id, job_id, job_type, status, cancel_requested, heartbeat_at
                FROM jobs WHERE revision_id IN ({placeholders})
                ORDER BY revision_id, CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, job_id
                """,
                revision_ids,
            ).fetchall()
    revisions_by_root: dict[str, dict[str, object]] = {}
    for row in revisions:
        root_id = str(row["root_id"])
        status = str(row["status"])
        by_status = revisions_by_root.setdefault(root_id, {})
        by_status.setdefault(status, row)
    scans_by_root: dict[str, object] = {}
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
        # 僵尸行不得被渲染成仍在处理，也不得永久阻止卡片操作。
        scan_interrupted = (
            scan is not None
            and scan_status in {"running", "cancelling"}
            and scan_is_stale(str(scan["heartbeat_at"] or ""))
        )
        scan_active = scan_status in {"running", "queued", "cancelling"} and not scan_interrupted
        scan_failed = scan_status in {"failed", "cancelled"}
        has_confirmed = confirmed is not None
        job_summary = dict(summaries.get(revision_id, {
            "total": 0, "queued": 0, "running": 0, "succeeded": 0, "failed": 0, "cancelled": 0,
        }))
        active_job = next(
            (row for row in jobs_by_revision.get(revision_id, []) if str(row["status"]) in {"running", "queued"}),
            None,
        )

        progress = _safe_progress(progress_service, revision_id) if confirmed is not None else {}
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
            evidence_count = _scan_evidence_count(database, str(scan["scan_id"]))
        work_count = len(bound_works.get(revision_id, set())) if confirmed is not None else len(candidate_works.get(revision_id, set()))
        asset_count = len(bound_assets.get(revision_id, set()))
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
        elif confirmed is not None:
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
                "total_work_count": work_count,
                "percent": None,
                "message": "识别结果待确认",
            }
        last_error = _friendly_error(
            str(scan["error"] or "") if scan_failed and scan is not None else _first_error(progress)
        )
        started_at = str(scan["started_at"] or "") if scan is not None else ""
        confirmed_at = str(confirmed["confirmed_at"] or "") if confirmed is not None else ""
        draft_created_at = str(draft["created_at"] or "") if draft is not None else ""
        added_at = confirmed_at or draft_created_at or started_at or str(root["created_at"] or "")
        updated_at = (
            str(scan["heartbeat_at"] or scan["finished_at"] or started_at)
            if scan is not None
            else confirmed_at or draft_created_at or str(root["updated_at"] or "")
        )
        resume_by_scan = scan_active or scan_failed or scan_interrupted
        can_resume = resume_by_scan or _can_resume(progress) or (
            job_summary["queued"] > 0
            or job_summary["running"] > 0
            or job_summary["failed"] > 0
            or job_summary["cancelled"] > 0
        ) or draft is not None
        active_task = None if scan_interrupted else _active_task(scan, active_job, revision_id, card_progress)
        cards.append({
            "root_id": root_id,
            "provider": str(root["provider"] or ""),
            "ingest_method": str(root["ingest_method"] or ""),
            "source_mode": str(root["source_mode"] or ""),
            "last_scan_mode": str(root["last_scan_mode"] or ""),
            "has_confirmed_baseline": has_confirmed,
            "source_locator": str(root["source_locator"] or ""),
            "display_path": _display_path(str(root["source_locator"] or ""), str(root["provider"] or "")),
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
            "attention_count": unresolved_issues.get(revision_id, 0)
            + (1 if scan_failed or scan_interrupted else 0),
            "last_error": last_error,
            "scan": scan_payload,
            "progress": card_progress,
            "active_task": active_task,
            "available_actions": ["inspect", "resume"] if can_resume else ["inspect", "update"],
            "can_resume": can_resume,
            "job_summary": job_summary,
        })
    return cards


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
        root = conn.execute(
            "SELECT enabled, retired_at FROM source_roots WHERE root_id = ?",
            (normalized_root_id,),
        ).fetchone()
        if root is None or str(root["retired_at"] or "") or not int(root["enabled"]):
            raise KeyError("来源卡不存在或已移除")
        active_scan_rows = conn.execute(
            "SELECT status, heartbeat_at FROM source_scans WHERE root_id = ?",
            (normalized_root_id,),
        ).fetchall()
        # queued 一定算活动（下一个 worker 会领取）；running/cancelling 只有
        # 心跳仍新鲜才算活动，失联的僵尸行不得永久阻止卡片删除。
        has_active_scan = any(
            str(row["status"]) == "queued"
            or (
                str(row["status"]) in {"running", "cancelling"}
                and not scan_is_stale(str(row["heartbeat_at"] or ""))
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
        "refresh_projection": "正在更新媒体库",
        "cleanup_superseded_artifacts": "正在整理旧文件",
    }
    return {
        "kind": "execution",
        "revision_id": revision_id,
        "status": status,
        "stage": job_type,
        "label": "正在终止任务" if status == "cancelling" else labels.get(job_type, "正在处理媒体库任务"),
        "percent": progress.get("percent"),
        "can_cancel": status in {"queued", "running"},
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
