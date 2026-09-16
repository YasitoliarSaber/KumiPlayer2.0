"""V4 Revision 草稿、确认和 transactional outbox。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import nullcontext
from dataclasses import asdict, replace
from datetime import UTC, datetime

from app.media_v4.domain.models import (
    ParsedFacts,
    ResolutionIssue,
    ResolvedMediaGraph,
    SourceEvidence,
)
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.repositories import V4Repository
from app.media_v4.resolution import candidates as candidate_service
from app.media_v4.resolution.candidates import CandidateSearch
from app.media_v4.resolution.identity_policy import historical_identity_conflict
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.resolution.title_norm import normalize_identity_title


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _friendly_job_error(value: str) -> str:
    """将持久任务的诊断错误投影为可行动的用户提示。"""

    text = (value or "").strip()
    if not text:
        return ""
    lowered = text.casefold()
    if "unique constraint failed" in lowered or "integrityerror" in lowered:
        return "媒体身份与已有记录冲突，请检查识别结果后重试"
    if "no such table" in lowered or "no such column" in lowered:
        return "媒体库结构不完整，请重试；如果仍失败，请检查数据库状态"
    if "timed out" in lowered or "timeout" in lowered:
        return "任务响应超时，请检查网络或来源连接后重试"
    if "filenotfound" in lowered or "enoent" in lowered or "文件不存在" in text:
        return "媒体文件或播放路径不可用，请检查来源后重试"
    if "permissionerror" in lowered or "access denied" in lowered or "拒绝访问" in text:
        return "无法访问媒体文件，请检查权限与挂载后重试"
    if "取消" in text or "cancel" in lowered:
        return "任务已终止"
    if text in {"磁盘写入失败", "任务异常中断，可重试"}:
        return text
    return "任务未能完成，请重试"


def _friendly_metadata_reason(status: str, value: str, reason_code: str = "") -> str:
    """元数据状态只暴露稳定的恢复说明，不暴露提供方异常细节。"""

    normalized_code = (reason_code or "").strip().casefold()
    normalized_value = (value or "").strip().casefold()
    identity_conflict = normalized_code in {
        "provider_identity_conflict",
        "work_identity_conflict",
        "historical_identity_conflict",
        "identity_conflict",
        "binding_conflict",
    } or any(
        marker in normalized_value
        for marker in ("关联到另一部作品", "作品身份冲突", "身份冲突", "provider 身份", "provider identity")
    )
    if identity_conflict:
        return "在线作品已关联到另一部作品，请选择正确作品或修正 Provider 后重试。"
    if normalized_code == "provider_auth_required":
        return "在线资料授权无效，请检查 TMDB Token 后重试。"
    if normalized_code == "provider_rate_limited":
        return "在线资料请求过于频繁，请稍后重试。"
    if normalized_code == "episode_mapping_incomplete":
        return "部分剧集资料暂不可用，作品信息已保留，可稍后重试。"
    if normalized_code == "artifact_incomplete":
        return "媒体资料已获取，但部分图片下载或发布失败。"
    if normalized_code == "mirror_root_missing":
        return "镜像目录未配置，无法生成元数据文件。"
    if status == "waiting_review":
        return "在线媒体信息没有唯一匹配，需要确认正确作品后继续。"
    if status == "source_unavailable":
        return "在线资料服务暂不可用，请稍后重试"
    if status == "waiting_metadata":
        return "媒体信息尚未准备好，请检查设置后重试"
    if status == "failed":
        return "媒体信息处理未能完成，请重试"
    return "" if not value else "媒体信息需要处理，请检查后重试"


def _artifact_view(metadata: dict | None) -> tuple[str, str, list[str]]:
    """返回 (metadata_state, artifact_state, artifact_reasons)。

    历史记录里“资料已拿到、只缺本地图片”的失败在这里做只读归一：作品照常算
    ready，产物标成 degraded；不写回真实数据。
    """

    from app.media_v4.jobs.completeness import artifact_only_failure

    payload = metadata if isinstance(metadata, dict) else {}
    state = str(payload.get("metadata_state") or "")
    artifact_state = str(payload.get("artifact_state") or "")
    raw_reasons = payload.get("artifact_reasons")
    reasons = [str(item) for item in raw_reasons] if isinstance(raw_reasons, (list, tuple)) else []
    if artifact_only_failure(payload):
        state = "ready"
        artifact_state = artifact_state or "degraded"
        if not reasons:
            fallback = payload.get("completeness")
            reasons = [str(item) for item in fallback] if isinstance(fallback, (list, tuple)) else []
    return state, artifact_state, reasons


def _metadata_recovery_action(status: str, reason_code: str = "", reason: str = "") -> str:
    """为前端提供稳定的恢复动作枚举，不把按钮逻辑散落到组件。"""

    return metadata_recovery_policy({
        "metadata_state": status,
        "reason_code": reason_code,
        "reason": reason,
    })["action"]


def _metadata_recovery_hint(action: str) -> str:
    return {
        "review_identity": "请检查识别结果，选择正确作品或修正 Provider 后重试。",
        "choose_candidate": "请从候选作品中选择正确的一部。",
        "retry_metadata": "可以稍后重试补齐在线资料。",
        "check_settings": "请先检查 TMDB Token、在线资料或镜像目录设置。",
        "none": "",
    }.get(action, "")


_IDENTITY_CONFLICT_CODES = frozenset({
    "provider_identity_conflict",
    "work_identity_conflict",
    "historical_identity_conflict",
    "identity_conflict",
    "binding_conflict",
})
_AUTH_CODES = frozenset({"provider_auth_required", "credentials_missing", "unauthorized"})


def _season_failure_label(item: dict) -> str:
    """把季度级失败转成不暴露 Provider 细节的用户称呼。"""

    local_number = _positive_detail_int(item.get("local_season_number"))
    if local_number == 0:
        return "特别篇"
    if local_number is not None:
        return f"第 {local_number} 季"
    return "该季度"


def _season_failure_reason(item: dict) -> str:
    label = _season_failure_label(item)
    code = str(item.get("reason_code") or "").strip().casefold()
    if code in _AUTH_CODES:
        return f"{label}在线资料授权失效，请检查 TMDB Token 后重试。"
    if code == "provider_rate_limited":
        return f"{label}在线资料请求过于频繁，请稍后重试。"
    if code == "provider_resource_missing":
        return f"{label}在线资料不可用：资料站可能采用不同的分季方式，尚未找到对应条目。"
    if code == "episode_not_found":
        return f"{label}在线集数未匹配，暂时保留本地剧集信息。"
    if code == "invalid_response":
        return f"{label}在线资料响应异常，可以稍后重试。"
    if code == "source_unavailable":
        return f"{label}在线资料服务暂不可用，请稍后重试。"
    return f"{label}在线资料需要处理，请检查后重试。"


def _join_season_labels(items: list[dict]) -> str:
    labels: list[str] = []
    for item in items:
        label = _season_failure_label(item)
        if label not in labels:
            labels.append(label)
    if len(labels) <= 2:
        return "、".join(labels)
    return "、".join(labels[:2]) + "等季度"


def metadata_recovery_policy(metadata: dict | None, *, binding_status: str = "") -> dict[str, str]:
    """根据同一份 metadata 快照决定展示原因、恢复动作和提示。

    季度错误不能在 API、进度投影和详情投影中各自解释；本函数只返回安全的
    用户文案与动作枚举，调用方仍可保留原始 reason_code/season_results 供内部
    处理。``binding_status`` 仅用于兼容旧记录中 metadata_state 缺失的情况。
    """

    payload = metadata if isinstance(metadata, dict) else {}
    state = str(payload.get("metadata_state") or binding_status or "").strip()
    reason_code = str(payload.get("reason_code") or "").strip().casefold()
    raw_reason = str(payload.get("reason") or "")
    normalized_reason = raw_reason.strip().casefold()
    failure_stage = str(payload.get("failure_stage") or "").strip().casefold()
    raw_season_results = payload.get("season_results")
    if not isinstance(raw_season_results, (list, tuple)):
        raw_season_results = []
    season_results = [
        item for item in raw_season_results
        if isinstance(item, dict) and str(item.get("reason_code") or "").strip()
    ]

    has_identity_conflict = reason_code in _IDENTITY_CONFLICT_CODES or any(
        marker in normalized_reason
        for marker in ("关联到另一部作品", "作品身份冲突", "身份冲突", "provider 身份", "provider identity")
    )
    if has_identity_conflict:
        action = "review_identity"
        reason = _friendly_metadata_reason(state, raw_reason, reason_code)
        return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}

    if reason_code == "special_episode_metadata_incomplete":
        # 特别篇线上条目是可选补全。作品身份、正片和本地 NFO 已经就绪时，
        # 不再把“没有对应条目”投影成待处理任务；详细页面另行展示 warning。
        return {"reason": "", "action": "none", "hint": ""}

    if reason_code in {"no_candidates", "ambiguous_candidates"} or state == "waiting_review":
        action = "choose_candidate"
        reason = _friendly_metadata_reason(state, raw_reason, reason_code)
        return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}

    auth_failures = [
        item for item in season_results
        if str(item.get("reason_code") or "").strip().casefold() in _AUTH_CODES
    ]
    if reason_code in _AUTH_CODES or auth_failures:
        if auth_failures:
            reason = f"{_join_season_labels(auth_failures)}在线资料授权失效，请检查 TMDB Token 后重试。"
        else:
            reason = _friendly_metadata_reason(state, raw_reason, reason_code)
        action = "check_settings"
        return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}

    if reason_code == "provider_resource_missing" and failure_stage == "work_detail":
        action = "choose_candidate"
        reason = "在线作品资料不可用，请重新选择正确的在线作品。"
        return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}

    if reason_code == "artifact_incomplete":
        # 只在**确有产物上下文**时才算"只缺本地图片"：`artifact_state=degraded`，或
        # 完整性原因全部是图片问题（`completeness`）。此时不得提供"重新获取在线资料"
        # ——那会发起一轮完整联网抓取，与"只重新下载缺失图片"的零网络承诺冲突；
        # 前端据此不显示联网重试按钮，补全走专门的图片重下入口。
        #
        # 没有产物上下文的历史快照保持原行为（retry_metadata）：那些行既没有
        # degraded 标记、也没有图片补全入口，若一律返回 none，用户将无动作可用。
        from app.media_v4.jobs.completeness import artwork_only_reasons

        artifact_context = (
            str(payload.get("artifact_state") or "").strip().casefold() == "degraded"
            or artwork_only_reasons(payload.get("completeness"))
        )
        if artifact_context:
            return {
                "reason": _friendly_metadata_reason(state, raw_reason, reason_code),
                "action": "none",
                "hint": "可只重新下载缺失的媒体图片，不需要重新获取在线资料。",
            }

    if season_results:
        missing_or_unmapped = [
            item for item in season_results
            if str(item.get("reason_code") or "").strip().casefold()
            in {"provider_resource_missing", "episode_not_found"}
        ]
        if missing_or_unmapped:
            action = "retry_metadata"
            hint = "这不表示资源有问题，无需重命名。可重试获取资料；仍有缺项时需检查在线分季对应关系。"
            if len(missing_or_unmapped) < len(season_results):
                # 混合失败各取一类，避免前两季的网络错误掩盖后续集号问题。
                other_failure = next(item for item in season_results if item not in missing_or_unmapped)
                reason = "；".join(
                    _season_failure_reason(item) for item in (other_failure, missing_or_unmapped[0])
                )
            elif all(
                str(item.get("reason_code") or "").strip().casefold() == "episode_not_found"
                for item in missing_or_unmapped
            ):
                reason = (
                    f"{_join_season_labels(missing_or_unmapped)}在线集数未匹配，暂时保留本地剧集信息。"
                )
            else:
                reason = "；".join(_season_failure_reason(item) for item in missing_or_unmapped[:2])
            return {"reason": reason, "action": action, "hint": hint}

        # 季度级网络、限流或响应异常仍允许用户手动重试；即使某次响应标记
        # retryable=false，也不能把“当前不宜自动恢复”误当成“永远不能再试”。
        action = "retry_metadata"
        reason = "；".join(_season_failure_reason(item) for item in season_results[:2])
        return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}

    if reason_code == "provider_resource_missing":
        # 旧快照可能没有 failure_stage；没有证据证明是作品详情 404 时，
        # 只允许安全重试，不擅自要求用户换绑作品身份。
        action = "retry_metadata"
        reason = "在线资料不可用，可以重试；若仍失败，请检查在线作品选择。"
        return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}
    if reason_code == "invalid_response":
        action = "retry_metadata"
        reason = "在线资料响应异常，可以稍后重试。"
        return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}

    action = _metadata_recovery_action_fallback(state, reason_code, raw_reason)
    reason = _friendly_metadata_reason(state, raw_reason, reason_code)
    return {"reason": reason, "action": action, "hint": _metadata_recovery_hint(action)}


def _metadata_recovery_action_fallback(status: str, reason_code: str, reason: str) -> str:
    """兼容无季度上下文的旧快照，供统一策略内部使用。"""

    code = (reason_code or "").strip().casefold()
    normalized_reason = (reason or "").strip().casefold()
    if code in _IDENTITY_CONFLICT_CODES or any(
        marker in normalized_reason
        for marker in ("关联到另一部作品", "作品身份冲突", "身份冲突", "provider 身份", "provider identity")
    ):
        return "review_identity"
    if code in {"no_candidates", "ambiguous_candidates"}:
        return "choose_candidate"
    if code in _AUTH_CODES:
        return "check_settings"
    if code in {"provider_rate_limited", "source_unavailable", "artifact_incomplete", "invalid_response", "provider_resource_missing"}:
        # artifact_incomplete 在没有产物上下文时保持"可联网重试"：这是历史快照的
        # 唯一可用动作；确为"只缺图片"的快照由 metadata_recovery_policy 提前拦下。
        return "retry_metadata"
    if code == "mirror_root_missing":
        return "check_settings"
    if status == "source_unavailable":
        return "retry_metadata"
    if status == "waiting_metadata":
        return "check_settings"
    if status == "failed":
        return "retry_metadata"
    if status == "waiting_review":
        return "choose_candidate"
    return "none"


def _job_summary(job: dict | None) -> dict:
    """把单个 job 折叠成作品单元可用的最小投影；无 job 时返回等待态。"""

    if job is None:
        return {"job_id": "", "status": "queued", "attempts": 0, "last_error": ""}
    return {
        "job_id": str(job["job_id"] or ""),
        "status": str(job["status"] or "queued"),
        "attempts": int(job["attempts"] or 0),
        "last_error": _friendly_job_error(str(job["last_error"] or "")),
    }


def _safe_detail_text(value: object) -> str:
    """将刮削详情中的可展示文本归一化为稳定字符串。"""

    return str(value or "").strip()


def _positive_detail_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _safe_detail_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None


def _safe_detail_text_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value:
        text = _safe_detail_text(item)
        if text and text not in result:
            result.append(text)
    return result[:20]


def _safe_season_results(value: object) -> list[dict]:
    """投影季度级资料状态，避免把原始异常/请求细节带到前端。"""

    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict] = []
    for item in value[:32]:
        if not isinstance(item, dict):
            continue
        result.append({
            "local_season_number": _positive_detail_int(item.get("local_season_number")),
            "provider_season_number": _positive_detail_int(item.get("provider_season_number")),
            "status": _safe_detail_text(item.get("status")),
            "reason_code": _safe_detail_text(item.get("reason_code")),
            "failure_stage": _safe_detail_text(item.get("failure_stage")),
            "retryable": bool(item.get("retryable")),
        })
    return result


def _safe_candidate_decision(value: object) -> dict | None:
    """只投影候选评分证据，拒绝把 metadata JSON 原样暴露给前端。"""

    if not isinstance(value, dict):
        return None
    ranked_candidates: list[dict] = []
    raw_candidates = value.get("ranked_candidates")
    if isinstance(raw_candidates, list):
        for candidate in raw_candidates[:12]:
            if not isinstance(candidate, dict):
                continue
            ranked_candidates.append({
                "provider": _safe_detail_text(candidate.get("provider")),
                "provider_id": _safe_detail_text(candidate.get("provider_id")),
                "media_type": _safe_detail_text(candidate.get("media_type")),
                "title": _safe_detail_text(candidate.get("title")),
                "original_title": _safe_detail_text(candidate.get("original_title")),
                "year": _positive_detail_int(candidate.get("year")),
                "score": _safe_detail_float(candidate.get("score")),
                "reasons": _safe_detail_text_list(candidate.get("reasons")),
                "recommended": bool(candidate.get("recommended")),
                "identity_safe": bool(candidate.get("identity_safe")),
                "blocked": bool(candidate.get("blocked")),
            })
    return {
        "decision": _safe_detail_text(value.get("decision")),
        "reason": _safe_detail_text(value.get("reason")),
        "selected_provider": _safe_detail_text(value.get("selected_provider")),
        "selected_provider_id": _safe_detail_text(value.get("selected_provider_id")),
        "selected_score": _safe_detail_float(value.get("selected_score")),
        "ranked_candidates": ranked_candidates,
    }


def _derive_work_status(mirror: dict | None, metadata: dict | None, scrape_status: str) -> str:
    """由 mirror + metadata jobs 推导作品单元的用户可见状态。

    优先级与文档一致：运行中 > 需要处理/失败/取消 > 等待 > 完成；
    失败/取消不能被后续 queued job 掩盖。scrape 元数据状态（waiting_review /
    source_unavailable / failed / waiting_metadata）不能显示为已完成。
    """

    def first(*jobs: dict | None) -> dict | None:
        return next((job for job in jobs if job is not None), None)

    mirror_job = first(mirror)
    metadata_job = first(metadata)
    if mirror_job is not None:
        if mirror_job["status"] == "running":
            return "running_mirror"
        if mirror_job["status"] == "failed":
            return "failed"
        if mirror_job["status"] == "cancelled":
            return "cancelled"
        if mirror_job["status"] == "queued":
            return "waiting_mirror"
        # mirror succeeded
        if metadata_job is not None:
            if metadata_job["status"] == "running":
                return "running_metadata"
            if metadata_job["status"] == "failed":
                return "failed"
            if metadata_job["status"] == "cancelled":
                return "cancelled"
            if metadata_job["status"] == "queued":
                return "waiting_metadata"
            # metadata succeeded
            if scrape_status and scrape_status not in {"ready", "confirmed"}:
                return "needs_attention"
            return "completed"
        return "waiting_metadata"
    # 理论上每个 work 都有 mirror job；异常情况按 metadata 状态展示。
    if metadata_job is not None:
        if metadata_job["status"] == "running":
            return "running_metadata"
        if metadata_job["status"] == "failed":
            return "failed"
        if metadata_job["status"] == "cancelled":
            return "cancelled"
        if metadata_job["status"] == "queued":
            return "waiting_metadata"
        if scrape_status and scrape_status not in {"ready", "confirmed"}:
            return "needs_attention"
        return "completed"
    return "waiting_mirror"


def _stage_status(jobs: list[dict]) -> str:
    """阶段级状态：running > failed/cancelled > queued > succeeded；空为 idle。"""

    if not jobs:
        return "idle"
    statuses = [str(job["status"]) for job in jobs]
    if "running" in statuses:
        return "running"
    if "failed" in statuses:
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    if "queued" in statuses:
        return "queued"
    return "succeeded"


def _stage_summary(jobs: list[dict]) -> dict:
    return {
        "status": _stage_status(jobs),
        "total": len(jobs),
        "queued": sum(1 for job in jobs if job["status"] == "queued"),
        "running": sum(1 for job in jobs if job["status"] == "running"),
        "succeeded": sum(1 for job in jobs if job["status"] == "succeeded"),
        "failed": sum(1 for job in jobs if job["status"] == "failed"),
        "cancelled": sum(1 for job in jobs if job["status"] == "cancelled"),
        "needs_attention": 0,
    }


def _metadata_stage_summary(jobs: list[dict], scrape_rows: dict[str, dict]) -> dict:
    """把刮削 job 的成功与“已完成但需处理”分开统计。"""

    summary = _stage_summary(jobs)
    attention = sum(
        1
        for job in jobs
        if str(job.get("status") or "") == "succeeded"
        and str(scrape_rows.get(str(job.get("work_id") or ""), {}).get("metadata_state") or "")
        not in {"", "ready", "confirmed"}
    )
    summary["needs_attention"] = attention
    summary["succeeded"] = max(0, int(summary["succeeded"]) - attention)
    if attention and summary["status"] == "succeeded":
        summary["status"] = "needs_attention"
    return summary


def _revision_overall_status(work_units: list[dict], stage: dict[str, list[dict]]) -> str:
    """revision 级整体状态：running > needs_attention > queued > cancelled > completed。"""

    if any(unit["overall_status"] in {"running_mirror", "running_metadata"} for unit in work_units):
        return "running"
    if any(stage["projection"] and str(job["status"]) == "running" for job in stage["projection"]):
        return "running"
    if any(unit["overall_status"] in {"failed", "needs_attention"} for unit in work_units):
        return "needs_attention"
    if any(
        str(job["status"]) == "failed"
        for stage_jobs in stage.values()
        for job in stage_jobs
    ):
        return "needs_attention"
    if any(
        str(job["status"]) in {"queued"}
        for stage_jobs in stage.values()
        for job in stage_jobs
    ):
        return "queued"
    if any(unit["overall_status"] == "waiting_mirror" or unit["overall_status"] == "waiting_metadata" for unit in work_units):
        return "queued"
    if any(unit["overall_status"] == "cancelled" for unit in work_units) or any(
        str(job["status"]) == "cancelled"
        for stage_jobs in stage.values()
        for job in stage_jobs
    ):
        return "cancelled"
    return "completed"



def _normalize_title(value: str) -> str:
    """身份语义：唯一实现在 `title_norm.normalize_identity_title`。"""

    return normalize_identity_title(value)


_SEASON_SPECIFIC_TITLE = re.compile(
    r"(?ix)(?:\bS\d{1,2}(?:\s*E\d{1,3})?\b|\bSeason\s*\d+\b|"
    r"\b\d+(?:st|nd|rd|th)\s+Season\b|第\s*\d+\s*季)"
)


def _is_season_specific_title(value: str) -> bool:
    return _SEASON_SPECIFIC_TITLE.search(value or "") is not None


def _should_preserve_existing_title(existing_title: str, draft_title: str) -> bool:
    """防止季度目录名覆盖已有的规范作品名。"""

    return bool(
        existing_title.strip()
        and draft_title.strip()
        and not _is_season_specific_title(existing_title)
        and _is_season_specific_title(draft_title)
    )


def _merge_map_from_candidates(
    graph: ResolvedMediaGraph,
    candidates_by_key: dict[str, list],
) -> dict[str, str]:
    """从候选快照按确定性标题规则重建合并映射。"""

    return candidate_service.merge_map_from_candidates(graph, candidates_by_key)


def _persist_candidates(
    conn,
    revision_id: str,
    work_ids: dict[str, str],
    candidates_by_key: dict[str, list],
    created_at: str,
) -> None:
    """按 revision_id + draft_work_key 持久化候选（P-001 7.7 R1）。"""

    conn.execute(
        "DELETE FROM revision_work_candidates WHERE revision_id = ?", (revision_id,)
    )
    now = _now()
    for work_key, items in candidates_by_key.items():
        work_id = work_ids.get(work_key, "")
        for item in items:
            conn.execute(
                """
                INSERT OR IGNORE INTO revision_work_candidates(
                    candidate_id, revision_id, work_id, draft_work_key, provider,
                    provider_id, media_type, title, original_title, aliases_json, year,
                    evidence, confidence, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    work_id,
                    work_key,
                    item.provider,
                    item.provider_id,
                    item.media_type,
                    item.title,
                    item.original_title,
                    json.dumps(list(item.aliases), ensure_ascii=False),
                    item.year,
                    item.evidence,
                    item.confidence,
                    item.status,
                    now,
                    now,
                ),
            )


