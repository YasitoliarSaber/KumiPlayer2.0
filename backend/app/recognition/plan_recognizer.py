# -*- coding: utf-8 -*-
"""ImportPlan 媒体结构识别入口

在 M03 draft ImportPlan 基础上，为视频条目填充媒体结构字段。
只处理 resource_type=video 且 action=generate_strm 的条目。
非视频条目保持 M03 结果，不参与媒体识别。
"""

import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.import_plan.placement_validator import validate_import_plan_placement
from app.library.identity import library_card_identity
from app.recognition.media import MediaGuess, recognize_media


def _effective_source(item: ImportPlanItem) -> str:
    """识别层使用真实内容提供商（provider_id），兼容旧 source 字段。

    OpenList 导入的百度/115 内容 provider_id=baidu/pan115，识别必须走
    对应提供商的规则，不能因为 ingest_method=openlist_api 就退化为通用分支。
    """
    return item.provider_id or item.source


def _apply_guess_to_item(item: ImportPlanItem, guess: MediaGuess) -> None:
    """将 MediaGuess 结果应用到 ImportPlanItem"""
    if guess.group_type == "ignored":
        item.action = "ignore"
    elif guess.group_type == "special":
        guess.season_number = 0
        guess.episode_number = None
        guess.media_type = guess.media_type or "tv"
    elif guess.group_type == "auxiliary":
        guess.season_number = 0
        guess.episode_number = None
        guess.media_type = guess.media_type or "tv"
    elif not guess.group_type and guess.media_type == "movie":
        guess.group_type = "movie"
        guess.card_type = guess.card_type or "standalone"
    elif guess.group_type == "movie":
        guess.media_type = guess.media_type or "movie"
        guess.card_type = guess.card_type or "standalone"

    if guess.group_type == "season" and guess.tmdb_hint_id and guess.episode_number is not None:
        from app.recognition.verified_titles import match_verified_tmdb_episode_placement

        placement = match_verified_tmdb_episode_placement(
            int(guess.tmdb_hint_id), item.relative_path, int(guess.episode_number)
        )
        if placement and placement != (guess.season_number, guess.episode_number):
            guess.season_number, guess.episode_number = placement
            guess.reasons.append(
                f"已核实 TMDB 篇章映射为第{guess.season_number}季第{guess.episode_number}集"
            )

    item.work_id = guess.work_id
    item.work_title = guess.work_title
    item.original_title = guess.original_title
    item.year = guess.year
    item.media_type = guess.media_type
    item.show_type = _infer_show_type(item, guess)
    item.tmdb_hint_id = guess.tmdb_hint_id
    item.tmdb_hint_type = guess.tmdb_hint_type
    item.series_group = guess.series_group
    item.card_type = guess.card_type
    item.belongs_to_series = guess.belongs_to_series
    item.relation_type = guess.relation_type
    item.group_type = guess.group_type
    item.season_number = guess.season_number
    item.episode_number = guess.episode_number
    item.special_number = guess.special_number
    item.title = guess.title

    # 置信度合并：取 M03 和 M04 中较低的
    item.confidence = _merge_confidence(item.confidence, guess.confidence)
    item.needs_review = item.needs_review or guess.needs_review

    # 追加 reasons 和 warnings
    item.reasons.extend(guess.reasons)
    item.warnings.extend(guess.warnings)


def _merge_confidence(existing: str, new: str) -> str:
    """合并两个置信度，取较低的

    priority: low < medium < high
    """
    priority = {"low": 0, "medium": 1, "high": 2}
    existing_val = priority.get(existing, 1)
    new_val = priority.get(new, 1)
    merged_val = min(existing_val, new_val)
    for k, v in priority.items():
        if v == merged_val:
            return k
    return "medium"


