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
    else:
        nfo_path = Path(str(work_nfo["target_path"]))
        if not nfo_path.is_file() or nfo_path.stat().st_size <= 0:
            reasons.append("Work NFO 文件缺失或为空")

    config = load_config()
    mode = str(getattr(config, "artwork_storage_mode", "") or "local").strip() or "local"
    poster_url = str(metadata.get("poster_url") or "")
    fanart_url = str(metadata.get("fanart_url") or "")
    if mode == "remote":
        if not _valid_remote_artwork_url(poster_url):
            reasons.append("海报远端地址非法或为空")
        if not _valid_remote_artwork_url(fanart_url):
            reasons.append("背景图远端地址非法或为空")
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
        if poster_artifact is None or not _file_ok(poster_artifact["target_path"]):
            reasons.append("海报下载或发布失败")
        if fanart_artifact is None or not _file_ok(fanart_artifact["target_path"]):
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
            episode_artifacts = conn.execute(
                """
                SELECT DISTINCT target_path FROM artifacts
                WHERE revision_id = ? AND work_id = ? AND artifact_type = 'episode_nfo' AND status = 'published'
                """,
                (revision_id, work_id),
            ).fetchall()
        missing_episode_nfo = 0
        for artifact in episode_artifacts:
            if not _file_ok(artifact["target_path"]):
                missing_episode_nfo += 1
        if int(episode_total or 0) > len(episode_artifacts) or missing_episode_nfo:
            reasons.append(
                f"缺少或损坏 {max(int(episode_total or 0) - len(episode_artifacts), 0) + missing_episode_nfo} 个剧集 NFO"
            )

    return not reasons, reasons


def _file_ok(path: str) -> bool:
    try:
        return Path(str(path)).is_file() and Path(str(path)).stat().st_size > 0
    except OSError:
        return False


def _valid_remote_artwork_url(url: str) -> bool:
    """远端 artwork 只允许 https 的受信任图片主机。"""

    from urllib.parse import urlsplit

    value = (url or "").strip()
    if not value:
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").casefold()
    return host in {"image.tmdb.org", "image.tmdb.org."}


def assess_persisted_completeness(
    database,
    *,
    revision_id: str,
    work_id: str,
    metadata: dict,
) -> tuple[bool, list[str]]:
    """投影重建时复查权威完整性：重新检查 artifact 文件，不信任 metadata_json。"""

    reasons: list[str] = []
    with database.connect() as conn:
        work_nfo = conn.execute(
            """
            SELECT target_path FROM artifacts
            WHERE revision_id = ? AND work_id = ? AND artifact_type = 'nfo' AND status = 'published'
            LIMIT 1
            """,
            (revision_id, work_id),
        ).fetchone()
    if work_nfo is None or not _file_ok(work_nfo["target_path"]):
        reasons.append("Work NFO 文件缺失或为空")

    config = load_config()
    mode = str(getattr(config, "artwork_storage_mode", "") or "local").strip() or "local"
    if mode == "remote":
        if not _valid_remote_artwork_url(str(metadata.get("poster_url") or "")):
            reasons.append("海报远端地址非法或为空")
        if not _valid_remote_artwork_url(str(metadata.get("fanart_url") or "")):
            reasons.append("背景图远端地址非法或为空")
    else:
        with database.connect() as conn:
            poster = conn.execute(
                "SELECT target_path FROM artifacts WHERE revision_id = ? AND work_id = ? "
                "AND artifact_type = 'poster' AND status = 'published' LIMIT 1",
                (revision_id, work_id),
            ).fetchone()
            fanart = conn.execute(
                "SELECT target_path FROM artifacts WHERE revision_id = ? AND work_id = ? "
                "AND artifact_type = 'fanart' AND status = 'published' LIMIT 1",
                (revision_id, work_id),
            ).fetchone()
        if poster is None or not _file_ok(poster["target_path"]):
            reasons.append("海报文件缺失")
        if fanart is None or not _file_ok(fanart["target_path"]):
            reasons.append("背景图文件缺失")

    with database.connect() as conn:
        episode_artifacts = conn.execute(
            "SELECT DISTINCT target_path FROM artifacts WHERE revision_id = ? AND work_id = ? "
            "AND artifact_type = 'episode_nfo' AND status = 'published'",
            (revision_id, work_id),
        ).fetchall()
        episode_total = conn.execute(
            "SELECT COUNT(DISTINCT episode_id) AS c FROM revision_bindings "
            "WHERE revision_id = ? AND work_id = ? AND episode_id IS NOT NULL",
            (revision_id, work_id),
        ).fetchone()["c"]
    missing = sum(1 for artifact in episode_artifacts if not _file_ok(artifact["target_path"]))
    if int(episode_total or 0) > len(episode_artifacts) or missing:
        reasons.append("剧集 NFO 缺失或损坏")
    return not reasons, reasons
