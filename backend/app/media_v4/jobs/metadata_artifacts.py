"""把已刮削元数据物化为可重建的 NFO 与图片产物。"""

from __future__ import annotations

import hashlib
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import httpx

from app.core.config import load_config
from app.core.url_guard import assert_public_dns_resolution, validate_remote_asset_url
from app.media_v4.jobs.artifact_paths import (
    episode_nfo_relative_path,
    episode_thumb_relative_path,
    work_nfo_relative_path,
)
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


def _resolve_relative(mirror_root: str | Path, relative_path: str) -> Path:
    """把 ``artifact_paths`` 的 POSIX 相对名拼成受管根下的真实路径。"""

    return Path(mirror_root).joinpath(*PurePosixPath(str(relative_path)).parts)


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


def _episode_numbers(episode: dict) -> tuple[int | None, int | None]:
    """NFO 的 ``(season, episode)``：未知字段省略，不写 0、不用电影兜底（C-006）。

    - 明确特别篇沿用旧的 ``S00`` 约定，集号取特别篇号；
    - 已知普通季 + 已知集号写真实值；
    - 未分季、只有绝对编号或完全未知时只保留确有证据的字段（或不写）。
    """

    season_kind = str(episode.get("season_kind") or "").strip().casefold()
    season = _safe_int(episode.get("local_season_number"))
    number = _safe_int(episode.get("local_episode_number"))
    special = _safe_int(episode.get("special_number"))
    episode_kind = str(episode.get("episode_kind") or "").strip().casefold()
    is_special = season_kind == "special" or (
        not season_kind and season == 0 and (special is not None or episode_kind == "special")
    )
    if is_special:
        return 0, special or number
    return (season if season and season > 0 else None), (number if number and number > 0 else None)


def _safe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _episode_nfo(episode: dict) -> bytes:
    root = ET.Element("episodedetails")
    season, number = _episode_numbers(episode)
    season_kind = str(episode.get("season_kind") or "").strip().casefold()
    local_title = str(episode.get("display_title") or "").strip()
    scraped_title = str(episode.get("title") or "").strip()
    is_special = season_kind == "special" or season == 0
    number = number or 0
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
    if season is not None:
        _add(root, "season", season)
    if number:
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


_MAX_ARTWORK_BYTES = 25 * 1024 * 1024


def _valid_artwork(payload: bytes, content_type: str) -> bool:
    """MIME 与文件签名一致；SVG 标题图仅接受无主动内容的 XML。"""
    if content_type == "image/jpeg":
        return payload.startswith(b"\xff\xd8\xff")
    if content_type == "image/png":
        return payload.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/webp":
        return payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    if content_type != "image/svg+xml" or not payload:
        return False
    lowered = payload.lower()
    if any(token in lowered for token in (b"<!doctype", b"<!entity", b"url(", b"@import")):
        return False
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return False
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        return False
    allowed_tags = {"svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
                    "defs", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "use",
                    "title", "desc", "text", "tspan"}
    for element in root.iter():
        if element.tag.removeprefix("{http://www.w3.org/2000/svg}") not in allowed_tags:
            return False
        for key, value in element.attrib.items():
            if key.lower().startswith("on") or (key.endswith("href") and not value.startswith("#")):
                return False
    return True


def _download_artwork(url: str, path: Path, *, client: httpx.Client) -> str:
    try:
        parsed = validate_remote_asset_url(url)
        decoded_path = unquote(parsed.path)
        if (parsed.hostname != "image.tmdb.org" or parsed.query or parsed.fragment
                or "\\" in decoded_path or any(part in {".", ".."} for part in decoded_path.split("/"))):
            return ""
        assert_public_dns_resolution(parsed.hostname)
    except ValueError:
        return ""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                if not response.is_redirect:
                    response.raise_for_status()
                return ""
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/jpeg", "image/png", "image/webp", "image/svg+xml"}:
                return ""
            length = response.headers.get("content-length")
            if length and (not length.isdigit() or int(length) > _MAX_ARTWORK_BYTES):
                return ""
            path.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            digest = hashlib.sha256()
            with temporary.open("wb") as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_ARTWORK_BYTES:
                        return ""
                    output.write(chunk)
                    digest.update(chunk)
            if not _valid_artwork(temporary.read_bytes(), content_type):
                return ""
            os.replace(temporary, path)
            from app.media_v4.assets.thumbnails import prepare_artwork

            prepare_artwork(path)
            return digest.hexdigest()
    finally:
        temporary.unlink(missing_ok=True)


def _artwork_filename(artifact_type: str, source_file_path: str) -> str:
    """保留 TMDB 图片的安全扩展名，尤其避免把 SVG 标题图伪装成 PNG。"""

    suffix = Path(urlparse(source_file_path).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".svg"}:
        suffix = ".jpg" if artifact_type in {"poster", "fanart"} else ".png"
    return f"{artifact_type}{suffix}"