def _infer_show_type(item: ImportPlanItem, guess: MediaGuess) -> str:
    """按导入目录语义决定首页大类，不依赖刮削结果。"""
    media_type = guess.media_type or ("movie" if guess.group_type == "movie" else "tv")
    parts = [p.strip() for p in item.relative_path.replace("\\", "/").split("/") if p.strip()]
    category = _find_category_segment(parts, _effective_source(item))

    if category in {"动画电影", "电影"}:
        media_type = "movie"
    elif media_type != "movie" and category in {"动画", "新番", "刮削好的动画", "番剧", "TV", "TV动画", "剧集"}:
        media_type = "tv"

    import_family = (getattr(item, "import_family", "") or "").strip().lower()

    if import_family == "anime":
        return "anime_movie" if media_type == "movie" else "anime_series"
    if import_family == "live":
        return "live_movie" if media_type == "movie" else "live_series"

    if category in {"动画电影"}:
        return "anime_movie"
    if category in {"动画", "新番", "刮削好的动画", "番剧", "TV", "TV动画"}:
        return "anime_movie" if media_type == "movie" else "anime_series"
    if category == "剧集":
        return "live_series"
    if category == "电影":
        return "live_movie"

    # 本地默认路径是动画库；没有分类层时按动画域处理。
    if _effective_source(item) == "local":
        return "anime_movie" if media_type == "movie" else "anime_series"

    # 百度目录树常从用户选中的动画根目录开始导出，relative_path 可能直接就是作品名。
    # 当前没有保存“本次导入选择的是动画库/影视库”的显式字段，先按现有百度动画库流程兜底。
    if _effective_source(item) == "baidu":
        return "anime_movie" if media_type == "movie" else "anime_series"

    return "live_movie" if media_type == "movie" else "live_series"


def _find_category_segment(parts: list[str], source: str) -> str:
    categories = {"动画", "新番", "刮削好的动画", "动画电影", "电影", "剧集", "番剧", "TV", "TV动画"}
    if source == "local":
        for part in parts[:2]:
            if part in categories:
                return part
        return ""
    return parts[0] if parts and parts[0] in categories else ""


def recognize_import_plan_media(plan: ImportPlan) -> ImportPlan:
    """为 ImportPlan 中的视频条目填充媒体结构字段

    只处理 resource_type=video 且 action=generate_strm 的条目。
    非视频条目保持 M03 结果不变。
    原地修改 plan.items 中的条目。

    参数:
        plan: M03 生成的 draft ImportPlan

    返回:
        同一个 ImportPlan，视频条目已填充媒体结构字段
    """
    for item in plan.items:
        # 只处理视频
        if item.resource_type != "video" or item.action != "generate_strm":
            continue

        if not item.import_family:
            item.import_family = plan.import_family

        if _is_skipped_bonus_directory(item.relative_path):
            _mark_skipped_bonus_item(item)
            continue
        if _is_ambiguous_special_container_item(item.relative_path):
            _mark_skipped_bonus_item(item)
            continue

        guess = recognize_media(
            filename=_extract_filename(item.relative_path),
            relative_path=item.relative_path,
            source=_effective_source(item),
            root_container=plan.root_container,
        )
        _apply_guess_to_item(item, guess)
        if item.group_type == "auxiliary":
            _mark_skipped_auxiliary_item(item)

    # 同一电影目录里只能有一个作品卡片；PV、菜单等附属视频挂到主电影但不进入特别篇。
    _fold_movie_folder_extras(plan)
    _normalize_special_titles(plan)
    _normalize_explicit_later_season_episode_numbers(plan)
    _move_implicit_season_collision_to_specials(plan)

    # 重复集号自动收口：保留一个最像正片的版本，其余跳过，减少人工确认。
    _auto_resolve_duplicate_episodes(plan)

    # 归位质检：识别结果与原始路径证据冲突时，强制进入人工确认
    validate_import_plan_placement(plan, mutate=True)

    return plan


def _normalize_explicit_later_season_episode_numbers(plan: ImportPlan) -> None:
    """把明确后续季目录中的连续绝对集号转换为季内集号。"""
    groups: dict[tuple[str, str, int, str], list[ImportPlanItem]] = defaultdict(list)
    for item in _video_items(plan):
        season = item.season_number or 0
        if item.group_type != "season" or season <= 1 or item.episode_number is None:
            continue
        path = (item.relative_path or "").replace("\\", "/")
        if not re.search(rf"(?:\[S0?{season}\]|(?:^|[\s._-])S0?{season}(?:[\s._-]|$))", path, re.IGNORECASE):
            continue
        parent = path.rsplit("/", 1)[0]
        groups[(item.source, item.work_id, season, parent)].append(item)

    for (_, _, season, _), items in groups.items():
        numbers = sorted({int(item.episode_number) for item in items if item.episode_number is not None})
        if len(numbers) < 2 or numbers[0] < 10:
            continue
        if numbers != list(range(numbers[0], numbers[-1] + 1)):
            continue
        offset = numbers[0] - 1
        for item in items:
            item.episode_number = int(item.episode_number) - offset
            item.reasons.append(
                f"明确第{season}季目录使用连续绝对集号，按季内第{item.episode_number}集归一化"
            )


