"""把已刮削元数据物化为可重建的 NFO 与图片产物。"""

from __future__ import annotations

import hashlib
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.core.config import load_config
from app.media_v4.jobs.paths import work_directory_name
from app.media_v4.parsing.episode_titles import (
    ensure_special_title_number,
    is_generic_special_title,
    is_special_marker_only,
)
from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _add(parent: ET.Element, name: str, value) -> None:
    if value is not None and value != "":
        ET.SubElement(parent, name).text = str(value)


def _work_nfo(target: dict, metadata: dict) -> bytes:
    is_series = target.get("work_type") == "series"
    root = ET.Element("tvshow" if is_series else "movie")
    _add(root, "title", metadata.get("title") or target.get("preferred_title"))
    _add(root, "originaltitle", metadata.get("original_title") or target.get("original_title"))
    _add(root, "year", metadata.get("year") or target.get("year"))
    _add(root, "plot", metadata.get("plot"))
    _add(root, "rating", metadata.get("rating"))
    _add(root, "premiered", metadata.get("premiered"))
    _add(root, "runtime", metadata.get("runtime"))
    if metadata.get("provider") and metadata.get("provider_id"):
        unique = ET.SubElement(root, "uniqueid", {"type": str(metadata["provider"]), "default": "true"})
        unique.text = str(metadata["provider_id"])
    for genre in metadata.get("genres") or []:
        _add(root, "genre", genre)
    for studio in metadata.get("studios") or []:
        _add(root, "studio", studio)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _episode_nfo(episode: dict) -> bytes:
    root = ET.Element("episodedetails")
    season = int(episode.get("local_season_number") or 0)
    number = int(episode.get("local_episode_number") or episode.get("special_number") or 0)
    local_title = str(episode.get("display_title") or "").strip()
    scraped_title = str(episode.get("title") or "").strip()
    is_special = season == 0 or episode.get("season_kind") == "special"
    if is_special:
        if local_title and not (
            is_special_marker_only(local_title) or is_generic_special_title(local_title)
        ):
            title = ensure_special_title_number(local_title, number)
        elif scraped_title and not is_generic_special_title(scraped_title):
            title = ensure_special_title_number(scraped_title, number)
        else:
            title = ensure_special_title_number(local_title or scraped_title, number)
    else:
        title = scraped_title or local_title or f"第 {number} 集"
    _add(root, "title", title)
    _add(root, "season", season)
    _add(root, "episode", number)
    _add(root, "plot", episode.get("plot"))
    _add(root, "runtime", episode.get("runtime"))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"


def _write_atomic(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def _artwork_client(config) -> httpx.Client:
    timeout = min(max(int(config.tmdb_timeout or 10), 3), 12)
    return httpx.Client(
        timeout=timeout,
        proxy=config.proxy_url or None,
        http2=True,
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=6, keepalive_expiry=60.0),
    )


def _download_artwork(url: str, path: Path, *, client: httpx.Client) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "image.tmdb.org":
        return ""
    response = client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("image/") or len(response.content) > 25 * 1024 * 1024:
        return ""
    return _write_atomic(path, response.content)


def _artwork_filename(artifact_type: str, source_file_path: str) -> str:
    """保留 TMDB 图片的安全扩展名，尤其避免把 SVG 标题图伪装成 PNG。"""

    suffix = Path(urlparse(source_file_path).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
        suffix = ".jpg" if artifact_type in {"poster", "fanart"} else ".png"
    return f"{artifact_type}{suffix}"


def _episode_thumb_filename(season: int, number: int, source_url: str) -> str:
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    return f"S{season:02d}E{number:02d}-thumb{suffix}"


def _materialize_local_artwork(
    *,
    config,
    work_dir: Path,
    target: dict,
    metadata: dict,
    episode_metadata: dict[str, dict],
    artifacts: list[tuple[str, Path, str]],
) -> None:
    with _artwork_client(config) as client:
        if target.get("work_type") == "series":
            for episode in target.get("episodes") or []:
                scraped = episode_metadata.get(str(episode.get("episode_id")), {})
                still_url = str(scraped.get("still_url") or "")
                if not still_url:
                    continue
                season = int(episode.get("local_season_number") or 0)
                number = int(episode.get("local_episode_number") or episode.get("special_number") or 0)
                season_dir = "Specials" if season == 0 or episode.get("season_kind") == "special" else f"Season {season:02d}"
                thumb_path = work_dir / season_dir / _episode_thumb_filename(season, number, still_url)
                try:
                    digest = _download_artwork(still_url, thumb_path, client=client)
                except (OSError, httpx.HTTPError):
                    digest = ""
                if digest:
                    artifacts.append(("episode_thumb", thumb_path, digest))
                    scraped["local_thumb_path"] = str(thumb_path)

        for artifact_type, key in (
            ("poster", "poster_url"),
            ("fanart", "fanart_url"),
            ("clearlogo", "clearlogo_url"),
        ):
            url = str(metadata.get(key) or "")
            if not url:
                continue
            filename = _artwork_filename(artifact_type, str(metadata.get(f"{artifact_type}_file_path") or ""))
            try:
                digest = _download_artwork(url, work_dir / filename, client=client)
            except (OSError, httpx.HTTPError):
                digest = ""
            if digest:
                artifacts.append((artifact_type, work_dir / filename, digest))
                metadata[f"local_{artifact_type}_path"] = str(work_dir / filename)


def publish_metadata_artifacts(
    database: V4Database,
    *,
    revision_id: str,
    work_id: str,
    target: dict,
    metadata: dict,
    mirror_root: str | Path,
) -> None:
    work_dir = Path(mirror_root) / work_directory_name(work_id)
    is_series = target.get("work_type") == "series"
    nfo_path = work_dir / ("tvshow.nfo" if is_series else "movie.nfo")
    artifacts = [("nfo", nfo_path, _write_atomic(nfo_path, _work_nfo(target, metadata)))]
    episode_metadata = {
        str(item.get("episode_id")): item
        for item in metadata.get("episode_mappings") or []
        if item.get("episode_id")
    }
    config = load_config()
    download_local_artwork = config.artwork_storage_mode != "remote"
    if is_series:
        for episode in target.get("episodes") or []:
            season = int(episode.get("local_season_number") or 0)
            number = int(episode.get("local_episode_number") or episode.get("special_number") or 0)
            if season == 0 or episode.get("season_kind") == "special":
                season_dir = "Specials"
            else:
                season_dir = f"Season {season:02d}"
            episode_path = work_dir / season_dir / f"S{season:02d}E{number:02d}.nfo"
            scraped = episode_metadata.get(str(episode.get("episode_id")), {})
            artifacts.append(("episode_nfo", episode_path, _write_atomic(episode_path, _episode_nfo({**episode, **scraped}))))

    if download_local_artwork:
        _materialize_local_artwork(
            config=config,
            work_dir=work_dir,
            target=target,
            metadata=metadata,
            episode_metadata=episode_metadata,
            artifacts=artifacts,
        )

    now = _now()
    with database.connect() as conn:
        for artifact_type, path, digest in artifacts:
            conn.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, revision_id, work_id, artifact_type,
                    target_path, digest, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?)
                ON CONFLICT(revision_id, artifact_type, target_path) DO UPDATE SET
                    digest = excluded.digest, status = 'published', updated_at = excluded.updated_at
                """,
                (str(uuid.uuid4()), revision_id, work_id, artifact_type, str(path), digest, now, now),
            )