def _reuses_confirmed_provider_identity(conn, work_id: str, candidates: list) -> bool:
    """仅当本次草稿复用了已确认身份时，才保护现有的作品标题。"""

    confirmed = {
        (item.provider, item.media_type, item.provider_id)
        for item in candidates
        if item.status == "confirmed" and candidate_service.supported_provider(item.provider)
    }
    if not confirmed:
        return False
    existing = {
        (str(row["provider"]), str(row["media_type"]), str(row["provider_id"]))
        for row in conn.execute(
            "SELECT provider, media_type, provider_id FROM provider_bindings WHERE work_id = ?",
            (work_id,),
        ).fetchall()
    }
    return bool(confirmed & existing)


def _freeze_candidate_bindings(
    conn,
    work_ids: dict[str, str],
    candidates_by_key: dict[str, list],
    created_at: str,
) -> None:
    """确认时把高置信唯一身份写入 provider_bindings，冻结身份供 scrape 消费。"""

    for work_key, work_id in work_ids.items():
        confirmed = [
            item for item in candidates_by_key.get(work_key, [])
            if item.status == "confirmed" and candidate_service.supported_provider(item.provider)
        ]
        unique_identities = {(item.provider, item.media_type, item.provider_id) for item in confirmed}
        if len(unique_identities) != 1:
            continue
        chosen = confirmed[0]
        existing_slot = conn.execute(
            """
            SELECT provider_id FROM provider_bindings
            WHERE work_id = ? AND provider = ? AND media_type = ?
            """,
            (work_id, chosen.provider, chosen.media_type),
        ).fetchone()
        if existing_slot is not None and str(existing_slot["provider_id"]) != chosen.provider_id:
            raise RevisionBlockedError(
                "Provider 身份冲突：该作品已经绑定另一条确认身份，请返回检查识别结果"
            )
        from app.media_v4.persistence.identity_lifecycle import release_inactive_provider_identity

        release_inactive_provider_identity(
            conn, chosen.provider, chosen.media_type, chosen.provider_id
        )
        identity_owner = conn.execute(
            """
            SELECT work_id FROM provider_bindings
            WHERE provider = ? AND media_type = ? AND provider_id = ?
            """,
            (chosen.provider, chosen.media_type, chosen.provider_id),
        ).fetchone()
        if identity_owner is not None and str(identity_owner["work_id"]) != work_id:
            raise RevisionBlockedError(
                "Provider 身份冲突：该身份已经属于另一部作品，请返回检查识别结果"
            )
        conn.execute(
            """
            INSERT INTO provider_bindings(work_id, provider, media_type, provider_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(work_id, provider, media_type) DO NOTHING
            """,
            (work_id, chosen.provider, chosen.media_type, chosen.provider_id),
        )


def _structural_key(
    relative_path: str,
    *,
    facts: ParsedFacts | None = None,
    work_key: str = "",
) -> str:
    """返回来源根内可复用的稳定作品边界。

    作品边界由解析事实决定：主系列使用 ``series_group``，独立作品使用
    自己的 ``work_title``。不能再把最近的 SPs、季度或发行版本目录当作
    Work binding；缺少明确边界时宁可不创建结构绑定，交给身份/候选规则处理。
    """

    if facts is None:
        return ""
    hinted_type = facts.tmdb_hint_type.casefold()
    media_type = (
        hinted_type
        if facts.tmdb_hint_id and hinted_type in {"movie", "tv"}
        else (facts.media_type or ("movie" if facts.group_type == "movie" else "tv"))
    ).casefold()

    if facts.card_type == "standalone":
        title = _normalize_title(facts.work_title)
        if title:
            return f"work:{title}:{media_type}"
        return ""
    if facts.card_type == "main_series" and facts.series_group:
        series = _normalize_title(facts.series_group)
        if series:
            return f"series:{series}:{media_type}"
    if work_key.startswith("series:") and facts.series_group:
        series = _normalize_title(facts.series_group)
        if series:
            return f"series:{series}:{media_type}"
    return ""