def _move_implicit_season_collision_to_specials(plan: ImportPlan) -> None:
    """显式 Sxx 与编号推断季撞车时，将隐式篇章保守归入特别篇。"""
    groups: dict[tuple[str, int, int], list[ImportPlanItem]] = defaultdict(list)
    for item in _video_items(plan):
        if item.group_type != "season" or item.season_number is None or item.episode_number is None:
            continue
        card_identity = library_card_identity(item)
        groups[(card_identity, int(item.season_number), int(item.episode_number))].append(item)

    implicit_parents: set[tuple[str, str]] = set()
    for items in groups.values():
        if len(items) < 2:
            continue
        explicit = [item for item in items if _path_has_explicit_season(item)]
        implicit = [item for item in items if not _path_has_explicit_season(item)]
        if explicit and implicit:
            implicit_parents.update(
                (library_card_identity(item), _item_parent_path(item))
                for item in implicit
            )

    for item in _video_items(plan):
        item_parent = (library_card_identity(item), _item_parent_path(item))
        if item.group_type != "season" or item_parent not in implicit_parents:
            continue
        number = int(item.episode_number or 1)
        item.group_type = "special"
        item.season_number = 0
        item.special_number = number
        item.episode_number = None
        item.reasons.append("与明确 Sxx 正季冲突，保留为特别篇而不是重复正片")


def _path_has_explicit_season(item: ImportPlanItem) -> bool:
    path = item.relative_path or ""
    season = item.season_number or 0
    return bool(re.search(
        rf"(?:\[S0?{season}\]|(?:^|[\s._-])S0?{season}(?:E\d+|[\s._-]|$))",
        path,
        flags=re.IGNORECASE,
    ))


def _item_parent_path(item: ImportPlanItem) -> str:
    return (item.relative_path or "").replace("\\", "/").rsplit("/", 1)[0]


def _fold_movie_folder_extras(plan: ImportPlan) -> None:
    groups: dict[tuple[str, str], list[ImportPlanItem]] = defaultdict(list)
    for item in _video_items(plan):
        key = _movie_folder_key(item)
        if key:
            groups[key].append(item)

    for items in groups.values():
        movie_items = [item for item in items if item.group_type == "movie"]
        if not movie_items:
            continue
        if not any(_is_movie_context_item(item) for item in movie_items):
            continue

        primary = min(movie_items, key=_primary_movie_sort_key)
        has_regular_season = any(item.group_type == "season" for item in items)
        special_no = 1
        for item in sorted(items, key=lambda it: it.relative_path):
            if item is primary or (item.action != "generate_strm" and item.group_type != "auxiliary"):
                continue
            if item.group_type in {"season", "movie", "ignored", "op_ed"}:
                continue
            if has_regular_season and item.group_type == "special":
                continue
            if item.group_type == "special" and item.media_type == "tv" and item.tmdb_hint_type == "tv":
                continue

            _inherit_movie_identity(item, primary)
            item.season_number = 0
            item.episode_number = None
            if item.group_type == "auxiliary" or _looks_like_auxiliary_extra_file(item):
                item.group_type = "auxiliary"
                item.relation_type = "auxiliary"
                item.special_number = None
                item.title = _clean_extra_title(item, primary, None)
            else:
                item.group_type = "special"
                item.special_number = item.special_number or special_no
                item.title = _special_display_title_from_path(item) or _clean_extra_title(item, primary, item.special_number)
                special_no += 1
            item.confidence = _merge_confidence(item.confidence, "medium")
            item.needs_review = False
            item.warnings = [
                warning for warning in item.warnings
                if "group_type" not in warning and "还没判断出" not in warning and "分组" not in warning
            ]


