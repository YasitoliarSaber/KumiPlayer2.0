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
    _add(root, "title", episode.get("display_title") or f"第 {number} 集")
    _add(root, "season", season)
    _add(root, "episode", number)
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


def _download_artwork(url: str, path: Path) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "image.tmdb.org":
        return ""
    config = load_config()
    timeout = min(max(int(config.tmdb_timeout or 10), 3), 12)
    client = (
        httpx.Client(timeout=timeout, proxy=config.proxy_url)
        if config.proxy_url
        else httpx.Client(timeout=timeout)
    )
    with client:
        response = client.get(url)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("image/") or len(response.content) > 25 * 1024 * 1024:
            return ""
        return _write_atomic(path, response.content)


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
    if is_series:
        for episode in target.get("episodes") or []:
            season = int(episode.get("local_season_number") or 0)
            number = int(episode.get("local_episode_number") or episode.get("special_number") or 0)
            if season == 0 or episode.get("season_kind") == "special":
                season_dir = "Specials"
            else:
                season_dir = f"Season {season:02d}"
            episode_path = work_dir / season_dir / f"S{season:02d}E{number:02d}.nfo"
            artifacts.append(("episode_nfo", episode_path, _write_atomic(episode_path, _episode_nfo(episode))))

    config = load_config()
    if config.artwork_storage_mode != "remote":
        for artifact_type, key, filename in (
            ("poster", "poster_url", "poster.jpg"),
            ("fanart", "fanart_url", "fanart.jpg"),
        ):
            url = str(metadata.get(key) or "")
            if not url:
                continue
            try:
                digest = _download_artwork(url, work_dir / filename)
            except (OSError, httpx.HTTPError):
                digest = ""
            if digest:
                artifacts.append((artifact_type, work_dir / filename, digest))
                metadata[f"local_{artifact_type}_path"] = str(work_dir / filename)

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