def _snapshot_structural_bindings(conn, root_id: str) -> dict[str, list[dict]]:
    """在确认事务写入新 Work 前冻结既有来源结构绑定。

    确认同一批图时不能读取本事务刚插入的 binding，否则先处理的错误
    特别篇会把季度目录绑定到主系列，再反向吞并后续季度 Work。
    """

    rows = conn.execute(
        """
        SELECT b.structural_key, w.work_id, w.work_type, w.year
        FROM work_source_bindings b
        JOIN works w ON w.work_id = b.work_id
        JOIN source_roots sr ON sr.root_id = b.root_id
        WHERE b.root_id = ? AND w.status = 'active' AND sr.retired_at = ''
        """,
        (root_id,),
    ).fetchall()
    from app.media_v4.persistence.identity_lifecycle import retired_only_work_ids

    retired_ids = retired_only_work_ids(conn)
    snapshot: dict[str, list[dict]] = {}
    for row in rows:
        if str(row["work_id"]) in retired_ids:
            continue
        snapshot.setdefault(str(row["structural_key"]), []).append(
            {
                "work_id": str(row["work_id"]),
                "work_type": str(row["work_type"]),
                "year": row["year"],
            }
        )
    return snapshot


def _lookup_work_by_key(conn, work_key: str, *, exclude_work_id: str = "") -> str:
    row = conn.execute(
        "SELECT work_id FROM works WHERE identity_key = ? AND status = 'active'", (work_key,)
    ).fetchone()
    if row:
        return str(row["work_id"])
    # 显式合集产生 series:<title>:<type> 身份；同一父作品若在较早 revision
    # 以普通 title 身份确认，关系仍应通过已持久化别名确定性复用。只接受唯一
    # 命中，避免同名作品被静默串联。
    if not work_key.startswith("series:"):
        return ""
    try:
        normalized_title, media_type = work_key[len("series:"):].rsplit(":", 1)
    except ValueError:
        return ""
    work_type = "movie" if media_type == "movie" else "series" if media_type == "tv" else ""
    if not normalized_title or not work_type:
        return ""
    rows = conn.execute(
        """
        SELECT DISTINCT works.work_id
        FROM works
        JOIN work_aliases ON work_aliases.work_id = works.work_id
        WHERE work_aliases.normalized_title = ? AND works.work_type = ?
          AND works.status = 'active'
          AND (? = '' OR works.work_id != ?)
        """,
        (normalized_title, work_type, exclude_work_id, exclude_work_id),
    ).fetchall()
    return str(rows[0]["work_id"]) if len(rows) == 1 else ""


def _work_identity_titles(work, related_entries: list[tuple[SourceEvidence, ParsedFacts]] | None = None) -> set[str]:
    """生成可用于复用 Work 的自身标题集合。

    ``series_group`` 既可能是父系列容器，也可能就是普通目录唯一提供的
    作品名。只有明确的独立子作品语义才排除父名；主系列自身名必须保留，
    否则 TXT/OpenList 的普通目录会失去唯一离线身份证据。
    """

    return {
        _normalize_title(title)
        for title in candidate_service.work_identity_title_inputs(
            work, [facts for _evidence, facts in related_entries or []],
        )
        if _normalize_title(title)
    }