def _normalize_special_titles(plan: ImportPlan) -> None:
    groups: dict[tuple[str, int], list[ImportPlanItem]] = defaultdict(list)
    for item in _video_items(plan):
        if item.group_type in {"special", "sps"}:
            groups[(library_card_identity(item), item.season_number or 0)].append(item)

    for items in groups.values():
        ordered_items = sorted(items, key=lambda it: it.relative_path)
        explicit_numbers = [
            int(item.special_number)
            for item in ordered_items
            if item.special_number is not None and int(item.special_number) > 0
        ]
        used_numbers: set[int] = set()
        next_number = max(explicit_numbers, default=0) + 1
        for item in ordered_items:
            number = int(item.special_number or 0)
            if number <= 0 or number in used_numbers:
                while next_number in used_numbers:
                    next_number += 1
                number = next_number
                next_number += 1
                item.special_number = number
            used_numbers.add(number)

        used: dict[str, int] = defaultdict(int)
        for item in ordered_items:
            title = (item.title or "").strip()
            preserved_title = _special_display_title_from_path(item)
            if preserved_title:
                title = preserved_title
            elif _is_generic_special_title(title, item):
                title = _clean_extra_title(item, item, item.special_number)
            key = re.sub(r"\s+", "", title).casefold()
            used[key] += 1
            if used[key] > 1:
                title = f"{title} {used[key]:02d}"
            item.title = title


def _video_items(plan: ImportPlan) -> list[ImportPlanItem]:
    return [
        item for item in plan.items
        if item.resource_type == "video"
        and (item.action == "generate_strm" or item.group_type == "auxiliary")
    ]


def _movie_folder_key(item: ImportPlanItem) -> tuple[str, str]:
    parts = _path_parts(item.relative_path)
    if len(parts) < 2:
        return ("", "")
    work_index = _source_work_index(parts, _effective_source(item))
    if work_index is None:
        return ("", "")
    key_parts = parts[:work_index + 1]
    if len(parts) >= work_index + 3:
        subwork_dir = parts[work_index + 1]
        if (
            not _is_skipped_bonus_dir_name(subwork_dir)
            and not _is_special_container_dir_name(subwork_dir)
            and not _is_plain_group_dir(subwork_dir)
        ):
            key_parts.append(subwork_dir)
    directory = "/".join(key_parts)
    return (item.source, directory.casefold())


def _path_parts(relative_path: str) -> list[str]:
    return [part.strip() for part in (relative_path or "").replace("\\", "/").split("/") if part.strip()]


def _is_skipped_bonus_directory(relative_path: str) -> bool:
    """Skip whole bonus folders so they cannot become cards, seasons, or scrape targets."""
    parts = _path_parts(relative_path)
    if len(parts) < 2:
        return False
    for dirname in parts[:-1]:
        if _is_skipped_bonus_dir_name(dirname):
            return True
    return False


def _is_skipped_bonus_dir_name(dirname: str) -> bool:
    normalized = re.sub(r"[\s._\-·:：/\\()（）【】\[\]]+", "", dirname).casefold()
    if normalized in {
        "cd", "cds",
        "menu", "menus",
        "bonus", "bonuses",
        "extra", "extras",
        "ncop", "nced", "oped", "opanded",
        "pv", "pvs", "cm", "cms",
        "trailer", "trailers",
        "eyecatch",
        "菜单", "预告", "花絮",
    }:
        return True
    return bool(re.fullmatch(r"(?:menus?|cds?|pvs?|cms?)\d+", normalized))


def _is_ambiguous_special_container_item(relative_path: str) -> bool:
    parts = _path_parts(relative_path)
    if len(parts) < 2:
        return False
    if not any(_is_special_container_dir_name(dirname) for dirname in parts[:-1]):
        return False
    return not _has_explicit_special_filename(parts[-1])


def _is_special_container_dir_name(dirname: str) -> bool:
    normalized = re.sub(r"[\s._\-·:：/\\()（）【】\[\]]+", "", dirname).casefold()
    return normalized in {"sp", "sps", "sps1", "special", "specials"} or bool(
        re.fullmatch(r"(?:sps?|specials?)\d+", normalized)
    )


