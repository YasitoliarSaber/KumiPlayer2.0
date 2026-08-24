"""元数据完整性评估（P-001 7.7 R3）。

只有真实 provider identity、Work NFO 非空、按 artwork_storage_mode 有有效
远端 poster/fanart URL 或已发布本地文件、且 TV 的所有可发布 Episode 均有
Episode NFO 时，binding/metadata 才能成为 confirmed + ready。缺失或写入
失败保存可读原因并进入非 ready 状态。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import load_config


def assess_metadata_completeness(
    database,
    *,
    revision_id: str,
    work_id: str,
    target: dict,
    metadata: dict,
    mirror_root: str | Path,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []

    provider = str(metadata.get("provider") or "")
    provider_id = str(metadata.get("provider_id") or "")
    if provider in {"", "local"} or not provider_id:
        reasons.append("缺少真实外部 Provider 身份")

    with database.connect() as conn:
        work_nfo = conn.execute(
            """
            SELECT digest, target_path FROM artifacts
            WHERE revision_id = ? AND work_id = ? AND artifact_type = 'nfo' AND status = 'published'
            LIMIT 1
            """,
            (revision_id, work_id),
        ).fetchone()
    if work_nfo is None or not (work_nfo["digest"] or ""):
        reasons.append("缺少 Work NFO 或内容为空")

    config = load_config()
    mode = str(getattr(config, "artwork_storage_mode", "") or "local").strip() or "local"
    poster_url = str(metadata.get("poster_url") or "")
    fanart_url = str(metadata.get("fanart_url") or "")
    if mode == "remote":
        if not poster_url:
            reasons.append("缺少海报远端地址")
        if not fanart_url:
            reasons.append("缺少背景图远端地址")
    else:
        with database.connect() as conn:
            poster_artifact = conn.execute(
                """
                SELECT target_path FROM artifacts
                WHERE revision_id = ? AND work_id = ? AND artifact_type = 'poster' AND status = 'published'
                LIMIT 1
                """,
                (revision_id, work_id),
            ).fetchone()
            fanart_artifact = conn.execute(
                """
                SELECT target_path FROM artifacts
                WHERE revision_id = ? AND work_id = ? AND artifact_type = 'fanart' AND status = 'published'
                LIMIT 1
                """,
                (revision_id, work_id),
            ).fetchone()
        if poster_artifact is None or not Path(str(poster_artifact["target_path"])).is_file():
            reasons.append("海报下载或发布失败")
        if fanart_artifact is None or not Path(str(fanart_artifact["target_path"])).is_file():
            reasons.append("背景图下载或发布失败")

    if str(target.get("work_type") or "") == "series":
        with database.connect() as conn:
            episode_total = conn.execute(
                """
                SELECT COUNT(DISTINCT episode_id) AS c FROM revision_bindings
                WHERE revision_id = ? AND work_id = ? AND episode_id IS NOT NULL
                """,
                (revision_id, work_id),
            ).fetchone()["c"]
            episode_nfo = conn.execute(
                """
                SELECT COUNT(DISTINCT target_path) AS c FROM artifacts
                WHERE revision_id = ? AND work_id = ? AND artifact_type = 'episode_nfo' AND status = 'published'
                """,
                (revision_id, work_id),
            ).fetchone()["c"]
        if int(episode_nfo or 0) < int(episode_total or 0):
            reasons.append(f"缺少 {int(episode_total or 0) - int(episode_nfo or 0)} 个剧集 NFO")

    return not reasons, reasons