def _published_artifacts(database, revision_id: str, work_id: str) -> dict[tuple[str, str], str]:
    """已发布产物的 ``{(类型, 目标路径): digest}``，用于跳过重复下载与重复写盘。

    `retry_artifacts` 的承诺是"只重新下载**缺失**的图片产物"，而下载是整轮里最贵、
    最容易被网络抖动影响的部分（24 集作品一次重试约 27 个 HTTPS 请求 + 25 次 NFO
    重写）。已有产物且磁盘文件非空时无需再动。
    """

    with database.connect() as conn:
        return {
            (str(row["artifact_type"]), str(row["target_path"])): str(row["digest"] or "")
            for row in conn.execute(
                "SELECT artifact_type, target_path, digest FROM artifacts "
                "WHERE revision_id = ? AND work_id = ? AND status = 'published'",
                (revision_id, work_id),
            )
        }


def _is_current(path: Path, published: dict[tuple[str, str], str], artifact_type: str) -> bool:
    """该产物已发布、且磁盘文件仍然存在且非空 → 本轮无需重下/重写。"""

    if (artifact_type, str(path)) not in published:
        return False
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _artifact_digest(
    path: Path,
    payload: bytes,
    *,
    artifact_type: str,
    published: dict[tuple[str, str], str],
) -> str:
    """内容未变就不重写文件（原子的写入也没必要动盘，且会刷新 updated_at）。"""

    digest = hashlib.sha256(payload).hexdigest()
    if _is_current(path, published, artifact_type):
        try:
            if path.read_bytes() == payload:
                return digest
        except OSError:
            pass
    return _write_atomic(path, payload)


def _materialize_local_artwork(
    *,
    config,
    work_id: str,
    mirror_root: str | Path,
    work_dir: Path,
    target: dict,
    metadata: dict,
    episode_metadata: dict[str, dict],
    artifacts: list[tuple[str, Path, str]],
    published: dict[tuple[str, str], str] | None = None,
    on_progress=None,
) -> None:
    published = published or {}
    from app.media_v4.jobs.artwork_provenance import artwork_download_status, ensure_artwork_provenance

    ensure_artwork_provenance(metadata)

    def tick() -> None:
        """每张图前后报一次心跳：下载阶段可能持续数分钟，不能让它看起来像失联。"""

        if on_progress is not None:
            on_progress()

    def take_existing(artifact_type: str, path: Path) -> bool:
        """已有可用产物时直接登记既有 digest，跳过下载。"""

        if not _is_current(path, published, artifact_type):
            return False
        artifacts.append((artifact_type, path, published[(artifact_type, str(path))]))
        from app.media_v4.assets.thumbnails import prepare_artwork

        prepare_artwork(path)
        return True

    with _artwork_client(config) as client:
        if target.get("work_type") == "series":
            for episode in target.get("episodes") or []:
                scraped = episode_metadata.get(str(episode.get("episode_id")), {})
                still_url = str(scraped.get("still_url") or "")
                if not still_url:
                    continue
                # 目标名只由 artifact_paths 计算：未知季/集进 Unassigned，不再写 Specials/S00E00。
                thumb_path = _resolve_relative(
                    mirror_root,
                    episode_thumb_relative_path(
                        work_id=work_id,
                        source_url=still_url,
                        season_kind=episode.get("season_kind"),
                        local_season_number=episode.get("local_season_number"),
                        local_episode_number=episode.get("local_episode_number"),
                        absolute_episode_number=episode.get("absolute_episode_number"),
                        special_number=episode.get("special_number"),
                    ),
                )
                if target.get('metadata_snapshot_id'):
                    thumb_path = work_dir / f"episode-{episode['episode_id']}-thumb.jpg"
                if take_existing("episode_thumb", thumb_path):
                    scraped["local_thumb_path"] = str(thumb_path)
                    artwork_download_status(metadata, 'still', published[('episode_thumb', str(thumb_path))],
                                            episode_id=str(episode.get('episode_id') or ''))
                    continue
                tick()
                try:
                    digest = _download_artwork(still_url, thumb_path, client=client)
                except (OSError, httpx.HTTPError):
                    digest = ""
                artwork_download_status(metadata, 'still', digest, episode_id=str(episode.get('episode_id') or ''))
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
            artwork_path = work_dir / filename
            if take_existing(artifact_type, artwork_path):
                metadata[f"local_{artifact_type}_path"] = str(artwork_path)
                artwork_download_status(metadata, {'fanart': 'backdrop', 'clearlogo': 'logo'}.get(artifact_type, artifact_type),
                                        published[(artifact_type, str(artwork_path))])
                continue
            tick()
            try:
                digest = _download_artwork(url, artwork_path, client=client)
            except (OSError, httpx.HTTPError):
                digest = ""
            artwork_download_status(metadata, {'fanart': 'backdrop', 'clearlogo': 'logo'}.get(artifact_type, artifact_type), digest)
            if digest:
                artifacts.append((artifact_type, artwork_path, digest))
                metadata[f"local_{artifact_type}_path"] = str(artwork_path)