def _has_explicit_special_filename(filename: str) -> bool:
    text = filename or ""
    patterns = (
        r"\bS00(?:E\d+)?\b",
        r"\b(?:OVA|OAD|SP)\s*\d*\b",
        r"\[(?:OVA|OAD|SP)\s*\d*\]",
        r"\d+\.5",
        r"(?:^|[\s._\-\[])(?:00)(?:$|[\s._\-\]【\[])",
        r"番外|短篇|小剧场|特典",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _is_plain_group_dir(dirname: str) -> bool:
    normalized = dirname.strip()
    return bool(
        re.fullmatch(r"Season\s*\d+", normalized, flags=re.IGNORECASE)
        or re.fullmatch(r"S\d+", normalized, flags=re.IGNORECASE)
        or re.fullmatch(r"第\s*\d+\s*季", normalized)
    )


def _mark_skipped_bonus_item(item: ImportPlanItem) -> None:
    item.action = "ignore"
    item.group_type = "auxiliary"
    item.media_type = "tv"
    item.show_type = "anime_series" if item.import_family != "live" else "live_series"
    item.card_type = "main_series"
    item.season_number = None
    item.episode_number = None
    item.special_number = None
    item.title = _extract_filename(item.relative_path)
    item.confidence = _merge_confidence(item.confidence, "high")
    reason = "位于菜单/CD/PV/CM/OPED 等附属目录或附属视频，跳过导入与刮削"
    if reason not in item.reasons:
        item.reasons.append(reason)


def _mark_skipped_auxiliary_item(item: ImportPlanItem) -> None:
    item.action = "ignore"
    item.group_type = "auxiliary"
    item.media_type = item.media_type or "tv"
    item.show_type = item.show_type or ("anime_series" if item.import_family != "live" else "live_series")
    item.card_type = item.card_type or "main_series"
    item.season_number = 0
    item.episode_number = None
    item.special_number = None
    reason = "附属视频不生成镜像和刮削目标"
    if reason not in item.reasons:
        item.reasons.append(reason)


def _source_work_index(parts: list[str], source: str) -> Optional[int]:
    if not parts:
        return None
    if source == "local":
        return 0 if len(parts) >= 2 else None
    if parts[0] in {"动画", "新番", "刮削好的动画", "动画电影", "电影", "剧集", "番剧", "TV", "TV动画"}:
        return 1 if len(parts) >= 2 else None
    return 0 if len(parts) >= 2 else None


def _is_movie_context_item(item: ImportPlanItem) -> bool:
    category = _find_category_segment(_path_parts(item.relative_path), _effective_source(item))
    return (
        item.media_type == "movie"
        or item.show_type in {"anime_movie", "live_movie"}
        or category in {"动画电影", "电影"}
    )


def _primary_movie_sort_key(item: ImportPlanItem) -> tuple[int, int, str]:
    return (
        1 if _looks_like_extra_file(item) else 0,
        len(_filename_stem(item)),
        item.relative_path,
    )


def _looks_like_extra_file(item: ImportPlanItem) -> bool:
    text = _filename_stem(item)
    return bool(re.search(
        r"(?i)(?:^|[\s._\-\[\(])(?:PV|CM|MENU|TRAILER|SP|OVA|OAD|NCOP|NCED|M\d+)(?:\d+)?(?:$|[\s._\-\]\)])"
        r"|番外|特典|映像|菜单|预告|花絮",
        text,
    ))


def _looks_like_auxiliary_extra_file(item: ImportPlanItem) -> bool:
    text = _filename_stem(item)
    return bool(re.search(
        r"(?i)(?:^|[\s._\-\[\(])(?:PV|CM|MENU|TRAILER|EYECATCH|NCOP|NCED)(?:\d+)?(?:$|[\s._\-\]\)])"
        r"|菜单|预告|花絮",
        text,
    ))


def _inherit_movie_identity(item: ImportPlanItem, primary: ImportPlanItem) -> None:
    item.work_id = primary.work_id
    item.work_title = primary.work_title
    item.original_title = primary.original_title
    item.year = primary.year
    item.media_type = primary.media_type
    item.show_type = primary.show_type
    item.tmdb_hint_id = primary.tmdb_hint_id
    item.tmdb_hint_type = primary.tmdb_hint_type
    item.series_group = primary.series_group
    item.card_type = primary.card_type
    item.belongs_to_series = primary.belongs_to_series
    item.relation_type = "auxiliary" if item.group_type == "auxiliary" else "special"


def _clean_extra_title(item: ImportPlanItem, primary: ImportPlanItem, fallback_number: Optional[int]) -> str:
    title = _filename_stem(item)
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)
    title = title.replace("_", " ")

    for value in {
        primary.work_title,
        primary.original_title,
        primary.series_group,
        _container_title(item.relative_path),
        _clean_primary_filename_title(primary) if primary is not item else "",
    }:
        if value:
            title = re.sub(re.escape(value), " ", title, flags=re.IGNORECASE)

    title = _remove_technical_brackets(title)
    title = _remove_technical_tokens(title)
    title = re.sub(r"[-_.]+", " ", title)
    title = " ".join(title.split()).strip(" -_.")

    marker = _extract_extra_marker(_filename_stem(item))
    if marker and (not title or len(title) < len(marker)):
        title = marker
    if not title:
        title = marker or f"特别篇 {fallback_number or 1:02d}"
    return title


