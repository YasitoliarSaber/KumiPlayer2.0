# -*- coding: utf-8 -*-
"""只读刮削完整性审计，不修改镜像或 ScrapeMap。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from app.scrape.models import ScrapeCandidate, ScrapeMap, ScrapeTarget


def audit_scrape_integrity(targets: list[ScrapeTarget], scrape_map: ScrapeMap) -> dict:
    issues: list[dict] = []
    maps_by_target = {item.scrape_target_id: item for item in scrape_map.items}
    maps_by_path = {
        _path_key(item.nfo_path): item
        for item in scrape_map.items
        if item.nfo_path
    }

    matched_map_ids: set[int] = set()
    for target in targets:
        nfo_path = Path(target.target_nfo_path or _default_nfo_path(target))
        map_item = maps_by_target.get(target.scrape_target_id) or maps_by_path.get(_path_key(str(nfo_path)))
        if map_item:
            matched_map_ids.add(id(map_item))
        context = _target_context(target, nfo_path, map_item)

        if not nfo_path.exists():
            if _movie_owned_special_has_parent_metadata(target):
                continue
            issues.append({
                **context,
                "code": "missing_metadata",
                "message": "当前刮削目标缺少作品级 NFO",
                "cleanup_preview": [],
            })
            continue

        try:
            root = ET.parse(nfo_path).getroot()
        except (ET.ParseError, OSError) as exc:
            issues.append({
                **context,
                "code": "invalid_nfo",
                "message": f"NFO 无法解析: {exc}",
                "cleanup_preview": _cleanup_preview(target, nfo_path),
            })
            continue

        nfo_tmdb_id = _int_text(root.findtext("tmdbid"))
        nfo_title = (root.findtext("title") or "").strip()
        nfo_original_title = (root.findtext("originaltitle") or "").strip()

        verified_binding = _verified_target_binding(target)
        if verified_binding and (
            (map_item and (int(getattr(map_item, "tmdb_id", 0) or 0) != verified_binding.tmdb_id))
            or nfo_tmdb_id != verified_binding.tmdb_id
        ):
            issues.append({
                **context,
                "code": "wrong_binding",
                "message": "已核实的目录强绑定与当前 TMDB ID 不一致",
                "expected_tmdb_id": verified_binding.tmdb_id,
                "nfo_tmdb_id": nfo_tmdb_id,
                "cleanup_preview": _cleanup_preview(target, nfo_path),
            })
            continue

        if map_item and map_item.tmdb_id and nfo_tmdb_id and int(map_item.tmdb_id) != nfo_tmdb_id:
            issues.append({
                **context,
                "code": "stale_map",
                "message": "ScrapeMap 的 TMDB ID 与 NFO 不一致",
                "nfo_tmdb_id": nfo_tmdb_id,
                "cleanup_preview": _cleanup_preview(target, nfo_path),
            })
            continue

        map_matches_target = bool(map_item) and _map_identity_matches_target(target, map_item)
        nfo_matches_target = _title_identity_safe(target, nfo_title, nfo_original_title)
        evidence_matches = bool(map_item) and _persisted_evidence_matches(target, map_item, nfo_title, nfo_original_title)
        stable_overlap = _has_stable_title_overlap(target, nfo_title, nfo_original_title)
        verified_identity = bool(verified_binding)
        if (not map_matches_target) or (
            not verified_identity
            and not nfo_matches_target
            and not evidence_matches
            and not stable_overlap
        ):
            code = "wrong_binding" if _same_script_identity_conflict(target, nfo_title, nfo_original_title) else "identity_unverified"
            issues.append({
                **context,
                "code": code,
                "message": (
                    "NFO 标题与当前目录作品身份明显不一致"
                    if code == "wrong_binding"
                    else "旧映射缺少可验证的跨语言别名证据，需人工核验"
                ),
                "nfo_title": nfo_title,
                "nfo_tmdb_id": nfo_tmdb_id,
                "cleanup_preview": _cleanup_preview(target, nfo_path),
            })

    for map_item in scrape_map.items:
        if id(map_item) in matched_map_ids:
            continue
        nfo_path = Path(map_item.nfo_path) if map_item.nfo_path else None
        issues.append({
            "code": "orphan_map",
            "message": "ScrapeMap 记录不属于当前有效 ImportPlan，需人工核验后清理",
            "source": map_item.source,
            "scrape_target_id": map_item.scrape_target_id,
            "series_group": map_item.series_group,
            "local_title": map_item.local_title,
            "map_tmdb_id": map_item.tmdb_id,
            "nfo_path": str(nfo_path) if nfo_path else "",
            "cleanup_preview": [str(nfo_path)] if nfo_path and nfo_path.exists() else [],
        })

    counts = {
        code: sum(1 for issue in issues if issue["code"] == code)
        for code in ("wrong_binding", "stale_map", "missing_metadata", "invalid_nfo", "orphan_map", "identity_unverified")
    }
    return {
        "ok": not any(issue["code"] in {"wrong_binding", "stale_map", "invalid_nfo"} for issue in issues),
        "summary": {
            "target_count": len(targets),
            "issue_count": len(issues),
            **{f"{code}_count": count for code, count in counts.items()},
        },
        "issues": issues,
    }


def _title_identity_safe(target: ScrapeTarget, title: str, original_title: str) -> bool:
    if _has_verified_alias(target, title, original_title):
        return True
    from app.scrape.auto import _candidate_title_identity_safe

    candidate = ScrapeCandidate(
        tmdb_type=target.scrape_type,
        title=title,
        original_title=original_title,
    )
    return _candidate_title_identity_safe(target, candidate)[0]


def _map_identity_matches_target(target: ScrapeTarget, map_item) -> bool:
    verified_binding = _verified_target_binding(target)
    if verified_binding:
        return int(getattr(map_item, "tmdb_id", 0) or 0) == verified_binding.tmdb_id
    if _has_verified_alias(
        target,
        getattr(map_item, "local_title", "") or getattr(map_item, "scrape_title", ""),
        getattr(map_item, "original_title", ""),
    ):
        return True
    map_candidate = ScrapeCandidate(
        tmdb_type=getattr(map_item, "tmdb_type", ""),
        title=getattr(map_item, "local_title", "") or getattr(map_item, "scrape_title", ""),
        original_title=getattr(map_item, "original_title", ""),
    )
    from app.scrape.auto import _candidate_title_identity_safe

    return _candidate_title_identity_safe(target, map_candidate)[0]


def _has_verified_alias(target: ScrapeTarget, title: str, original_title: str) -> bool:
    from app.recognition.verified_titles import titles_share_verified_alias

    local_titles = (target.scrape_title, target.local_title, target.series_group)
    remote_titles = (title, original_title)
    return any(
        titles_share_verified_alias(local, remote)
        for local in local_titles
        for remote in remote_titles
        if local and remote
    )


def _verified_target_binding(target: ScrapeTarget):
    from app.recognition.verified_titles import match_verified_tmdb_binding

    path_text = " ".join(
        value
        for value in (
            target.series_group,
            target.local_title,
            target.original_title,
            target.source_subwork_dir,
        )
        if value
    )
    return match_verified_tmdb_binding(path_text)


def _persisted_evidence_matches(target: ScrapeTarget, map_item, title: str, original_title: str) -> bool:
    evidence = getattr(map_item, "identity_evidence", None) or {}
    if evidence.get("provider_tmdb_link") == "explicit_verified":
        return True
    aliases = list(evidence.get("provider_title_aliases") or [])
    if not aliases:
        return False
    from app.scrape.auto import _candidate_has_trusted_provider_identity

    candidate = ScrapeCandidate(
        provider=str(evidence.get("provider") or ""),
        tmdb_type=getattr(map_item, "tmdb_type", ""),
        title=str(evidence.get("candidate_title") or title),
        original_title=str(evidence.get("candidate_original_title") or original_title),
        raw={
            "provider_title_aliases": aliases,
            "provider_tmdb_link": evidence.get("provider_tmdb_link") or "",
        },
    )
    return _candidate_has_trusted_provider_identity(target, candidate)


def _same_script_identity_conflict(target: ScrapeTarget, title: str, original_title: str) -> bool:
    local = " ".join((target.scrape_title, target.local_title, target.series_group))
    remote = " ".join((title, original_title))
    local_has_cjk = bool(re.search(r"[\u3400-\u9fff]", local))
    remote_has_cjk = bool(re.search(r"[\u3400-\u9fff]", remote))
    local_has_latin = bool(re.search(r"[a-z]", local, re.IGNORECASE))
    remote_has_latin = bool(re.search(r"[a-z]", remote, re.IGNORECASE))
    return (local_has_cjk and remote_has_cjk) or (local_has_latin and remote_has_latin)


def _has_stable_title_overlap(target: ScrapeTarget, title: str, original_title: str) -> bool:
    local = " ".join((target.scrape_title, target.local_title, target.series_group))
    remote = " ".join((title, original_title))
    local_cjk = re.sub(r"[^\u3400-\u9fff]", "", local)
    remote_cjk = re.sub(r"[^\u3400-\u9fff]", "", remote)
    shorter, longer = sorted((local_cjk, remote_cjk), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True
    local_words = {word for word in re.findall(r"[a-z0-9]+", local.casefold()) if len(word) >= 4}
    remote_words = {word for word in re.findall(r"[a-z0-9]+", remote.casefold()) if len(word) >= 4}
    return bool(local_words & remote_words)


def _default_nfo_path(target: ScrapeTarget) -> str:
    name = "movie.nfo" if target.scrape_type == "movie" else "tvshow.nfo"
    return str(Path(target.target_dir) / name)


def _movie_owned_special_has_parent_metadata(target: ScrapeTarget) -> bool:
    if target.group_type != "special" or target.media_type != "movie":
        return False
    return (Path(target.target_dir).parent / "movie.nfo").exists()


def _path_key(value: str) -> str:
    if not value:
        return ""
    return os.path.abspath(value).casefold()


def _int_text(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.isdigit() else None


def _target_context(target: ScrapeTarget, nfo_path: Path, map_item) -> dict:
    return {
        "source": target.source,
        "scrape_target_id": target.scrape_target_id,
        "work_id": target.work_id,
        "series_group": target.series_group,
        "local_title": target.local_title,
        "local_season_number": target.local_season_number,
        "nfo_path": str(nfo_path),
        "map_scrape_target_id": getattr(map_item, "scrape_target_id", "") if map_item else "",
        "map_tmdb_id": getattr(map_item, "tmdb_id", None) if map_item else None,
    }


def _cleanup_preview(target: ScrapeTarget, nfo_path: Path) -> list[str]:
    paths = [
        nfo_path,
        Path(target.target_poster_path) if target.target_poster_path else None,
        Path(target.target_fanart_path) if target.target_fanart_path else None,
        Path(target.target_clearlogo_path) if target.target_clearlogo_path else None,
    ]
    return [str(path) for path in paths if path and path.exists()]