def publish_metadata_artifacts(
    database: V4Database,
    *,
    revision_id: str,
    work_id: str,
    target: dict,
    metadata: dict,
    mirror_root: str | Path,
    on_progress=None,
    snapshot_id: str | None = None,
) -> tuple[str, ...]:
    """本轮实际写入/更新的产物 artifact_id（供 job 结果如实上报）。"""
    work_dir = Path(mirror_root) / work_directory_name(work_id)
    final_dir = None
    if snapshot_id:
        if str(uuid.UUID(snapshot_id)) != snapshot_id:
            raise ValueError('invalid metadata snapshot ID')
        final_dir = work_dir / '.metadata' / snapshot_id
        work_dir = final_dir.with_name('.pending-' + snapshot_id)
        managed_root = Path(mirror_root).resolve(strict=False)
        for directory in (work_dir, final_dir):
            if managed_root not in directory.resolve(strict=False).parents:
                raise ValueError('metadata generation is outside managed root')
        target = {**target, 'metadata_snapshot_id': snapshot_id}
    is_series = target.get("work_type") == "series"
    nfo_path = _resolve_relative(
        mirror_root,
        work_nfo_relative_path(work_id=work_id, is_movie=not is_series),
    )
    if snapshot_id:
        nfo_path = work_dir / 'work.nfo'
    # 已发布产物集合：重试路径据此跳过"没必要再动"的下载与写盘。
    published = _published_artifacts(database, revision_id, work_id)
    artifacts = [(
        "nfo",
        nfo_path,
        _artifact_digest(
            nfo_path,
            _work_nfo(target, metadata),
            artifact_type="nfo",
            published=published,
        ),
    )]
    episode_metadata = {
        str(item.get("episode_id")): item
        for item in metadata.get("episode_mappings") or []
        if item.get("episode_id")
    }
    config = load_config()
    download_local_artwork = config.artwork_storage_mode != "remote"
    if is_series:
        for episode in target.get("episodes") or []:
            # 目标名只由 artifact_paths 计算；未知季/集落在 Unassigned，不用 S00E00 兜底。
            episode_path = _resolve_relative(
                mirror_root,
                episode_nfo_relative_path(
                    work_id=work_id,
                    season_kind=episode.get("season_kind"),
                    local_season_number=episode.get("local_season_number"),
                    local_episode_number=episode.get("local_episode_number"),
                    absolute_episode_number=episode.get("absolute_episode_number"),
                    special_number=episode.get("special_number"),
                ),
            )
            scraped = episode_metadata.get(str(episode.get("episode_id")), {})
            if snapshot_id:
                if not scraped:
                    continue
                episode_path = work_dir / f"episode-{episode['episode_id']}.nfo"
            episode_payload = _episode_nfo({**episode, **scraped})
            artifacts.append((
                "episode_nfo",
                episode_path,
                _artifact_digest(
                    episode_path,
                    episode_payload,
                    artifact_type="episode_nfo",
                    published=published,
                ),
            ))

    if download_local_artwork:
        _materialize_local_artwork(
            config=config,
            work_id=work_id,
            mirror_root=mirror_root,
            work_dir=work_dir,
            target=target,
            metadata=metadata,
            episode_metadata=episode_metadata,
            artifacts=artifacts,
            published=published,
            on_progress=on_progress,
        )

    if final_dir is not None:
        # 整代文件准备好后移动到唯一目标目录，任何旧代次均不参与覆盖。
        os.rename(work_dir, final_dir)
        artifacts = [(kind, final_dir / path.relative_to(work_dir), digest) for kind, path, digest in artifacts]
        def relocate(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.startswith('local_') and isinstance(item, str):
                        try:
                            value[key] = str(final_dir / Path(item).relative_to(work_dir))
                        except ValueError:
                            pass
                    elif isinstance(item, (dict, list)):
                        relocate(item)
            elif isinstance(value, list):
                for item in value:
                    relocate(item)
        relocate(metadata)
    now = _now()
    produced_ids: list[str] = []
    with database.connect() as conn:
        for artifact_type, path, digest in artifacts:
            artifact_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, revision_id, work_id, artifact_type,
                    target_path, digest, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?)
                ON CONFLICT(revision_id, artifact_type, target_path) DO UPDATE SET
                    digest = excluded.digest, status = 'published', updated_at = excluded.updated_at
                """,
                (artifact_id, revision_id, work_id, artifact_type, str(path), digest, now, now),
            )
            stored = conn.execute(
                "SELECT artifact_id FROM artifacts "
                "WHERE revision_id = ? AND artifact_type = ? AND target_path = ?",
                (revision_id, artifact_type, str(path)),
            ).fetchone()
            produced_ids.append(str(stored["artifact_id"]) if stored is not None else artifact_id)
    return tuple(produced_ids)