def _special_display_title_from_path(item: ImportPlanItem) -> str:
    """Return a display title for specials that preserves the source filename.

    Specials are user-curated extras, so their local names are often more useful
    than aggressive scraper-oriented cleanup. We only strip leading release-group
    bracket blocks such as "[MAI]" or "【VCB-Studio】".
    """
    title = _filename_stem(item).strip()
    if not title:
        return ""
    while True:
        cleaned = re.sub(r"^\s*(?:\[[^\]]+\]|【[^】]+】)\s*", "", title, count=1)
        if cleaned == title:
            break
        title = cleaned.strip()
    return title


def _clean_primary_filename_title(item: ImportPlanItem) -> str:
    title = _filename_stem(item)
    title = re.sub(r"^\[[^\]]+\]\s*", "", title)
    title = _remove_technical_brackets(title)
    title = _remove_technical_tokens(title)
    title = re.sub(r"[-_.]+", " ", title)
    return " ".join(title.split()).strip(" -_.")


def _filename_stem(item: ImportPlanItem) -> str:
    filename = _extract_filename(item.relative_path)
    return Path(filename).stem if filename else ""


def _container_title(relative_path: str) -> str:
    parts = _path_parts(relative_path)
    if len(parts) < 2:
        return ""
    container = parts[-2]
    container = re.sub(r"[.．]\d{4}$", "", container).strip()
    container = re.sub(r"[(（]\d{4}[)）]$", "", container).strip()
    return container


def _remove_technical_brackets(title: str) -> str:
    def repl(match: re.Match) -> str:
        token = match.group(1)
        return " " if _is_technical_token(token) else f" {token} "

    return re.sub(r"[\[\(【（]([^\]\)】）]+)[\]\)】）]", repl, title)


def _remove_technical_tokens(title: str) -> str:
    tokens = []
    for token in re.split(r"\s+", title):
        if token and not _is_technical_token(token):
            tokens.append(token)
    return " ".join(tokens)


def _is_technical_token(token: str) -> bool:
    normalized = re.sub(r"[^A-Za-z0-9一-龥]+", " ", token or "").strip().casefold()
    if not normalized:
        return True
    technical_words = {
        "bdrip", "webdl", "web dl", "webrip", "bdmv", "remux", "hevc", "avc",
        "h264", "h265", "x264", "x265", "flac", "aac", "opus", "ass", "softsub",
        "chs", "cht", "jpn", "eng", "1080p", "2160p", "720p", "480p", "10bit",
        "8bit", "hi10p", "ma10p", "mkv", "mp4", "bd", "dvd",
    }
    if normalized in technical_words:
        return True
    return bool(re.fullmatch(
        r"(?i)(?:ma\d+p.*|\d{3,4}p|x26[45].*|hevc.*|avc.*|flac.*|aac.*|opus.*|"
        r"chs[-_\s]?jpn|chs[-_\s]?cht|softsub|bd(?:rip)?|web[-_\s]?dl|10bit|8bit)",
        normalized,
    ))


