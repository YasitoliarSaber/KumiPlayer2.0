# -*- coding: utf-8 -*-
"""Scrape metadata validation.

The recognizer is necessarily heuristic.  This module validates the selected
metadata against the local scrape target before it is persisted, so bad matches
can be routed to review instead of silently poisoning the library.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any, Optional

from app.scrape.models import ScrapeTarget


@dataclass
class ScrapeValidationIssue:
    code: str
    level: str  # info / warn / block
    message: str
    local_value: Any = None
    remote_value: Any = None


def validate_scrape_metadata(
    target: ScrapeTarget,
    detail: dict,
    tmdb_type: str,
    tmdb_season_number: Optional[int] = None,
    tmdb_client: Optional[Any] = None,
    trusted_series_binding: bool = False,
) -> list[ScrapeValidationIssue]:
    """Validate a selected metadata record against local structure.

    ``trusted_series_binding`` 仅用于同一 ``series_group`` 已确认过的
    TMDB 条目复用。该场景中，后续季度的本地中文标题与 TMDB 主标题可能
    完全不同；仍校验媒体类型、动画域和季度结构，但不再重复以标题相似度
    否定已确认的系列绑定。
    """
    issues: list[ScrapeValidationIssue] = []
    if tmdb_type != target.scrape_type:
        issues.append(ScrapeValidationIssue(
            code="type_mismatch",
            level="block",
            message=f"刮削类型不一致: {tmdb_type} != {target.scrape_type}",
            local_value=target.scrape_type,
            remote_value=tmdb_type,
        ))
        return issues

    if not trusted_series_binding:
        title_issue = _validate_title(target, detail, tmdb_type)
        if title_issue:
            issues.append(title_issue)

    domain_issue = _validate_media_domain(target, detail, tmdb_type)
    if domain_issue:
        issues.append(domain_issue)

    if tmdb_type == "tv":
        issues.extend(_validate_tv_structure(target, detail, tmdb_season_number, tmdb_client))

    return issues


def blocking_issues(issues: list[ScrapeValidationIssue]) -> list[ScrapeValidationIssue]:
    return [issue for issue in issues if issue.level == "block"]


def issue_messages(issues: list[ScrapeValidationIssue]) -> list[str]:
    return [issue.message for issue in issues]


def _validate_title(target: ScrapeTarget, detail: dict, tmdb_type: str) -> Optional[ScrapeValidationIssue]:
    remote_title = (
        detail.get("name")
        or detail.get("title")
        or detail.get("original_name")
        or detail.get("original_title")
        or ""
    )
    local_title = target.scrape_title or target.local_title or target.series_group or ""
    if not local_title or not remote_title:
        return None

    # Explicit TMDB hints are considered intentional, but still allow structure
    # checks below to catch wrong season/card shape.
    if target.tmdb_hint_id and detail.get("id") == target.tmdb_hint_id:
        return None

    local = _normalize_title(local_title)
    remote = _normalize_title(remote_title)
    series = _normalize_title(target.series_group or "")
    if local and remote and (local in remote or remote in local):
        return None
    if series and remote and (series in remote or remote in series):
        return None

    similarity = SequenceMatcher(None, local, remote).ratio() if local and remote else 0.0
    if similarity < 0.22 and len(local) >= 5:
        return ScrapeValidationIssue(
            code="title_low_similarity",
            level="warn" if tmdb_type == "tv" else "block",
            message=f"标题相似度很低: 本地「{local_title}」/ 刮削「{remote_title}」",
            local_value=local_title,
            remote_value=remote_title,
        )
    return None


def _validate_media_domain(target: ScrapeTarget, detail: dict, tmdb_type: str) -> Optional[ScrapeValidationIssue]:
    """防止动画目标自动绑定到真人/非动画 TV 条目。"""
    if tmdb_type != "tv":
        return None
    if not _target_prefers_anime(target):
        return None
    if _detail_has_animation_genre(detail):
        return None
    genre_names = _detail_genre_names(detail)
    genre_ids = detail.get("genre_ids") or []
    genres_payload = detail.get("genres") or []
    # “没有类型数据”与“明确不是动画”是两种状态。第三方返回空 genres 时
    # 不能据此否定正确作品；只有详情明确给出了其他类型才阻断。
    if not genre_names and not genre_ids and not genres_payload:
        return None
    genres = ", ".join(genre_names) or "未知类型"
    return ScrapeValidationIssue(
        code="anime_domain_mismatch",
        level="block",
        message=f"动画目标候选缺少 Animation 类型: {genres}",
        local_value=target.show_type or "anime",
        remote_value=genres,
    )


def _target_prefers_anime(target: ScrapeTarget) -> bool:
    show_type = (target.show_type or "").casefold()
    if show_type.startswith("anime"):
        return True
    text = " ".join(
        str(value or "")
        for value in (
            target.media_type,
            target.series_group,
            target.local_title,
            target.original_title,
            target.source_subwork_dir,
            target.target_dir,
        )
    ).casefold()
    return any(keyword in text for keyword in ("动画", "番剧", "anime"))


def _detail_has_animation_genre(detail: dict) -> bool:
    names = {_normalize_genre_name(name) for name in _detail_genre_names(detail)}
    if names.intersection({"animation", "anime", "动画", "アニメ"}):
        return True
    genre_ids = {
        int(value)
        for value in (detail.get("genre_ids") or [])
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    }
    for genre in detail.get("genres") or []:
        genre_id = genre.get("id") if isinstance(genre, dict) else None
        if isinstance(genre_id, int):
            genre_ids.add(genre_id)
    return 16 in genre_ids


def _detail_genre_names(detail: dict) -> list[str]:
    names: list[str] = []
    for genre in detail.get("genres") or []:
        if isinstance(genre, dict):
            name = genre.get("name")
            if name:
                names.append(str(name))
        elif genre:
            names.append(str(genre))
    return names


def _normalize_genre_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


def _validate_tv_structure(
    target: ScrapeTarget,
    detail: dict,
    tmdb_season_number: Optional[int],
    tmdb_client: Optional[Any],
) -> list[ScrapeValidationIssue]:
    issues: list[ScrapeValidationIssue] = []
    local_count = target.local_episode_count or len(target.item_ids or [])
    season_number = 0 if target.group_type == "special" else (tmdb_season_number or target.local_season_number or 1)

    remote_count = _season_episode_count_from_detail(detail, season_number)
    if remote_count is None and tmdb_client and detail.get("id") is not None:
        try:
            get_season = (
                getattr(tmdb_client, "get_tv_season_episodes", None)
                or tmdb_client.get_tv_season_detail
            )
            season_detail = get_season(detail["id"], season_number)
            episodes = season_detail.get("episodes") or []
            remote_count = len(episodes) if episodes else None
        except Exception:
            remote_count = None

    if local_count and remote_count:
        severity = (
            ""
            if (
                _matches_complete_tmdb_season_prefix(target, detail, season_number, local_count)
                or _is_contiguous_local_episode_prefix(
                    target,
                    season_number,
                    local_count,
                    remote_count,
                )
            )
            else _episode_count_severity(local_count, remote_count, target.group_type)
        )
        if (
            severity == "block"
            and _looks_like_absolute_numbered_tmdb_season(target, detail, season_number, local_count, remote_count)
        ):
            severity = "warn"
        # 显式 TMDB ID 是用户或目录提供的强绑定。目录可能只收录拆分季度的
        # 某一部分（例如后半季），此时集数差异不能否定已确认的作品身份；保留
        # 警告以提示分集 NFO 可能不完整，但仍允许写入系列元数据和前台卡片。
        if (
            severity == "block"
            and target.tmdb_hint_id
            and detail.get("id") == target.tmdb_hint_id
        ):
            severity = "warn"
        if severity:
            issues.append(ScrapeValidationIssue(
                code="episode_count_mismatch",
                level=severity,
                message=f"集数差异过大: 本地 {local_count} 集 / 刮削 {remote_count} 集",
                local_value=local_count,
                remote_value=remote_count,
            ))

    if target.group_type == "movie":
        issues.append(ScrapeValidationIssue(
            code="movie_bound_to_tv",
            level="block",
            message="电影目标不能绑定到 TV 元数据",
            local_value=target.group_type,
            remote_value="tv",
        ))

    return issues


def _season_episode_count_from_detail(detail: dict, season_number: int) -> Optional[int]:
    for season in detail.get("seasons") or []:
        if season.get("season_number") == season_number:
            count = season.get("episode_count")
            return int(count) if count is not None else None
    return None


def _matches_complete_tmdb_season_prefix(
    target: ScrapeTarget,
    detail: dict,
    season_number: int,
    local_count: int,
) -> bool:
    """识别本地绝对编号恰好收录到某个 TMDB 季度边界的情况。"""
    if target.group_type != "season" or season_number != 1 or local_count <= 0:
        return False
    seasons = sorted(
        (
            (int(season.get("season_number") or 0), int(season.get("episode_count") or 0))
            for season in detail.get("seasons") or []
            if int(season.get("season_number") or 0) > 0
            and int(season.get("episode_count") or 0) > 0
        ),
        key=lambda item: item[0],
    )
    if len(seasons) < 2:
        return False
    if [season_number for season_number, _ in seasons] != list(range(1, len(seasons) + 1)):
        return False
    cumulative = 0
    for _, episode_count in seasons:
        cumulative += episode_count
        if local_count == cumulative:
            return True
    return False


def _is_contiguous_local_episode_prefix(
    target: ScrapeTarget,
    season_number: int,
    local_count: int,
    remote_count: int,
) -> bool:
    """本地从第 1 集连续收录到中途时，不把尚未保存的后续内容当成错配。"""
    if (
        target.group_type != "season"
        or season_number != 1
        or local_count <= 0
        or local_count >= remote_count
        or not target.import_plan_id
    ):
        return False
    try:
        from app.import_plan.store import load_import_plan

        plan = load_import_plan(plan_id=target.import_plan_id)
    except Exception:
        return False
    if not plan:
        return False
    item_ids = set(target.item_ids or [])
    numbers = {
        int(item.episode_number)
        for item in plan.items
        if item.id in item_ids
        and item.group_type == "season"
        and (item.season_number or 1) == (target.local_season_number or 1)
        and item.episode_number is not None
    }
    return (
        len(numbers) == local_count
        and numbers == set(range(1, local_count + 1))
    )


def _episode_count_severity(local_count: int, remote_count: int, group_type: str) -> str:
    if local_count <= 0 or remote_count <= 0:
        return ""
    diff = abs(local_count - remote_count)
    ratio = local_count / remote_count
    if group_type == "special":
        # Season 0 often contains many unrelated specials, so mismatch is useful
        # as a warning but should not block by count alone.
        return "warn" if diff >= 3 and (ratio < 0.5 or ratio > 1.8) else ""
    if diff <= 1:
        return ""
    # 本地媒体库可以合法地只保存部分剧集，在线条目也可能落后于新番进度。
    # 数量差异只负责提示，不能否定已经确认的作品身份。
    if diff >= 2 and (ratio < 0.8 or ratio > 1.2):
        return "warn"
    return ""


def _looks_like_absolute_numbered_tmdb_season(
    target: ScrapeTarget,
    detail: dict,
    season_number: int,
    local_count: int,
    remote_count: int,
) -> bool:
    """判断 TMDB 是否可能把多个本地季度合并成一个绝对编号 Season 1，
    或者 TMDB Season 1 的 episode_count 被错误标注为多季总和。"""
    if target.group_type != "season":
        return False
    if season_number != 1:
        return False
    if remote_count <= local_count:
        return False
    if local_count < 8:
        return False

    non_special_seasons = [
        season
        for season in detail.get("seasons") or []
        if int(season.get("season_number") or 0) > 0
    ]

    # 情况 1：TMDB 只有一个 non-special season（绝对编号）
    if len(non_special_seasons) == 1:
        if int(non_special_seasons[0].get("season_number") or 0) != 1:
            return False
        # 有些动漫条目（例如把多季正片全放进 TMDB Season 1 的条目）
        # 不一定是 local_count 的整数倍，但明确 TMDB hint 命中时仍应走
        # 绝对集数映射，而不是把正确条目挡进人工确认。
        if target.tmdb_hint_id and detail.get("id") == target.tmdb_hint_id:
            return True
        # remote 是 local 的整数倍（如 12 vs 24、25 vs 50）才视为绝对编号合并
        # 8 vs 13 等非整除差异仍然是 block
        if remote_count % local_count == 0 and remote_count // local_count >= 2:
            return True
        return False

    # 情况 2：TMDB 有多个 season，但 Season 1 的 episode_count 可能是多季总和
    # 例如：本地 12 集 vs TMDB Season 1 24 集，且 TMDB 还有 Season 2（各 12 集）
    if remote_count % local_count == 0:
        multiplier = remote_count // local_count
        if multiplier >= 2 and multiplier == len(non_special_seasons):
            return True

    return False


def _normalize_title(value: str) -> str:
    value = (value or "").casefold()
    value = re.sub(r"\{?\[?\s*(?:tmdb|tmdbid)\s*[-_=：:]?\s*\d+\s*[\}\]]?", " ", value)
    value = re.sub(r"\b(?:season|s)\s*\d+\b", " ", value)
    value = re.sub(r"第\s*\d+\s*季", " ", value)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)