def _existing_work_matches(
    conn,
    work,
    related_entries: list[tuple[SourceEvidence, ParsedFacts]] | None = None,
) -> list:
    """按 identity key 或自身标题寻找相容已有 Work，返回所有安全候选。

    返回一个元素表示可确定复用；多个元素表示同名历史记录仍有冲突，调用方
    必须阻止静默新建/绑定。若多个同名记录中只有一个持有 Provider 身份，
    将其视为唯一权威 owner，以修复历史上“空副本 + 正确 owner”的情况。
    """

    from app.media_v4.persistence.identity_lifecycle import retired_only_work_ids
    from app.media_v4.resolution.ranker import _normalize_title as compare_title

    retired_ids = retired_only_work_ids(conn)
    work_type = "series" if str(getattr(work, "media_type", "") or "") == "tv" else "movie"
    work_year = getattr(work, "year", None)
    exact = conn.execute(
        "SELECT * FROM works WHERE identity_key = ? AND work_type = ? "
        "AND status = 'active' AND (year IS NULL OR year = ? OR ? IS NULL)",
        (getattr(work, "work_key", ""), work_type, work_year, work_year),
    ).fetchone()
    def safe_historical_row(row):
        if str(row["work_id"]) in retired_ids:
            return None
        structural_keys = {
            str(binding["structural_key"] or "")
            for binding in conn.execute(
                "SELECT b.structural_key FROM work_source_bindings b "
                "JOIN source_roots sr ON sr.root_id = b.root_id "
                "WHERE b.work_id = ? AND sr.retired_at = ''",
                (str(row["work_id"]),),
            ).fetchall()
        }
        if historical_identity_conflict(work, structural_keys) is not None:
            return None
        return row

    exact = safe_historical_row(exact) if exact is not None else None
    titles = _work_identity_titles(work, related_entries)
    comparison_titles = {compare_title(title) for title in titles}
    verified_title_owners: set[str] = set()
    # 已确认的在线译名是同一作品的身份事实。按当前 Provider 绑定限定，
    # 不读取已退役或失败候选，也不让过期刮削记录恢复旧身份。
    provider_titles: dict[str, set[str]] = {}
    for scraped in conn.execute(
        "SELECT sb.work_id, sb.metadata_json FROM scrape_bindings sb "
        "JOIN provider_bindings pb ON pb.work_id=sb.work_id "
        "AND pb.provider=sb.provider AND pb.provider_id=sb.provider_id "
        "JOIN works w ON w.work_id=sb.work_id "
        "WHERE w.status='active' AND w.work_type=? AND sb.status='confirmed' "
        "AND pb.media_type=? ORDER BY sb.updated_at DESC",
        (work_type, 'tv' if work_type == 'series' else 'movie'),
    ).fetchall():
        owner_id = str(scraped['work_id'])
        if owner_id in provider_titles or owner_id in retired_ids:
            continue
        try:
            payload = json.loads(scraped['metadata_json'] or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get('metadata_state') != 'ready':
            continue
        provider_titles[owner_id] = {
            compare_title(str(payload.get(key) or '')) for key in ('title', 'original_title')
        } - {''}
    title_rows: list = []
    if titles:
        rows = conn.execute(
            """
            SELECT DISTINCT w.*
            FROM works w
            LEFT JOIN work_aliases a ON a.work_id = w.work_id
            WHERE w.status = 'active' AND w.work_type = ?
              AND (w.year IS NULL OR w.year = ? OR ? IS NULL)
            """,
            (work_type, work_year, work_year),
        ).fetchall()
        for row in rows:
            names = {_normalize_title(str(row["preferred_title"] or ""))}
            names.update(
                _normalize_title(str(alias["normalized_title"] or ""))
                for alias in conn.execute(
                    "SELECT normalized_title FROM work_aliases WHERE work_id = ?",
                    (row["work_id"],),
                ).fetchall()
            )
            translated_match = bool(provider_titles.get(str(row['work_id']), set()) & comparison_titles)
            if {compare_title(name) for name in names} & comparison_titles or translated_match:
                safe_row = safe_historical_row(row)
                if safe_row is not None:
                    title_rows.append(safe_row)
                    if translated_match:
                        verified_title_owners.add(str(row['work_id']))
    # identity_key 是强身份线索，但不能单独覆盖一个已有的唯一 Provider
    # owner：历史上曾先生成空的 series 键、后又留下带 Provider 的 title 键，
    # 正是本次 TXT 批次冲突的来源。只有“首选标题相同”的 Provider owner
    # 才能覆盖精确键；仅通过外传保存的父系列别名命中的记录不能抢走主系列。
    if exact is not None:
        exact_preferred = _normalize_title(str(exact["preferred_title"] or ""))
        preferred_rows = [
            row for row in title_rows
            if compare_title(str(row["preferred_title"] or "")) in comparison_titles
            or str(row['work_id']) in verified_title_owners
        ]
        preferred_ids = {str(row["work_id"]) for row in preferred_rows}
        owner_rows = conn.execute(
            "SELECT DISTINCT work_id FROM provider_bindings "
            "WHERE work_id IN (" + ",".join("?" for _ in title_rows) + ")",
            tuple(str(row["work_id"]) for row in title_rows),
        ).fetchall() if title_rows else []
        owner_ids = {str(row["work_id"]) for row in owner_rows}
        if exact_preferred in titles:
            preferred_owners = preferred_ids & owner_ids
            if len(preferred_owners) == 1:
                owner_id = next(iter(preferred_owners))
                return [row for row in title_rows if str(row["work_id"]) == owner_id]
            if len(preferred_owners) > 1:
                return [row for row in title_rows if str(row["work_id"]) in preferred_owners]
            return [exact]
        if len(owner_ids) == 1:
            owner_id = next(iter(owner_ids))
            return [row for row in title_rows if str(row["work_id"]) == owner_id]
        if len(owner_ids) > 1:
            return [row for row in title_rows if str(row["work_id"]) in owner_ids]
        return [exact]
    if title_rows:
        placeholders = ",".join("?" for _ in title_rows)
        owner_rows = conn.execute(
            "SELECT DISTINCT work_id FROM provider_bindings "
            f"WHERE work_id IN ({placeholders})",
            tuple(str(row["work_id"]) for row in title_rows),
        ).fetchall()
        if len(owner_rows) == 1:
            owner_id = str(owner_rows[0]["work_id"])
            return [row for row in title_rows if str(row["work_id"]) == owner_id]
        return title_rows
    return [exact] if exact is not None else []


class RevisionBlockedError(RuntimeError):
    """Revision 仍有未解决 review issue。"""


class V4RevisionService:
    """把一次解析结果固化为 revision，并以单事务发布执行任务。"""

    def __init__(self, database: V4Database):
        self.database = database
        self.repository = V4Repository(database)
        self.resolver = MediaResolver()

    _OVERRIDE_FIELDS = frozenset({
        "work_title",
        "original_title",
        "series_group",
        "title_candidates",
        "year_candidate",
        "media_type",
        "group_type",
        "season_candidate",
        "episode_candidate",
        "absolute_episode_candidate",
        "special_candidate",
        "special_number",
        "tmdb_hint_id",
        "tmdb_hint_type",
        "edition_tags",
        "needs_review",
        "is_importable",
        "is_auxiliary",
    })

    def _load_draft_candidates(self, revision_id: str, conn=None) -> dict[str, list]:
        """确认时复用 draft 已冻结候选，避免再次联网搜索。"""

        from app.media_v4.resolution.candidates import WorkCandidate

        with (nullcontext(conn) if conn is not None else self.database.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM revision_work_candidates WHERE revision_id = ?",
                (revision_id,),
            ).fetchall()
        result: dict[str, list] = {}
        for row in rows:
            key = str(row["draft_work_key"])
            result.setdefault(key, []).append(WorkCandidate(
                work_key=key,
                provider=str(row["provider"]),
                provider_id=str(row["provider_id"]),
                media_type=str(row["media_type"]),
                title=str(row["title"]),
                original_title=str(row["original_title"] or ""),
                aliases=tuple(
                    str(alias) for alias in json.loads(row["aliases_json"] or "[]")
                ),
                year=row["year"],
                evidence=str(row["evidence"]),
                confidence=str(row["confidence"]),
                status=str(row["status"]),
            ))
        return result

    def _existing_bindings_by_key(
        self,
        graph: ResolvedMediaGraph,
        entries: list[tuple[SourceEvidence, ParsedFacts]] | None = None,
        conn=None,
    ) -> dict[str, list[tuple[str, str, str]]]:
        """读取可安全归属的既有 provider binding，供跨 revision 候选复用。

        目录树、OpenList 与本地扫描可能为同一作品生成不同的 identity_key。
        先按 identity_key 命中；未命中时再用规范作品标题/别名和媒体类型、
        年份兼容性寻找唯一 owner。多个同名 owner 时保持空结果，让后续
        review 流程处理，不能按数据库遍历顺序猜一个。
        """

        bindings: dict[str, list[tuple[str, str, str]]] = {}
        with (nullcontext(conn) if conn is not None else self.database.connect()) as conn:
            for work in graph.works:
                related_entries = [
                    (evidence, facts)
                    for evidence, facts in entries or []
                    if evidence.evidence_id in work.source_evidence_ids
                ]
                matches = _existing_work_matches(conn, work, related_entries)
                if len(matches) != 1:
                    bindings[work.work_key] = []
                    continue
                rows = conn.execute(
                    """
                    SELECT pb.provider, pb.media_type, pb.provider_id
                    FROM provider_bindings pb
                    WHERE pb.work_id = ?
                    """,
                    (str(matches[0]["work_id"]),),
                ).fetchall()
                bindings[work.work_key] = [(str(r[0]), str(r[1]), str(r[2])) for r in rows]
        return bindings

    def _candidate_identity_conflicts(
        self,
        graph: ResolvedMediaGraph,
        candidates_by_key: dict[str, list],
        entries: list[tuple[SourceEvidence, ParsedFacts]],
        conn=None,
    ) -> list[ResolutionIssue]:
        """在草稿预览阶段阻断会改写既有 Work 身份的候选。

        同一个本地 ``identity_key`` 已经确认过某个 provider/media_type
        身份时，新的草稿不能静默换绑到另一个 provider_id。尤其当新 ID 已
        属于另一 Work 时，确认阶段会触发 SQLite 的全局唯一索引；在这里先
        生成可读 review issue，既不泄漏 SQL，也避免用户等到第 3 步才发现。
        """

        issues: list[ResolutionIssue] = []
        evidence_by_id = {evidence.evidence_id: evidence for evidence, _facts in entries}
        facts_by_evidence_id = {evidence.evidence_id: facts for evidence, facts in entries}
        with (nullcontext(conn) if conn is not None else self.database.connect()) as conn:
            from app.media_v4.persistence.identity_lifecycle import retired_only_work_ids

            retired_ids = retired_only_work_ids(conn)
            for work in graph.works:
                existing_work_ids: set[str] = set()
                related_entries = [
                    (evidence_by_id[evidence_id], facts_by_evidence_id[evidence_id])
                    for evidence_id in work.source_evidence_ids
                    if evidence_id in evidence_by_id
                ]
                title_matches = _existing_work_matches(conn, work, related_entries)
                if len(title_matches) > 1:
                    issues.append(ResolutionIssue(
                        code="work_identity_ambiguous",
                        evidence_id=next(iter(work.source_evidence_ids), ""),
                        message="同名作品已有多个相容记录，无法安全复用，请先合并或修正识别结果",
                    ))
                    continue
                if len(title_matches) == 1:
                    existing_work_ids.add(str(title_matches[0]["work_id"]))
                # Provider key 会把跨语言目录合并到同一候选 Work；同时仍要以
                # 来源根 + 结构目录检查既有本地谱系，防止一个错误 hint 把
                # Show One 重绑到已经属于 Show Two 的 provider_id。
                for evidence_id in work.source_evidence_ids:
                    evidence = evidence_by_id.get(evidence_id)
                    if evidence is None:
                        continue
                    structural_key = _structural_key(
                        evidence.relative_path,
                        facts=facts_by_evidence_id.get(evidence.evidence_id),
                        work_key=work.work_key,
                    )
                    if not structural_key:
                        continue
                    rows = conn.execute(
                        """
                        SELECT DISTINCT b.work_id
                        FROM work_source_bindings b
                        JOIN works w ON w.work_id = b.work_id
                        JOIN source_roots sr ON sr.root_id = b.root_id
                        WHERE b.root_id = ? AND b.structural_key = ?
                          AND w.status = 'active' AND sr.retired_at = ''
                        """,
                        (evidence.root_id, structural_key),
                    ).fetchall()
                    existing_work_ids.update(str(row["work_id"]) for row in rows)
                # Provider owner 可能没有被标题/结构查询选中，但确认阶段仍会
                # 通过全局 Provider 唯一键回收它。先检查 owner 的历史结构边界，
                # 防止污染 Work 绕过上面的安全复用筛选再次进入 confirmed。
                for candidate in candidates_by_key.get(work.work_key, []):
                    if (
                        candidate.status not in {"confirmed", "proposed"}
                        or candidate.confidence != "high"
                        or not candidate_service.supported_provider(candidate.provider)
                    ):
                        continue
                    owner = conn.execute(
                        "SELECT work_id FROM provider_bindings "
                        "WHERE provider = ? AND media_type = ? AND provider_id = ?",
                        (candidate.provider, candidate.media_type, candidate.provider_id),
                    ).fetchone()
                    if owner is None or str(owner["work_id"]) in retired_ids:
                        continue
                    owner_keys = {
                        str(binding["structural_key"] or "")
                        for binding in conn.execute(
                            "SELECT b.structural_key FROM work_source_bindings b "
                            "JOIN source_roots sr ON sr.root_id = b.root_id "
                            "WHERE b.work_id = ? AND sr.retired_at = ''",
                            (str(owner["work_id"]),),
                        ).fetchall()
                    }
                    if historical_identity_conflict(work, owner_keys) is not None:
                        issues.append(ResolutionIssue(
                            code="work_identity_conflict",
                            evidence_id=next(iter(work.source_evidence_ids), ""),
                            message=(
                                "Provider 身份属于一个同时覆盖主系列与独立作品的历史记录；"
                                "请先执行作品身份修复，不能继续复用"
                            ),
                        ))
                        break
                existing_work_ids -= retired_ids
                if not existing_work_ids:
                    continue
                placeholders = ",".join("?" for _ in existing_work_ids)
                bindings = {
                    (str(row["provider"]), str(row["media_type"])): str(row["provider_id"])
                    for row in conn.execute(
                        "SELECT provider, media_type, provider_id FROM provider_bindings "
                        f"WHERE work_id IN ({placeholders})",
                        tuple(sorted(existing_work_ids)),
                    ).fetchall()
                }
                for candidate in candidates_by_key.get(work.work_key, []):
                    if (
                        candidate.status not in {"confirmed", "proposed"}
                        or candidate.confidence != "high"
                        or not candidate_service.supported_provider(candidate.provider)
                    ):
                        continue
                    existing_id = bindings.get((candidate.provider, candidate.media_type))
                    if existing_id is None or existing_id == candidate.provider_id:
                        continue
                    issues.append(ResolutionIssue(
                        code="provider_identity_conflict",
                        evidence_id=next(iter(work.source_evidence_ids), ""),
                        message=(
                            "识别到的 Provider 身份与该作品已确认的身份冲突；"
                            "请在检查识别结果中修正作品或 Provider 提示后重试"
                        ),
                    ))
                    break
        return issues

    def _structural_identity_conflicts(
        self,
        graph: ResolvedMediaGraph,
        entries: list[tuple[SourceEvidence, ParsedFacts]],
        conn=None,
    ) -> list[ResolutionIssue]:
        """阻止一个稳定来源边界静默对应多个既有 Work。"""

        evidence_by_id = {evidence.evidence_id: evidence for evidence, _facts in entries}
        facts_by_evidence_id = {evidence.evidence_id: facts for evidence, facts in entries}
        issues: list[ResolutionIssue] = []
        with (nullcontext(conn) if conn is not None else self.database.connect()) as conn:
            from app.media_v4.persistence.identity_lifecycle import retired_only_work_ids

            retired_ids = retired_only_work_ids(conn)
            for work in graph.works:
                work_entries = [
                    (evidence_by_id[evidence_id], facts_by_evidence_id.get(evidence_id))
                    for evidence_id in work.source_evidence_ids
                    if evidence_id in evidence_by_id
                ]
                root_ids = {evidence.root_id for evidence, _facts in work_entries}
                if len(root_ids) != 1:
                    continue
                structural_keys = {
                    _structural_key(
                        evidence.relative_path,
                        facts=facts,
                        work_key=work.work_key,
                    )
                    for evidence, facts in work_entries
                }
                structural_keys.discard("")
                if not structural_keys:
                    continue
                owner_ids: set[str] = set()
                for structural_key in structural_keys:
                    rows = conn.execute(
                        """
                        SELECT DISTINCT b.work_id
                        FROM work_source_bindings b
                        JOIN works w ON w.work_id = b.work_id
                        JOIN source_roots sr ON sr.root_id = b.root_id
                        WHERE b.root_id = ? AND b.structural_key = ?
                          AND w.status = 'active' AND sr.retired_at = ''
                        """,
                        (next(iter(root_ids)), structural_key),
                    ).fetchall()
                    owner_ids.update(str(row["work_id"]) for row in rows)
                identity_row = conn.execute(
                    "SELECT work_id FROM works WHERE identity_key = ? AND status = 'active'",
                    (work.work_key,),
                ).fetchone()
                owner_ids -= retired_ids
                identity_id = str(identity_row["work_id"]) if identity_row is not None else ""
                if identity_id in retired_ids:
                    identity_id = ""
                if len(owner_ids) <= 1 and (not identity_id or not owner_ids or identity_id in owner_ids):
                    continue
                evidence_id = next(iter(work.source_evidence_ids), "")
                issues.append(
                    ResolutionIssue(
                        code="structural_identity_ambiguous",
                        evidence_id=evidence_id,
                        message="同一来源作品边界已对应多部作品，无法安全复用，请检查识别结果后重试",
                    )
                )
        return issues

    def _evaluate_draft(
        self,
        entries: list[tuple[SourceEvidence, ParsedFacts]],
        *,
        conn,
        frozen_candidates: dict[str, list] | None = None,
        candidate_search: CandidateSearch | None = None,
    ) -> tuple[ResolvedMediaGraph, dict[str, list]]:
        """在调用方事务快照内重算草稿图、候选和全部身份检查。

        ``frozen_candidates`` 只作为离线候选输入，绝不触发网络搜索；人工
        修正导致 Work key 改变时，旧 key 没有候选会自然回到待确认状态。
        """

        graph = self.resolver.resolve(entries)
        if frozen_candidates is not None:
            def search(work_key, _queries, _year, _media_type):
                return frozen_candidates.get(work_key, [])
        elif candidate_search is not None:
            search = candidate_search
        else:
            def search(_work_key, _queries, _year, _media_type):
                return []

        existing_bindings = self._existing_bindings_by_key(
            graph,
            entries,
            conn=conn,
        )
        candidates_by_key, merge_map, candidate_issues = candidate_service.plan_work_candidates(
            graph,
            entries,
            search,
            existing_bindings=existing_bindings,
            preserve_candidate_status=frozen_candidates is not None,
        )
        candidate_issues.extend(
            self._candidate_identity_conflicts(
                graph,
                candidates_by_key,
                entries,
                conn=conn,
            )
        )
        candidate_issues.extend(
            self._structural_identity_conflicts(graph, entries, conn=conn)
        )
        graph = candidate_service.merge_graph(graph, merge_map)
        if candidate_issues:
            graph = replace(graph, issues=(*graph.issues, *candidate_issues))
        return graph, candidates_by_key

    @staticmethod
    def _graph_digest(graph: ResolvedMediaGraph) -> str:
        return hashlib.sha256(
            json.dumps(asdict(graph), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def create_draft(
        self,
        revision_id: str,
        entries: list[tuple[SourceEvidence, ParsedFacts]],
        *,
        resolver_version: str = "v4-resolver-1",
        root_id: str = "",
        scan_id: str = "",
        source_provider: str = "local",
        ingest_method: str = "local_scan",
        source_metadata: dict[str, str] | None = None,
        source_mode: str = "",
        _publish: bool = False,
        _evidence_already_persisted: bool = False,
        _facts_already_persisted: bool = False,
        _override_payloads: dict[str, dict] | None = None,
        candidate_search: CandidateSearch | None = None,
    ) -> ResolvedMediaGraph:
        if entries and any(evidence.root_id != entries[0][0].root_id for evidence, _ in entries):
            raise ValueError("同一 revision 不能混合多个来源根")
        if entries:
            root_id = entries[0][0].root_id
            scan_id = entries[0][0].scan_id
            source_provider = entries[0][0].provider
            ingest_method = entries[0][0].ingest_method
        if not root_id or not scan_id:
            raise ValueError("空 revision 必须明确提供 root_id 和 scan_id")
        # P-002 8.4：来源根级模式是独立权威字段，不再从排序后的第一条文件证据
        # 反推。显式 source_mode 存在时，root 级 ingest_method 只保留为兼容遗留
        # 列并按其稳定映射；文件级 SourceEvidence.ingest_method 仍表达观测方式。
        if source_mode:
            ingest_method = {
                "local": "local_scan",
                "tree_snapshot": "directory_tree",
                "tree_openlist": "directory_tree",
                "openlist_full": "openlist_scan",
            }.get(source_mode, ingest_method)

        graph = self.resolver.resolve(entries)
        # 首次草稿只做离线身份解析：显式 TMDB hint、sidecar NFO 与既有
        # provider binding 可以冻结候选；普通标题的在线搜索由 confirmed
        # revision 的 scrape job 执行。这样来源扫描不会因作品数量放大网络
        # 请求，也不会用不稳定的在线候选反向吞并本地主系列/外传边界。
        candidates_by_key: dict[str, list] = {}
        if _publish:
            candidates_by_key = self._load_draft_candidates(revision_id)
            graph = candidate_service.merge_graph(
                graph,
                _merge_map_from_candidates(graph, candidates_by_key),
            )
        else:
            if candidate_search is not None:
                search = candidate_search
            else:
                def search(_work_key, _queries, _year, _media_type):
                    return []

            existing_bindings = self._existing_bindings_by_key(graph, entries)
            candidates_by_key, merge_map, candidate_issues = candidate_service.plan_work_candidates(
                graph,
                entries,
                search,
                existing_bindings=existing_bindings,
            )
            candidate_issues.extend(self._candidate_identity_conflicts(graph, candidates_by_key, entries))
            candidate_issues.extend(self._structural_identity_conflicts(graph, entries))
            graph = candidate_service.merge_graph(graph, merge_map)
            if candidate_issues:
                graph = replace(graph, issues=(*graph.issues, *candidate_issues))
        override_payloads = _override_payloads or {}
        source_metadata = source_metadata or {}
        created_at = _now()

        if not _publish:
            with self.database.connect() as conn:
                # 同一来源只允许一份可操作草稿。旧扫描的草稿保留审计证据，
                # 但必须退出候选集，避免来源卡把新扫描和旧识别结果拼在一起。
                conn.execute(
                    """
                    UPDATE import_revisions
                    SET status = 'superseded'
                    WHERE root_id = ? AND scan_id != ? AND revision_id != ? AND status = 'draft'
                    """,
                    (root_id, scan_id, revision_id),
                )
                conn.execute(
                    """
                    INSERT INTO source_roots(
                        root_id, provider, ingest_method, source_locator, playback_locator,
                        route_id, display_name, root_container, source_mode, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(root_id) DO UPDATE SET
                        provider = excluded.provider,
                        ingest_method = excluded.ingest_method,
                        source_locator = CASE
                            WHEN excluded.source_locator != '' THEN excluded.source_locator
                            ELSE source_roots.source_locator
                        END,
                        playback_locator = CASE
                            WHEN excluded.playback_locator != '' THEN excluded.playback_locator
                            ELSE source_roots.playback_locator
                        END,
                        route_id = CASE
                            WHEN excluded.route_id != '' THEN excluded.route_id
                            ELSE source_roots.route_id
                        END,
                        display_name = CASE
                            WHEN excluded.display_name != '' THEN excluded.display_name
                            ELSE source_roots.display_name
                        END,
                        root_container = CASE
                            WHEN excluded.root_container != '' THEN excluded.root_container
                            ELSE source_roots.root_container
                        END,
                        source_mode = CASE
                            WHEN excluded.source_mode != '' THEN excluded.source_mode
                            ELSE source_roots.source_mode
                        END,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (
                        root_id,
                        source_provider,
                        ingest_method,
                        str(source_metadata.get("source_locator") or ""),
                        str(source_metadata.get("playback_locator") or ""),
                        str(source_metadata.get("route_id") or ""),
                        str(source_metadata.get("display_name") or ""),
                        str(source_metadata.get("root_container") or ""),
                        source_mode,
                        created_at,
                        created_at,
                    ),
                )
                existing_scan = conn.execute(
                    "SELECT scan_id FROM source_scans WHERE scan_id = ?",
                    (scan_id,),
                ).fetchone()
                if existing_scan is None:
                    generation = conn.execute(
                        "SELECT COALESCE(MAX(generation), 0) + 1 FROM source_scans WHERE root_id = ?",
                        (root_id,),
                    ).fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO source_scans(scan_id, root_id, generation, status)
                        VALUES (?, ?, ?, 'completed')
                        """,
                        (scan_id, root_id, generation),
                    )

        # Facts are immutable. Re-observing the same evidence is idempotent and
        # never replaces the previously parsed payload.
        if not _publish:
            if not _evidence_already_persisted:
                self.repository.save_scan_evidence_bulk([evidence for evidence, _facts in entries])
            if not _facts_already_persisted:
                self.repository.save_parsed_facts_bulk([facts for _evidence, facts in entries])

        graph_digest = self._graph_digest(graph)
        facts_by_evidence = {evidence.evidence_id: facts for evidence, facts in entries}
        episodes_by_evidence: dict[str, list] = {}
        for episode in graph.episodes:
            for evidence_id in episode.asset_evidence_ids:
                episodes_by_evidence.setdefault(evidence_id, []).append(episode)
        work_assets_by_evidence: dict[str, list] = {}
        for work_asset in graph.work_assets:
            for evidence_id in work_asset.asset_evidence_ids:
                work_assets_by_evidence.setdefault(evidence_id, []).append(work_asset)

        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if _publish:
                    from app.media_v4.persistence.identity_lifecycle import release_retired_source_identities

                    # 确认发布必须在同一写事务中重新读取事实、覆盖和冻结候选。
                    # confirm() 之前的预览只能作为提示，不能把旧快照直接写入权威表。
                    current_revision = conn.execute(
                        "SELECT status, root_id, scan_id FROM import_revisions "
                        "WHERE revision_id = ?",
                        (revision_id,),
                    ).fetchone()
                    if current_revision is None:
                        raise KeyError(revision_id)
                    if current_revision["status"] != "draft":
                        raise ValueError(f"revision 已经不是可重建草稿: {revision_id}")
                    entries = self._load_revision_entries(revision_id, conn=conn)
                    candidates_by_key = self._load_draft_candidates(revision_id, conn=conn)
                    current_graph = self.resolver.resolve(entries)
                    release_retired_source_identities(
                        conn, root_id, created_at,
                        work_keys={work.work_key for work in current_graph.works},
                        titles={
                            title for work in current_graph.works
                            for title in _work_identity_titles(work, [
                                entry for entry in entries
                                if entry[0].evidence_id in work.source_evidence_ids
                            ])
                        },
                        provider_identities={
                            (item.provider, item.media_type, item.provider_id)
                            for items in candidates_by_key.values() for item in items
                            if item.status == "confirmed"
                        },
                    )
                    graph, candidates_by_key = self._evaluate_draft(
                        entries,
                        conn=conn,
                        frozen_candidates=candidates_by_key,
                    )
                    if graph.issues:
                        raise RevisionBlockedError("revision 仍有 review issue，不能确认")
                    root_id = str(current_revision["root_id"] or root_id)
                    scan_id = str(current_revision["scan_id"] or scan_id)
                    override_payloads = self._load_override_payloads(revision_id, conn=conn)
                    graph_digest = self._graph_digest(graph)
                    facts_by_evidence = {
                        evidence.evidence_id: facts for evidence, facts in entries
                    }
                    episodes_by_evidence = {}
                    for episode in graph.episodes:
                        for evidence_id in episode.asset_evidence_ids:
                            episodes_by_evidence.setdefault(evidence_id, []).append(episode)
                    work_assets_by_evidence = {}
                    for work_asset in graph.work_assets:
                        for evidence_id in work_asset.asset_evidence_ids:
                            work_assets_by_evidence.setdefault(evidence_id, []).append(work_asset)
                existing_revision = conn.execute(
                    "SELECT status FROM import_revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if existing_revision is not None:
                    if existing_revision["status"] != "draft":
                        raise ValueError(f"revision 已经不是可重建草稿: {revision_id}")
                    conn.execute(
                        "DELETE FROM import_revisions WHERE revision_id = ?",
                        (revision_id,),
                    )
                conn.execute(
                    """
                    INSERT INTO import_revisions(
                        revision_id, root_id, scan_id, resolver_version, status,
                        graph_digest, created_at
                    ) VALUES (?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (revision_id, root_id, scan_id, resolver_version, graph_digest, created_at),
                )
                for evidence, facts in entries:
                    conn.execute(
                        """
                        INSERT INTO revision_evidence(revision_id, evidence_id, parsed_fact_id)
                        VALUES (?, ?, ?)
                        """,
                        (revision_id, evidence.evidence_id, facts.parsed_fact_id),
                    )
                for evidence_id, payload in override_payloads.items():
                    conn.execute(
                        """
                        INSERT INTO revision_overrides(
                            revision_id, evidence_id, overrides_json, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            evidence_id,
                            json.dumps(payload, ensure_ascii=False),
                            created_at,
                        ),
                    )

                if not _publish:
                    for index, issue in enumerate(graph.issues):
                        conn.execute(
                            """
                            INSERT INTO revision_issues(
                                revision_id, issue_id, code, evidence_id, message
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (
                                revision_id,
                                f"issue-{index}",
                                issue.code,
                                issue.evidence_id,
                                issue.message,
                            ),
                        )
                    _persist_candidates(conn, revision_id, {}, candidates_by_key, created_at)
                    conn.commit()
                    return graph

                # 来源结构绑定只用于跨 revision 复用。冻结快照后再处理本图，
                # 避免当前事务刚写入的目录绑定参与后续 Work 匹配。
                structural_binding_snapshot = _snapshot_structural_bindings(conn, root_id)
                work_ids: dict[str, str] = {}
                for work in graph.works:
                    related_entries = [
                        (evidence, facts)
                        for evidence, facts in entries
                        if evidence.evidence_id in work.source_evidence_ids
                    ]
                    work_type = "series" if work.media_type == "tv" else "movie"
                    identity_matches = _existing_work_matches(conn, work, related_entries)
                    if len(identity_matches) > 1:
                        raise RevisionBlockedError(
                            "同名作品已有多个身份归属，无法安全复用；请先合并或修正识别结果"
                        )
                    existing_work = identity_matches[0] if identity_matches else None

                    if existing_work is None:
                        # P-001 7.8 R6：确认事务按冻结候选 identity 复用已有 Work
                        # （provider+media_type+provider_id 在数据库层唯一归属一个 Work）。
                        frozen = [
                            item for item in candidates_by_key.get(work.work_key, [])
                            if item.status == "confirmed"
                            and candidate_service.supported_provider(item.provider)
                        ]
                        unique_frozen = {
                            (item.provider, item.media_type, item.provider_id) for item in frozen
                        }
                        if len(unique_frozen) == 1:
                            chosen = frozen[0]
                            owner_row = conn.execute(
                                """
                                SELECT w.work_id FROM provider_bindings pb
                                JOIN works w ON w.work_id = pb.work_id
                                WHERE pb.provider = ? AND pb.media_type = ? AND pb.provider_id = ?
                                  AND w.status = 'active'
                                LIMIT 1
                                """,
                                (chosen.provider, chosen.media_type, chosen.provider_id),
                            ).fetchone()
                            if owner_row is not None:
                                existing_work = conn.execute(
                                    "SELECT * FROM works WHERE work_id = ?",
                                    (owner_row["work_id"],),
                                ).fetchone()

                    if existing_work is None:
                        provider_candidates = {
                            (facts.tmdb_hint_type.casefold(), str(facts.tmdb_hint_id))
                            for _evidence, facts in related_entries
                            if facts.tmdb_hint_id and facts.tmdb_hint_type
                        }
                        provider_work_ids = {
                            str(row["work_id"])
                            for media_type, provider_id in provider_candidates
                            for row in [
                                conn.execute(
                                    """
                                    SELECT w.work_id
                                    FROM provider_bindings pb
                                    JOIN works w ON w.work_id = pb.work_id
                                    WHERE pb.provider = 'tmdb'
                                      AND pb.media_type = ? AND pb.provider_id = ?
                                      AND w.status = 'active'
                                    """,
                                    (media_type, provider_id),
                                ).fetchone()
                            ]
                            if row is not None
                        }
                        if len(provider_work_ids) == 1:
                            existing_work = conn.execute(
                                "SELECT * FROM works WHERE work_id = ?",
                                (next(iter(provider_work_ids)),),
                            ).fetchone()

                    if existing_work is None:
                        source_work_ids: set[str] = set()
                        for evidence, facts in related_entries:
                            structural_key = _structural_key(
                                evidence.relative_path,
                                facts=facts,
                                work_key=work.work_key,
                            )
                            if not structural_key:
                                continue
                            for binding in structural_binding_snapshot.get(structural_key, []):
                                if binding["work_type"] != work_type:
                                    continue
                                binding_year = binding["year"]
                                if (
                                    binding_year is not None
                                    and work.year is not None
                                    and int(binding_year) != int(work.year)
                                ):
                                    continue
                                source_work_ids.add(binding["work_id"])
                        if len(source_work_ids) > 1:
                            raise RevisionBlockedError(
                                "同一来源作品边界已对应多部作品，无法安全复用；请检查识别结果后重试"
                            )
                        if len(source_work_ids) == 1:
                            existing_work = conn.execute(
                                "SELECT * FROM works WHERE work_id = ?",
                                (next(iter(source_work_ids)),),
                            ).fetchone()

                    if existing_work is None:
                        alias_matches = _existing_work_matches(conn, work, related_entries)
                        if len(alias_matches) == 1:
                            existing_work = alias_matches[0]
                        elif len(alias_matches) > 1:
                            raise RevisionBlockedError(
                                "同名作品已有多个身份归属，无法安全复用；请先合并或修正识别结果"
                            )

                    if existing_work is None:
                        work_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO works(
                                work_id, identity_key, work_type, preferred_title,
                                year, show_type, card_type, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                work_id,
                                work.work_key,
                                work_type,
                                work.preferred_title,
                                work.year,
                                work.show_type,
                                work.card_type,
                                created_at,
                                created_at,
                            ),
                        )
                    else:
                        work_id = str(existing_work["work_id"])
                        preserve_preferred_title = (
                            _reuses_confirmed_provider_identity(
                                conn,
                                work_id,
                                candidates_by_key.get(work.work_key, []),
                            )
                            or _should_preserve_existing_title(
                                str(existing_work["preferred_title"] or ""),
                                work.preferred_title,
                            )
                        )
                        if existing_work["preferred_title"]:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO work_aliases(
                                    work_id, normalized_title, alias_type
                                ) VALUES (?, ?, 'previous_preferred')
                                """,
                                (work_id, _normalize_title(existing_work["preferred_title"])),
                            )
                        draft_title = _normalize_title(work.preferred_title)
                        if draft_title and draft_title != _normalize_title(existing_work["preferred_title"]):
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO work_aliases(
                                    work_id, normalized_title, alias_type
                                ) VALUES (?, ?, 'structural')
                                """,
                                (work_id, draft_title),
                            )
                        conn.execute(
                            """
                            UPDATE works
                            SET preferred_title = CASE
                                    WHEN ? OR ? = '' THEN preferred_title
                                    ELSE ?
                                END,
                                year = COALESCE(year, ?),
                                show_type = CASE WHEN show_type = '' THEN ? ELSE show_type END,
                                card_type = CASE WHEN card_type = '' THEN ? ELSE card_type END,
                                updated_at = ?
                            WHERE work_id = ?
                            """,
                            (
                                int(preserve_preferred_title),
                                work.preferred_title,
                                work.preferred_title,
                                work.year,
                                work.show_type,
                                work.card_type,
                                created_at,
                                work_id,
                            ),
                        )

                    work_ids[work.work_key] = work_id
                    for evidence, facts in related_entries:
                        structural_key = _structural_key(
                            evidence.relative_path,
                            facts=facts,
                            work_key=work.work_key,
                        )
                        if structural_key:
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO work_source_bindings(
                                    work_id, root_id, structural_key, confidence, binding_source
                                ) VALUES (?, ?, ?, ?, 'resolver')
                                """,
                                (work_id, root_id, structural_key, facts.confidence),
                            )
                    # 与候选识别使用同一边界：不能把特典单集名或父系列名
                    # 写成外传的永久别名，再反向污染后续导入。
                    for normalized_title in _work_identity_titles(work, related_entries):
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO work_aliases(
                                work_id, normalized_title, alias_type
                            ) VALUES (?, ?, 'observed')
                            """,
                            (work_id, normalized_title),
                        )

                # P-001 7.7 R4：持久化作品关系；父 Work 可能只存在于已确认数据库。
                for relation in graph.relations:
                    parent_work_id = work_ids.get(relation.parent_work_key)
                    child_work_id = work_ids.get(relation.child_work_key)
                    if not parent_work_id:
                        parent_work_id = _lookup_work_by_key(
                            conn,
                            relation.parent_work_key,
                            exclude_work_id=child_work_id or "",
                        )
                    if not parent_work_id or not child_work_id:
                        # 父 Work 不存在时保存明确待解析记录，不静默丢弃。
                        conn.execute(
                            """
                            INSERT INTO revision_issues(
                                revision_id, issue_id, code, evidence_id, message
                            ) VALUES (?, ?, 'unresolved_parent_relation', ?, ?)
                            """,
                            (
                                revision_id,
                                f"relation-{uuid.uuid4().hex}",
                                child_work_id or "",
                                f"父系列 {relation.parent_work_key} 尚未导入，无法建立作品关系",
                            ),
                        )
                        continue
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO work_relations(
                            relation_id, parent_work_id, child_work_id, relation_type
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            parent_work_id,
                            child_work_id,
                            relation.relation_type,
                        ),
                    )

                # P-001 7.7 R1：确认时冻结候选身份（work_id 落库），并把高置信
                # 唯一身份写入 provider_bindings，使确认后的 scrape 不再搜索。
                _persist_candidates(conn, revision_id, work_ids, candidates_by_key, created_at)
                _freeze_candidate_bindings(conn, work_ids, candidates_by_key, created_at)

                # Provider identity is a mapping, never a replacement for the
                # local Work identity.  Hints observed during parsing are
                # attached here without changing local season/episode numbers.
                for evidence, facts in entries:
                    if not facts.tmdb_hint_id or not facts.tmdb_hint_type:
                        continue
                    episodes = episodes_by_evidence.get(evidence.evidence_id, [])
                    episode_candidate = episodes[0] if episodes else None
                    work_key = episode_candidate.work_key if episode_candidate is not None else next(
                        (work.work_key for work in graph.works if evidence.evidence_id in work.source_evidence_ids),
                        "",
                    )
                    if not work_key:
                        continue
                    work_id = work_ids[work_key]
                    provider_id = str(facts.tmdb_hint_id)
                    from app.media_v4.persistence.identity_lifecycle import (
                        release_inactive_provider_identity,
                    )

                    release_inactive_provider_identity(
                        conn, "tmdb", facts.tmdb_hint_type, provider_id
                    )
                    identity_owner = conn.execute(
                        """
                        SELECT work_id FROM provider_bindings
                        WHERE provider = 'tmdb' AND media_type = ? AND provider_id = ?
                        """,
                        (facts.tmdb_hint_type, provider_id),
                    ).fetchone()
                    work_binding = conn.execute(
                        """
                        SELECT provider_id FROM provider_bindings
                        WHERE work_id = ? AND provider = 'tmdb' AND media_type = ?
                        """,
                        (work_id, facts.tmdb_hint_type),
                    ).fetchone()
                    if identity_owner is not None and identity_owner["work_id"] != work_id:
                        raise RevisionBlockedError("TMDB 身份已经属于另一个作品，拒绝静默合并")
                    if work_binding is not None and work_binding["provider_id"] != provider_id:
                        raise RevisionBlockedError("作品已经绑定另一个 TMDB 身份，拒绝静默覆盖")
                    conn.execute(
                        """
                        INSERT INTO provider_bindings(
                            work_id, provider, media_type, provider_id
                        ) VALUES (?, 'tmdb', ?, ?)
                        ON CONFLICT(work_id, provider, media_type) DO NOTHING
                        """,
                        (
                            work_id,
                            facts.tmdb_hint_type,
                            provider_id,
                        ),
                    )

                episode_ids: dict[str, str] = {}
                season_ids: dict[tuple[str, int | None, str], str] = {}
                edition_ids: dict[tuple[str, str], str | None] = {}
                asset_ids: dict[tuple[str, str], str] = {}

                def ensure_asset(evidence_id: str) -> str:
                    evidence_row = conn.execute(
                        "SELECT * FROM source_evidence WHERE evidence_id = ?",
                        (evidence_id,),
                    ).fetchone()
                    if evidence_row is None:
                        raise ValueError(f"缺少 Asset 来源事实: {evidence_id}")
                    asset_row = conn.execute(
                        "SELECT asset_id FROM assets WHERE evidence_id = ?",
                        (evidence_id,),
                    ).fetchone()
                    if asset_row is not None:
                        return str(asset_row["asset_id"])
                    facts = facts_by_evidence[evidence_id]
                    asset_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO assets(
                            asset_id, evidence_id, root_id, source_locator,
                            playback_locator, fingerprint, size, mtime,
                            resolution, release_group, version_tags_json,
                            availability_state
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            asset_id,
                            evidence_id,
                            evidence_row["root_id"],
                            evidence_row["source_locator"],
                            evidence_row["playback_locator"],
                            evidence_row["fingerprint"],
                            evidence_row["size"],
                            evidence_row["mtime"],
                            facts.quality_tags[0] if facts.quality_tags else "",
                            facts.release_group,
                            json.dumps(facts.quality_tags, ensure_ascii=False),
                            "available" if evidence_row["presence_state"] == "present" else "missing",
                        ),
                    )
                    return asset_id

                for episode in graph.episodes:
                    work_id = work_ids[episode.work_key]
                    season_key = (episode.work_key, episode.local_season_number, episode.season_kind)
                    season_id = season_ids.get(season_key)
                    if season_id is None:
                        season_row = conn.execute(
                            """
                            SELECT season_id FROM seasons
                            WHERE work_id = ? AND local_season_number = ? AND season_kind = ?
                            """,
                            (work_id, episode.local_season_number or 0, episode.season_kind),
                        ).fetchone()
                        if season_row is None:
                            season_id = str(uuid.uuid4())
                            conn.execute(
                                """
                                INSERT INTO seasons(season_id, work_id, local_season_number, season_kind)
                                VALUES (?, ?, ?, ?)
                                """,
                                (season_id, work_id, episode.local_season_number or 0, episode.season_kind),
                            )
                        else:
                            season_id = str(season_row["season_id"])
                        season_ids[season_key] = season_id

                    episode_row = conn.execute(
                        """
                        SELECT episode_id FROM episodes
                        WHERE work_id = ? AND season_id = ?
                          AND local_episode_number IS ?
                          AND special_number IS ?
                          AND episode_kind = ?
                        """,
                        (
                            work_id,
                            season_id,
                            episode.local_episode_number,
                            episode.special_number,
                            episode.episode_kind,
                        ),
                    ).fetchone()
                    if episode_row is None:
                        episode_id = str(uuid.uuid4())
                        conn.execute(
                            """
                            INSERT INTO episodes(
                                episode_id, work_id, season_id, local_episode_number,
                                absolute_episode_number, special_number, episode_kind,
                                display_title
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                episode_id,
                                work_id,
                                season_id,
                                episode.local_episode_number,
                                episode.absolute_episode_number,
                                episode.special_number,
                                episode.episode_kind,
                                episode.display_title,
                            ),
                        )
                    else:
                        episode_id = str(episode_row["episode_id"])
                    episode_ids[episode.episode_key] = episode_id

                    edition_id = None
                    if episode.edition_key != "default":
                        edition_row = conn.execute(
                            "SELECT edition_id FROM editions WHERE episode_id = ? AND edition_key = ?",
                            (episode_id, episode.edition_key),
                        ).fetchone()
                        if edition_row is None:
                            edition_id = str(uuid.uuid4())
                            conn.execute(
                                """
                                INSERT INTO editions(edition_id, episode_id, edition_key, display_name)
                                VALUES (?, ?, ?, ?)
                                """,
                                (edition_id, episode_id, episode.edition_key, episode.edition_key),
                            )
                        else:
                            edition_id = str(edition_row["edition_id"])
                    edition_ids[(episode.episode_key, episode.edition_key)] = edition_id

                    for evidence_id in episode.asset_evidence_ids:
                        asset_id = ensure_asset(evidence_id)
                        asset_ids[(episode.episode_key, evidence_id)] = asset_id
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO episode_assets(episode_id, edition_id, asset_id)
                            VALUES (?, ?, ?)
                            """,
                            (episode_id, edition_id, asset_id),
                        )

                work_edition_ids: dict[tuple[str, str], str | None] = {}
                work_asset_ids: dict[tuple[str, str, str], str] = {}
                for work_asset in graph.work_assets:
                    work_id = work_ids[work_asset.work_key]
                    edition_id = None
                    if work_asset.edition_key != "default":
                        edition_row = conn.execute(
                            "SELECT edition_id FROM editions WHERE work_id = ? AND edition_key = ?",
                            (work_id, work_asset.edition_key),
                        ).fetchone()
                        if edition_row is None:
                            edition_id = str(uuid.uuid4())
                            conn.execute(
                                """
                                INSERT INTO editions(edition_id, work_id, edition_key, display_name)
                                VALUES (?, ?, ?, ?)
                                """,
                                (edition_id, work_id, work_asset.edition_key, work_asset.edition_key),
                            )
                        else:
                            edition_id = str(edition_row["edition_id"])
                    work_edition_ids[(work_asset.work_key, work_asset.edition_key)] = edition_id
                    for evidence_id in work_asset.asset_evidence_ids:
                        asset_id = ensure_asset(evidence_id)
                        work_asset_ids[(work_asset.work_key, work_asset.edition_key, evidence_id)] = asset_id
                        conn.execute(
                            "INSERT OR IGNORE INTO work_assets(work_id, edition_id, asset_id) VALUES (?, ?, ?)",
                            (work_id, edition_id, asset_id),
                        )

                for evidence, _facts in entries:
                    bound_episodes = episodes_by_evidence.get(evidence.evidence_id, [])
                    facts = facts_by_evidence[evidence.evidence_id]
                    for episode in bound_episodes:
                        conn.execute(
                            """
                            INSERT INTO revision_bindings(
                                binding_id, revision_id, evidence_id, work_id, season_id,
                                episode_id, edition_id, asset_id, confidence, decision_source,
                                reasons_json, override_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                revision_id,
                                evidence.evidence_id,
                                work_ids[episode.work_key],
                                season_ids[(episode.work_key, episode.local_season_number, episode.season_kind)],
                                episode_ids[episode.episode_key],
                                edition_ids[(episode.episode_key, episode.edition_key)],
                                asset_ids[(episode.episode_key, evidence.evidence_id)],
                                facts.confidence,
                                "manual_override" if evidence.evidence_id in override_payloads else "resolver",
                                json.dumps(facts.reasons, ensure_ascii=False),
                                json.dumps(override_payloads.get(evidence.evidence_id, {}), ensure_ascii=False),
                            ),
                        )
                    for work_asset in work_assets_by_evidence.get(evidence.evidence_id, []):
                        conn.execute(
                            """
                            INSERT INTO revision_bindings(
                                binding_id, revision_id, evidence_id, work_id,
                                edition_id, asset_id, confidence, decision_source,
                                reasons_json, override_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                revision_id,
                                evidence.evidence_id,
                                work_ids[work_asset.work_key],
                                work_edition_ids[(work_asset.work_key, work_asset.edition_key)],
                                work_asset_ids[(work_asset.work_key, work_asset.edition_key, evidence.evidence_id)],
                                facts.confidence,
                                "manual_override" if evidence.evidence_id in override_payloads else "resolver",
                                json.dumps(facts.reasons, ensure_ascii=False),
                                json.dumps(override_payloads.get(evidence.evidence_id, {}), ensure_ascii=False),
                            ),
                        )

                for index, issue in enumerate(graph.issues):
                    conn.execute(
                        """
                        INSERT INTO revision_issues(revision_id, issue_id, code, evidence_id, message)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (revision_id, f"issue-{index}", issue.code, issue.evidence_id, issue.message),
                    )
                conn.execute(
                    """
                    UPDATE import_revisions
                    SET status = 'superseded'
                    WHERE root_id = ? AND revision_id != ? AND status = 'confirmed'
                    """,
                    (root_id, revision_id),
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled', cancel_requested = 1, last_error = '',
                        heartbeat_at = ?, finished_at = ?, updated_at = ?
                    WHERE status = 'queued' AND revision_id IN (
                        SELECT revision_id FROM import_revisions
                        WHERE root_id = ? AND status = 'superseded'
                    )
                    """,
                    (created_at, created_at, created_at, root_id),
                )
                conn.execute(
                    """
                    UPDATE jobs
                    SET cancel_requested = 1, heartbeat_at = ?, updated_at = ?
                    WHERE status = 'running' AND revision_id IN (
                        SELECT revision_id FROM import_revisions
                        WHERE root_id = ? AND status = 'superseded'
                    )
                    """,
                    (created_at, created_at, root_id),
                )
                conn.execute(
                    "UPDATE import_revisions SET status = 'confirmed', confirmed_at = ? WHERE revision_id = ?",
                    (created_at, revision_id),
                )
                # 确认即代表该来源重新进入媒体库。来源卡必须在任务开始时就可见，
                # 不能等镜像、刮削和最终投影全部完成后才恢复。
                conn.execute(
                    """
                    UPDATE source_roots
                    SET retired_at = '', retired_reason = '', enabled = 1, updated_at = ?
                    WHERE root_id = ?
                    """,
                    (created_at, root_id),
                )
                conn.execute(
                    """
                    INSERT INTO v4_meta(key, value) VALUES ('library_projection_dirty', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (created_at,),
                )
                self._enqueue_execution_jobs(conn, revision_id, set(work_ids.values()), created_at)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return graph

    def get_status(self, revision_id: str) -> str:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
        if row is None:
            raise KeyError(revision_id)
        return row["status"]

    def load_draft_graph(self, revision_id: str, *, refresh_snapshot: bool = True) -> ResolvedMediaGraph:
        """从已持久化事实重建草稿，并刷新可再生的候选与问题快照。

        草稿中的 ``revision_issues``/候选是派生状态。识别规则升级后，若只
        在内存中重算，导入页虽然已无问题，媒体管理来源卡仍会读取旧表而持续
        显示红色冲突；因此预览成功后必须把当前离线评估结果回写。
        来源卡使用只读模式统计当前草稿，不在轮询期间修改媒体状态。
        """

        with self.database.connect() as conn:
            # 状态检查、评估和回写必须共享事务，防止并发确认后覆盖已发布快照。
            conn.execute("BEGIN IMMEDIATE" if refresh_snapshot else "BEGIN")
            revision = conn.execute(
                "SELECT status FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            if revision["status"] != "draft":
                raise RuntimeError("只有 draft revision 可以读取识别预览")
            entries = self._load_revision_entries(revision_id, conn=conn)
            candidates_by_key = self._load_draft_candidates(revision_id, conn=conn)
            graph, refreshed_candidates = self._evaluate_draft(
                entries,
                conn=conn,
                frozen_candidates=candidates_by_key,
            )
            if not refresh_snapshot:
                return graph
            try:
                _persist_candidates(conn, revision_id, {}, refreshed_candidates, _now())
                conn.execute("DELETE FROM revision_issues WHERE revision_id = ?", (revision_id,))
                for index, issue in enumerate(graph.issues):
                    conn.execute(
                        """
                        INSERT INTO revision_issues(revision_id, issue_id, code, evidence_id, message)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (revision_id, f"issue-{index}", issue.code, issue.evidence_id, issue.message),
                    )
                conn.execute(
                    "UPDATE import_revisions SET graph_digest = ? WHERE revision_id = ?",
                    (self._graph_digest(graph), revision_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return graph

    def apply_override(
        self,
        revision_id: str,
        evidence_id: str,
        changes: dict,
    ) -> ResolvedMediaGraph:
        unknown = set(changes) - self._OVERRIDE_FIELDS
        if unknown:
            raise ValueError("不允许修正字段: " + ", ".join(sorted(unknown)))
        if not changes:
            raise ValueError("人工修正不能为空")
        normalized = self._normalize_override(changes)
        with self.database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                revision = conn.execute(
                    "SELECT status FROM import_revisions WHERE revision_id = ?",
                    (revision_id,),
                ).fetchone()
                if revision is None:
                    raise KeyError(revision_id)
                if revision["status"] != "draft":
                    raise RuntimeError("只有 draft revision 可以人工修正")
                member = conn.execute(
                    "SELECT 1 FROM revision_evidence WHERE revision_id = ? AND evidence_id = ?",
                    (revision_id, evidence_id),
                ).fetchone()
                if member is None:
                    raise KeyError(evidence_id)
                existing = conn.execute(
                    "SELECT overrides_json FROM revision_overrides "
                    "WHERE revision_id = ? AND evidence_id = ?",
                    (revision_id, evidence_id),
                ).fetchone()
                merged = (
                    json.loads(existing["overrides_json"] or "{}")
                    if existing is not None
                    else {}
                )
                merged.update(normalized)
                conn.execute(
                    """
                    INSERT INTO revision_overrides(
                        revision_id, evidence_id, overrides_json, created_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(revision_id, evidence_id) DO UPDATE SET
                        overrides_json = excluded.overrides_json
                    """,
                    (revision_id, evidence_id, json.dumps(merged, ensure_ascii=False), _now()),
                )

                entries = self._load_revision_entries(revision_id, conn=conn)
                frozen_candidates = self._load_draft_candidates(revision_id, conn=conn)
                graph, _candidates = self._evaluate_draft(
                    entries,
                    conn=conn,
                    frozen_candidates=frozen_candidates,
                )
                conn.execute("DELETE FROM revision_issues WHERE revision_id = ?", (revision_id,))
                for index, issue in enumerate(graph.issues):
                    conn.execute(
                        """
                        INSERT INTO revision_issues(revision_id, issue_id, code, evidence_id, message)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (revision_id, f"issue-{index}", issue.code, issue.evidence_id, issue.message),
                    )
                conn.execute(
                    "UPDATE import_revisions SET graph_digest = ? WHERE revision_id = ?",
                    (self._graph_digest(graph), revision_id),
                )
                conn.commit()
                return graph
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _normalize_override(changes: dict) -> dict:
        normalized = dict(changes)
        text_fields = {
            "work_title",
            "original_title",
            "series_group",
            "media_type",
            "group_type",
            "tmdb_hint_type",
        }
        for key in text_fields & normalized.keys():
            if not isinstance(normalized[key], str):
                raise ValueError(f"人工修正字段 {key} 必须是字符串")
            normalized[key] = normalized[key].strip()
        if normalized.get("media_type") not in {None, "", "tv", "movie"}:
            raise ValueError("media_type 只能是 tv 或 movie")
        if normalized.get("tmdb_hint_type") not in {None, "", "tv", "movie"}:
            raise ValueError("tmdb_hint_type 只能是 tv 或 movie")
        if normalized.get("group_type") not in {
            None,
            "",
            "season",
            "special",
            "movie",
            "unknown",
        }:
            raise ValueError("group_type 不是允许的媒体分组")
        for key in ("title_candidates", "edition_tags"):
            if key not in normalized:
                continue
            value = normalized[key]
            if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"人工修正字段 {key} 必须是字符串数组")
            normalized[key] = [item.strip() for item in value if item.strip()]
        numeric_fields = {
            "year_candidate",
            "season_candidate",
            "episode_candidate",
            "absolute_episode_candidate",
            "special_number",
            "tmdb_hint_id",
        }
        for key in numeric_fields & normalized.keys():
            value = normalized[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"人工修正字段 {key} 必须是整数或 null")
            if value is not None and value < 0:
                raise ValueError(f"人工修正字段 {key} 不能为负数")
        year = normalized.get("year_candidate")
        if year is not None and not 1800 <= year <= 2200:
            raise ValueError("year_candidate 超出允许范围")
        tmdb_id = normalized.get("tmdb_hint_id")
        if tmdb_id is not None and tmdb_id <= 0:
            raise ValueError("tmdb_hint_id 必须大于 0")
        for key in ("special_candidate", "needs_review", "is_importable", "is_auxiliary"):
            if key in normalized and not isinstance(normalized[key], bool):
                raise ValueError(f"人工修正字段 {key} 必须是布尔值")
        return normalized

    def _load_revision_entries(self, revision_id: str, conn=None) -> list[tuple[SourceEvidence, ParsedFacts]]:
        with (nullcontext(conn) if conn is not None else self.database.connect()) as connection:
            rows = connection.execute(
                """
                SELECT re.evidence_id, re.parsed_fact_id,
                       COALESCE(ro.overrides_json, '{}') AS overrides_json
                FROM revision_evidence re
                LEFT JOIN revision_overrides ro
                  ON ro.revision_id = re.revision_id AND ro.evidence_id = re.evidence_id
                WHERE re.revision_id = ?
                ORDER BY re.evidence_id
                """,
                (revision_id,),
            ).fetchall()
            entries = []
            tuple_fields = {"title_candidates", "edition_tags"}
            # 批量读取：逐条 get_source_evidence/get_parsed_facts 会让 revision 级评估
            # 退化成 N+1（3 万条证据 ≈ 6 万次单条 SELECT），而来源卡在有活动任务时
            # 每 1.5 秒就要做一次只读评估。对象与单条读取完全一致。
            evidence_by_id = self.repository.get_source_evidence_bulk(
                [row["evidence_id"] for row in rows], conn=connection
            )
            facts_by_id = self.repository.get_parsed_facts_bulk(
                [row["parsed_fact_id"] for row in rows], conn=connection
            )
            for row in rows:
                evidence = evidence_by_id[str(row["evidence_id"])]
                facts = facts_by_id[str(row["parsed_fact_id"])]
                overrides = json.loads(row["overrides_json"] or "{}")
                for key in tuple_fields:
                    if key in overrides:
                        overrides[key] = tuple(overrides[key] or ())
                entries.append((evidence, replace(facts, **overrides)))
        return entries

    def _load_override_payloads(self, revision_id: str, conn=None) -> dict[str, dict]:
        with (nullcontext(conn) if conn is not None else self.database.connect()) as conn:
            rows = conn.execute(
                "SELECT evidence_id, overrides_json FROM revision_overrides WHERE revision_id = ?",
                (revision_id,),
            ).fetchall()
        return {
            str(row["evidence_id"]): json.loads(row["overrides_json"] or "{}")
            for row in rows
        }

    def confirm(self, revision_id: str) -> None:
        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT status, root_id, scan_id FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            if revision["status"] == "confirmed":
                return
            if revision["status"] != "draft":
                raise RuntimeError(f"revision 状态不可确认: {revision['status']}")
        self.create_draft(
            revision_id,
            [],
            root_id=str(revision["root_id"]),
            scan_id=str(revision["scan_id"]),
            _publish=True,
        )

    @staticmethod
    def _enqueue_execution_jobs(conn, revision_id: str, work_ids: set[str], now: str) -> None:
        for work_id in sorted(work_ids):
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_id, job_type, revision_id, work_id, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, 'materialize_mirror', ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    work_id,
                    f"materialize_mirror:{revision_id}:{work_id}",
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_id, job_type, revision_id, work_id, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, 'scrape_work', ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    work_id,
                    f"scrape_work:{revision_id}:{work_id}",
                    now,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT OR IGNORE INTO jobs(
                job_id, job_type, revision_id, idempotency_key, created_at, updated_at
            ) VALUES (?, 'refresh_projection', ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), revision_id, f"refresh_projection:{revision_id}", now, now),
        )
        root_id = conn.execute(
            "SELECT root_id FROM import_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()[0]
        has_superseded = conn.execute(
            "SELECT 1 FROM import_revisions WHERE root_id = ? AND status = 'superseded' LIMIT 1",
            (root_id,),
        ).fetchone()
        if has_superseded is not None:
            conn.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    job_id, job_type, revision_id, idempotency_key, created_at, updated_at
                ) VALUES (?, 'cleanup_superseded_artifacts', ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    revision_id,
                    f"cleanup_superseded_artifacts:{revision_id}",
                    now,
                    now,
                ),
            )

    def list_jobs(self, revision_id: str) -> list[dict]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE revision_id = ? ORDER BY job_type, job_id",
                (revision_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_work_execution_detail(
        self, revision_id: str, work_id: str, *, episode_offset: int = 0, episode_limit: int = 50
    ) -> dict:
        """3.1 P0：作品级执行详情只读投影。

        从 revision_bindings / seasons / episodes / episode_provider_mappings /
        episode_assets + assets / artifacts / scrape_bindings / jobs 组装，
        不重新刮削、不重新扫描、不访问源盘、不写库。work 不属于该 revision
        或不存在时抛 KeyError（路由层转 404）。
        """

        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT revision_id FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            belongs = conn.execute(
                "SELECT 1 FROM revision_bindings WHERE revision_id = ? AND work_id = ? LIMIT 1",
                (revision_id, work_id),
            ).fetchone()
            job_link = conn.execute(
                "SELECT 1 FROM jobs WHERE revision_id = ? AND work_id = ? LIMIT 1",
                (revision_id, work_id),
            ).fetchone()
            if belongs is None and job_link is None:
                raise KeyError(work_id)
            work = conn.execute(
                "SELECT work_id, preferred_title, work_type FROM works WHERE work_id = ?",
                (work_id,),
            ).fetchone()
            if work is None:
                raise KeyError(work_id)

            job_rows = conn.execute(
                "SELECT job_id, job_type, status, attempts, last_error, finished_at "
                "FROM jobs WHERE revision_id = ? AND work_id = ? "
                "ORDER BY job_type, updated_at DESC, job_id",
                (revision_id, work_id),
            ).fetchall()
            artifact_rows = conn.execute(
                "SELECT target_path, status FROM artifacts "
                "WHERE revision_id = ? AND work_id = ? AND artifact_type = 'mirror' "
                "ORDER BY target_path LIMIT 20",
                (revision_id, work_id),
            ).fetchall()
            artifact_total = int(conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE revision_id = ? AND work_id = ? AND artifact_type = 'mirror'",
                (revision_id, work_id),
            ).fetchone()[0])
            scrape_row = conn.execute(
                "SELECT provider, provider_id, status, metadata_json FROM scrape_bindings "
                "WHERE revision_id = ? AND work_id = ? "
                "ORDER BY updated_at DESC, binding_id DESC LIMIT 1",
                (revision_id, work_id),
            ).fetchone()
            season_rows = conn.execute(
                "SELECT s.season_id, s.local_season_number, s.season_kind, s.title FROM seasons s "
                "WHERE s.work_id = ? AND EXISTS ("
                "SELECT 1 FROM revision_bindings rb "
                "WHERE rb.revision_id = ? AND rb.work_id = ? AND rb.season_id = s.season_id"
                ") ORDER BY CASE WHEN s.season_kind = 'special' THEN 1 ELSE 0 END, s.local_season_number, s.season_id",
                (work_id, revision_id, work_id),
            ).fetchall()
            episode_total = int(conn.execute(
                "SELECT COUNT(*) FROM episodes e WHERE e.work_id = ? AND EXISTS ("
                "SELECT 1 FROM revision_bindings rb WHERE rb.revision_id = ? "
                "AND rb.work_id = ? AND rb.episode_id = e.episode_id)",
                (work_id, revision_id, work_id),
            ).fetchone()[0])
            season_count_rows = conn.execute(
                """
                SELECT s.season_id, COUNT(DISTINCT rb.episode_id) AS episode_count
                FROM seasons s
                LEFT JOIN revision_bindings rb
                  ON rb.revision_id = ? AND rb.work_id = ? AND rb.season_id = s.season_id
                WHERE s.work_id = ? AND EXISTS (
                  SELECT 1 FROM revision_bindings season_binding
                  WHERE season_binding.revision_id = ?
                    AND season_binding.work_id = ?
                    AND season_binding.season_id = s.season_id
                )
                GROUP BY s.season_id
                """,
                (revision_id, work_id, work_id, revision_id, work_id),
            ).fetchall()
            episode_rows = conn.execute(
                """
                SELECT e.episode_id, e.season_id, e.local_episode_number, e.special_number,
                       e.episode_kind, e.display_title,
                       s.local_season_number AS season_number, s.season_kind
                FROM episodes e
                JOIN seasons s ON s.season_id = e.season_id
                WHERE e.work_id = ? AND EXISTS (
                  SELECT 1 FROM revision_bindings rb
                  WHERE rb.revision_id = ? AND rb.work_id = ? AND rb.episode_id = e.episode_id
                )
                ORDER BY CASE WHEN s.season_kind = 'special' THEN 1 ELSE 0 END,
                         s.local_season_number, e.local_episode_number, e.special_number, e.episode_id
                LIMIT ? OFFSET ?
                """,
                (work_id, revision_id, work_id, episode_limit, episode_offset),
            ).fetchall()
            asset_rows = conn.execute(
                """
                SELECT ea.episode_id, a.source_locator, a.playback_locator
                FROM episode_assets ea
                JOIN assets a ON a.asset_id = ea.asset_id
                JOIN revision_bindings rb ON rb.asset_id = a.asset_id
                WHERE rb.revision_id = ? AND rb.work_id = ?
                ORDER BY ea.episode_id, ea.preference_rank
                """,
                (revision_id, work_id),
            ).fetchall()

        # 刮削详情来自最近一次 scrape_bindings.metadata_json 的白名单字段。
        # 这里是只读投影：不能把完整 metadata_json 直接返回给前端，避免把
        # 本地路径、产物路径或未来新增的内部诊断字段泄漏到执行页。
        metadata: dict = {}
        mappings_by_episode: dict[str, dict] = {}
        metadata_state = ""
        metadata_reason_code = ""
        artifact_state = ""
        artifact_reasons: list[str] = []
        provider = ""
        provider_id = ""
        if scrape_row is not None:
            provider = str(scrape_row["provider"] or "")
            provider_id = str(scrape_row["provider_id"] or "")
            metadata_state = str(scrape_row["status"] or "")
            try:
                decoded_metadata = json.loads(str(scrape_row["metadata_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded_metadata = {}
            if isinstance(decoded_metadata, dict):
                metadata = decoded_metadata
            if isinstance(metadata, dict):
                # 绑定行的 status 是 confirmed 等订阅状态；面向用户的元数据
                # 状态（ready/waiting_review/...）保存在 metadata_json 内。
                metadata_state = str(metadata.get("metadata_state") or metadata_state)
                metadata_reason_code = str(metadata.get("reason_code") or "")
                metadata_state, artifact_state, artifact_reasons = _artifact_view(metadata)
                for mapping in metadata.get("episode_mappings") or []:
                    if isinstance(mapping, dict) and mapping.get("episode_id"):
                        mappings_by_episode.setdefault(str(mapping["episode_id"]), mapping)

        file_by_episode: dict[str, dict] = {}
        for row in asset_rows:
            episode_id = str(row["episode_id"])
            if episode_id in file_by_episode:
                continue
            locator = str(row["source_locator"] or "")
            file_by_episode[episode_id] = {
                "file_name": locator.replace("\\", "/").rsplit("/", 1)[-1] if locator else "",
                "playback_ready": bool(str(row["playback_locator"] or "").strip()),
            }

        season_episode_counts = {
            str(row["season_id"]): int(row["episode_count"] or 0)
            for row in season_count_rows
        }
        episodes: list[dict] = []
        for row in episode_rows:
            episode_id = str(row["episode_id"])
            season_number = int(row["season_number"] or 0)
            season_kind = str(row["season_kind"] or "")
            mapping = mappings_by_episode.get(episode_id, {})
            asset_fact = file_by_episode.get(episode_id, {"file_name": "", "playback_ready": False})
            special_number = int(row["special_number"] or 0) if row["special_number"] is not None else None
            mapped = bool(str(mapping.get("provider_episode_id") or "").strip())
            episodes.append({
                "episode_id": episode_id,
                "season_number": season_number,
                "season_kind": season_kind,
                "episode_number": int(row["local_episode_number"] or 0) if row["local_episode_number"] is not None else special_number,
                "display_title": str(row["display_title"] or ""),
                "scraped_title": str(mapping.get("title") or ""),
                "scraped_plot": str(mapping.get("plot") or ""),
                # 执行详情只能用本 revision 的 metadata 快照，不能把后续导入
                # 写入的全局 episode_provider_mappings 反投影回来。
                "provider_episode_number": _positive_detail_int(mapping.get("provider_episode_number")),
                "provider_episode_id": str(mapping.get("provider_episode_id") or ""),
                "runtime": _positive_detail_int(mapping.get("runtime")),
                "still_url": str(mapping.get("still_url") or ""),
                "mapped": mapped,
                "file_name": asset_fact["file_name"],
                "playback_ready": asset_fact["playback_ready"],
                "playback_locator_available": asset_fact["playback_ready"],
            })

        mirror_job = next((dict(row) for row in job_rows if row["job_type"] == "materialize_mirror"), None)
        metadata_job = next((dict(row) for row in job_rows if row["job_type"] == "scrape_work"), None)

        mirror_status = str(mirror_job["status"]) if mirror_job else ""
        artifacts = [
            {"file_name": str(row["target_path"]).replace("\\", "/").rsplit("/", 1)[-1], "status": str(row["status"] or "")}
            for row in artifact_rows
        ]
        candidate_decision = _safe_candidate_decision(metadata.get("candidate_decision"))
        metadata_policy = metadata_recovery_policy(
            metadata,
            binding_status=str(scrape_row["status"] or "") if scrape_row is not None else "",
        )
        scrape_summary = {
            "metadata_state": metadata_state,
            "metadata_reason": metadata_policy["reason"],
            "metadata_reason_code": metadata_reason_code,
            "artifact_state": artifact_state,
            "artifact_reasons": artifact_reasons,
            "metadata_warning": _safe_detail_text(metadata.get("metadata_warning")),
            "metadata_recovery_action": metadata_policy["action"],
            "metadata_recovery_hint": metadata_policy["hint"],
            "title": _safe_detail_text(metadata.get("title")),
            "original_title": _safe_detail_text(metadata.get("original_title")),
            "year": _positive_detail_int(metadata.get("year")),
            "plot": _safe_detail_text(metadata.get("plot")),
            "rating": _safe_detail_float(metadata.get("rating")),
            "runtime": _positive_detail_int(metadata.get("runtime")),
            "genres": _safe_detail_text_list(metadata.get("genres")),
            "studios": _safe_detail_text_list(metadata.get("studios")),
            "premiered": _safe_detail_text(metadata.get("premiered")),
            "candidate_decision": candidate_decision,
            "identity_status": _safe_detail_text(metadata.get("identity_status")),
            "work_metadata_status": _safe_detail_text(metadata.get("work_metadata_status")),
            "episode_mapping_status": _safe_detail_text(metadata.get("episode_mapping_status")),
            "failure_stage": _safe_detail_text(metadata.get("failure_stage")),
            "retryable": bool(metadata.get("retryable")),
            "season_results": _safe_season_results(metadata.get("season_results")),
        }
        has_detail = bool(
            episodes
            or artifacts
            or season_rows
            or bool(metadata.get("season_results"))
            or metadata_state in {"confirmed", "ready"}
            or candidate_decision is not None
            or any(scrape_summary[key] for key in ("title", "original_title", "plot", "year", "rating", "runtime", "genres", "studios", "premiered"))
        )

        return {
            "revision_id": revision_id,
            "work_id": work_id,
            "work": {
                "title": str(work["preferred_title"] or ""),
                "media_type": "movie" if str(work["work_type"] or "") == "movie" else "tv",
                "provider": provider,
                "provider_id": provider_id,
                "metadata_state": metadata_state,
                "metadata_reason": metadata_policy["reason"],
                "metadata_reason_code": metadata_reason_code,
                "artifact_state": artifact_state,
                "artifact_reasons": artifact_reasons,
                "metadata_warning": _safe_detail_text(metadata.get("metadata_warning")),
                "metadata_recovery_action": metadata_policy["action"],
                "metadata_recovery_hint": metadata_policy["hint"],
            },
            "mirror": {
                "status": mirror_status,
                "error": _friendly_job_error(str((mirror_job or {}).get("last_error") or "")),
                "artifact_count": artifact_total,
                "artifacts": artifacts,
            },
            "metadata_job_status": str(metadata_job["status"]) if metadata_job else "",
            "scrape": scrape_summary,
            "seasons": [
                {
                    "season_number": int(row["local_season_number"] or 0),
                    "season_kind": str(row["season_kind"] or ""),
                    "title": str(row["title"] or ""),
                    "episode_count": season_episode_counts.get(str(row["season_id"]), 0),
                }
                for row in season_rows
            ],
            "episodes": episodes,
            "episode_total": episode_total,
            "episodes_truncated": episode_offset + len(episodes) < episode_total,
            "next_episode_offset": episode_offset + len(episodes) if episode_offset + len(episodes) < episode_total else None,
            "has_detail": has_detail,
        }

    def get_execution_progress(self, revision_id: str) -> dict:
        """P-003：V4 只读执行进度投影。

        从 authoritative tables（import_revisions / jobs / works /
        revision_bindings / scrape_bindings）读取，禁止从前端 preview、日志
        文本或 Library Projection 反推运行状态。返回作品单元 + 用户阶段摘要，
        原始 job_id 只作为重试命令参数。
        """

        with self.database.connect() as conn:
            revision = conn.execute(
                "SELECT revision_id, status, confirmed_at FROM import_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None:
                raise KeyError(revision_id)
            job_rows = conn.execute(
                "SELECT job_id, job_type, work_id, status, attempts, last_error, updated_at "
                "FROM jobs WHERE revision_id = ? ORDER BY job_type, job_id",
                (revision_id,),
            ).fetchall()
            binding_rows = conn.execute(
                "SELECT work_id, COUNT(DISTINCT episode_id) AS episode_count, "
                "COUNT(DISTINCT asset_id) AS asset_count "
                "FROM revision_bindings WHERE revision_id = ? AND work_id != '' "
                "GROUP BY work_id",
                (revision_id,),
            ).fetchall()
            work_ids = sorted(
                {str(row["work_id"]) for row in job_rows if str(row["work_id"])}
                | {str(row["work_id"]) for row in binding_rows}
            )
            works: dict[str, dict] = {}
            if work_ids:
                placeholders = ",".join("?" for _ in work_ids)
                for row in conn.execute(
                    "SELECT work_id, preferred_title, work_type FROM works WHERE work_id IN (" + placeholders + ")",
                    work_ids,
                ).fetchall():
                    works[str(row["work_id"])] = dict(row)
            scrape_rows: dict[str, dict] = {}
            for row in conn.execute(
                "SELECT sb.work_id, sb.status, sb.metadata_json, sb.updated_at FROM scrape_bindings sb "
                "WHERE sb.revision_id = ? AND sb.updated_at = ( "
                "  SELECT MAX(updated_at) FROM scrape_bindings latest "
                "  WHERE latest.revision_id = sb.revision_id AND latest.work_id = sb.work_id "
                ")",
                (revision_id,),
            ).fetchall():
                metadata: dict = {}
                try:
                    decoded = json.loads(str(row["metadata_json"] or "{}"))
                    metadata = decoded if isinstance(decoded, dict) else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                scrape_rows[str(row["work_id"])] = {
                    "status": str(row["status"] or ""),
                    "binding_status": str(row["status"] or ""),
                    "metadata": metadata,
                    "metadata_state": str(metadata.get("metadata_state") or row["status"] or ""),
                    "reason": str(metadata.get("reason") or ""),
                    "reason_code": str(metadata.get("reason_code") or ""),
                    "updated_at": str(row["updated_at"] or ""),
                }

        jobs_by_work: dict[str, dict] = {}
        stage: dict[str, list[dict]] = {"mirror": [], "metadata": [], "projection": []}
        for row in job_rows:
            job = dict(row)
            job_type = str(job["job_type"])
            work_id = str(job["work_id"] or "")
            if job_type == "materialize_mirror":
                stage["mirror"].append(job)
                jobs_by_work.setdefault(work_id, {})["mirror"] = job
            elif job_type == "scrape_work":
                stage["metadata"].append(job)
                jobs_by_work.setdefault(work_id, {})["metadata"] = job
            elif job_type == "refresh_projection":
                stage["projection"].append(job)

        work_units: list[dict] = []
        for work_id in work_ids:
            work = works.get(work_id, {})
            job_pair = jobs_by_work.get(work_id, {})
            mirror = job_pair.get("mirror")
            metadata_job = job_pair.get("metadata")
            scrape = scrape_rows.get(work_id, {})
            scrape_status = str(scrape.get("metadata_state") or scrape.get("status") or "")
            # 历史“只缺图片”的失败在这里只读归一为 ready + degraded，
            # 作品不再被算成待处理，也不再从媒体墙消失。
            normalized_state, artifact_state, artifact_reasons = _artifact_view(scrape.get("metadata"))
            if normalized_state:
                scrape_status = normalized_state
            metadata_policy = metadata_recovery_policy(
                scrape.get("metadata") if isinstance(scrape.get("metadata"), dict) else {
                    "metadata_state": scrape_status,
                    "reason": scrape.get("reason", ""),
                    "reason_code": scrape.get("reason_code", ""),
                },
                binding_status=str(scrape.get("binding_status") or ""),
            )
            work_units.append({
                "work_id": work_id,
                "title": str(work.get("preferred_title") or work_id),
                "media_type": "movie" if str(work.get("work_type") or "") == "movie" else "tv",
                "episode_count": int(next(
                    (row["episode_count"] for row in binding_rows if row["work_id"] == work_id),
                    0,
                ) or 0),
                "asset_count": int(next(
                    (row["asset_count"] for row in binding_rows if row["work_id"] == work_id),
                    0,
                ) or 0),
                "overall_status": _derive_work_status(
                    mirror,
                    metadata_job,
                    scrape_status,
                ),
                "metadata_state": scrape_status,
                "metadata_reason": metadata_policy["reason"],
                "metadata_reason_code": str(scrape.get("reason_code") or ""),
                "artifact_state": artifact_state,
                "artifact_reasons": artifact_reasons,
                "metadata_warning": _safe_detail_text(scrape.get("metadata", {}).get("metadata_warning"))
                if isinstance(scrape.get("metadata"), dict) else "",
                "metadata_recovery_action": metadata_policy["action"],
                "metadata_recovery_hint": metadata_policy["hint"],
                "mirror": _job_summary(mirror),
                "metadata": _job_summary(metadata_job),
                # 任务状态相同并不代表详情快照不变，例如确认候选会更新
                # scrape_bindings.metadata_json。用持久更新时间组成版本键，前端
                # 才能在展开状态下可靠失效旧详情缓存。
                "detail_version": "|".join((
                    str((mirror or {}).get("updated_at") or ""),
                    str((metadata_job or {}).get("updated_at") or ""),
                    str(scrape.get("updated_at") or ""),
                )),
            })

        return {
            "revision_id": revision_id,
            "revision_status": str(revision["status"]),
            "overall_status": _revision_overall_status(work_units, stage),
            "stage_summary": {
                "mirror": _stage_summary(stage["mirror"]),
                "metadata": _metadata_stage_summary(stage["metadata"], scrape_rows),
                "projection": _stage_summary(stage["projection"]),
            },
            "work_units": work_units,
        }