def _extract_extra_marker(title: str) -> str:
    match = re.search(r"(?i)(?:^|[\s._\-\[\(])((?:PV|CM|SP|OVA|OAD|MENU|M)\s*\d*)(?:$|[\s._\-\]\)])", title)
    if match:
        return match.group(1).replace(" ", "").upper()
    if re.search(r"番外|特典|映像|菜单|预告|花絮", title):
        return re.search(r"番外|特典|映像|菜单|预告|花絮", title).group(0)
    return ""


def _is_generic_special_title(title: str, item: ImportPlanItem) -> bool:
    if not title:
        return True
    normalized = re.sub(r"\s+", "", title).casefold()
    generic_values = [
        item.work_title,
        item.original_title,
        item.series_group,
        _container_title(item.relative_path),
    ]
    return normalized in {
        re.sub(r"\s+", "", value or "").casefold()
        for value in generic_values
        if value
    }


def _auto_resolve_duplicate_episodes(plan: ImportPlan) -> None:
    """自动处理同一作品目录、季度和集号下的重复 season 条目。

    默认导入应该尽量向前走。重复正片保留一个最像正片/质量最高的版本，
    其余改为 ignore，避免同一集生成多个镜像文件和重复卡片。若用户发现
    保留版本不理想，再用具体样本反向调整评分规则。
    """
    from collections import defaultdict

    season_map: dict = defaultdict(list)
    for item in plan.items:
        if (item.resource_type == "video"
                and item.action == "generate_strm"
                and item.group_type == "season"
                and item.season_number is not None
                and item.episode_number is not None):
            # 作品维度用 library_card_identity：它对目录树 TXT（路径段）与
            # OpenList（无分类层时回退识别身份）都稳定。
            key = (library_card_identity(item), item.season_number, item.episode_number)
            season_map[key].append(item)

    for key, items in season_map.items():
        if len(items) <= 1:
            continue
        _, sn, en = key
        keep = max(items, key=_duplicate_episode_keep_score)
        for item in items:
            if item is keep:
                warning = f"同一季集存在多个来源文件（{len(items)} 个），已自动保留此版本"
                if warning not in item.warnings:
                    item.warnings.append(warning)
                continue
            item.action = "ignore"
            item.group_type = "ignored"
            item.season_number = None
            item.episode_number = None
            item.special_number = None
            item.needs_review = False
            item.title = item.title or f"S{sn:02d}E{en:02d} 重复版本"
            warning = f"同一季集存在多个来源文件（{len(items)} 个），已自动跳过重复版本"
            if warning not in item.warnings:
                item.warnings.append(warning)


def _duplicate_episode_keep_score(item: ImportPlanItem) -> tuple[int, int, int, int, int, str]:
    stem = _filename_stem(item)
    text = f"{item.relative_path} {stem}"
    return (
        0 if _looks_like_extra_file(item) else 1,
        _resolution_score(text),
        _codec_score(text),
        _title_match_score(item),
        -len(item.relative_path or ""),
        item.relative_path,
    )


def _resolution_score(text: str) -> int:
    match = re.search(r"(?i)(2160p|1080p|720p|480p)", text or "")
    if not match:
        return 0
    return {"2160p": 4, "1080p": 3, "720p": 2, "480p": 1}.get(match.group(1).lower(), 0)


def _codec_score(text: str) -> int:
    normalized = (text or "").casefold()
    score = 0
    if "hevc" in normalized or "x265" in normalized or "h265" in normalized:
        score += 2
    if "flac" in normalized:
        score += 1
    return score


def _title_match_score(item: ImportPlanItem) -> int:
    title = re.sub(r"\s+", "", item.work_title or "").casefold()
    stem = re.sub(r"\s+", "", _filename_stem(item)).casefold()
    if title and title in stem:
        return 2
    if item.original_title:
        original = re.sub(r"\s+", "", item.original_title).casefold()
        if original and original in stem:
            return 1
    return 0


def _extract_filename(relative_path: str) -> str:
    """从 relative_path 提取文件名"""
    parts = relative_path.replace("\\", "/").split("/")
    return parts[-1] if parts else ""
