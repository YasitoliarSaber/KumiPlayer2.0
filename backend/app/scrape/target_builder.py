# -*- coding: utf-8 -*-
"""ScrapeTarget 生成器

从 confirmed/executed ImportPlan 生成可刮削目标。
生成 season、special 和 movie 目标；不生成 OP/ED/附属视频。
"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.import_plan.models import ImportPlan, ImportPlanItem
from app.library.identity import library_card_identity
from app.scrape.models import ScrapeTarget


def _make_target_id(
    source: str, plan_id: str, canonical: str, series_group: str,
    local_title: str, group_type: str,
    local_season_number: Optional[int], scrape_type: str,
    card_identity: str,
) -> str:
    """生成稳定的 scrape_target_id（CP2：聚合键必须包含 canonical ownership，
    不同 canonical work 即使其他键相同也不会得到同一个 target id）"""
    parts = [
        source, canonical, series_group, local_title,
        group_type, str(local_season_number or ""), scrape_type, card_identity,
    ]
    content = ":".join(parts)
    return hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]


def _extract_subwork_dir(item: ImportPlanItem) -> str:
    """从 item 的 reasons/warnings 提取子作品目录"""
    for text in item.reasons + item.warnings:
        if "子作品目录:" in text:
            return text.replace("子作品目录:", "").strip()
    return ""


def _clean_scrape_title(title: str, series_group: str = "") -> str:
    """清理搜索标题：去掉 [S01]、S2、字幕组标签等"""
    if not title:
        return ""
    cleaned = title
    cleaned = _strip_tmdb_hint(cleaned)
    # 去掉目录树分区字母前缀：B 86-不存在的战区 -> 86-不存在的战区
    # 只处理单字母 + 数字标题，避免误伤 A Channel / K-ON! 等真实标题。
    cleaned = re.sub(r"^[A-Za-z]\s+(?=\d)", "", cleaned).strip()
    # 去掉 [S01] / [S1] 等
    cleaned = re.sub(r"\[S\d+\]", "", cleaned, flags=re.IGNORECASE)
    # 去掉标题末尾紧贴的季号：虫师S1 / 虫师 S01 / 虫师 第1季。
    cleaned = re.sub(r"\s*(?:S|Season)\s*\d+\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*第\s*\d+\s*季\s*$", "", cleaned)
    # 去掉独立的 S2 / S02（前面是空格或开头，后面是结尾或空格）
    cleaned = re.sub(r"(?:^|\s)S\d+(?:\s|$)", " ", cleaned, flags=re.IGNORECASE)
    # 去掉方括号技术标签
    cleaned = re.sub(r"\[[^\]]*\]", "", cleaned)
    # 去掉残留未闭合方括号
    cleaned = re.sub(r"\[[^\]]*$", "", cleaned)
    # 去掉末尾年份，避免 "Title.[S02].2008" 清成 "Title.2008"
    cleaned = re.sub(r"[.．]\d{4}$", "", cleaned)
    cleaned = re.sub(r"[(\（]\d{4}[)\）]$", "", cleaned)
    cleaned = re.sub(r"\s\d{4}$", "", cleaned)
    # 连续点号归一化为单个点号
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    # 去掉末尾年份后残留的点号（如 "Title.2008" 清理年份后变成 "Title."）
    cleaned = re.sub(r"\.\s*$", "", cleaned)
    # 统一数字与中文标题之间的连接符，提升 TMDB 搜索宽容度。
    cleaned = re.sub(r"(?<=\d)\s*[-－—–]\s*(?=[\u4e00-\u9fff])", " ", cleaned)
    # 清理空格和尾部点号
    cleaned = " ".join(cleaned.split()).strip(" .")
    return cleaned if cleaned else title


def _strip_tmdb_hint(title: str) -> str:
    from app.scrape.tmdb_hint import strip_tmdb_hint

    return strip_tmdb_hint(title)


def _extract_tmdb_hint(title: str) -> Optional[int]:
    from app.scrape.tmdb_hint import extract_tmdb_hint

    return extract_tmdb_hint(title)


def _strip_sequence_prefix(title: str) -> str:
    """Remove local ordering prefixes such as 4. from a subwork title."""
    return re.sub(r"^\d+[.．]\s*", "", title or "").strip()


def _remove_year_suffix(title: str) -> str:
    """Remove common trailing year forms from a title."""
    cleaned = title or ""
    cleaned = re.sub(r"[.．]\d{4}$", "", cleaned).strip()
    cleaned = re.sub(r"[(\（]\d{4}[)\）]$", "", cleaned).strip()
    cleaned = re.sub(r"\s\d{4}$", "", cleaned).strip()
    return cleaned


def _specific_movie_title(item: ImportPlanItem, series_group: str, subwork_dir: str) -> str:
    """Build a movie search title with the concrete movie subtitle retained.

    Standalone movies inside a series often have work_title=series_group
    (e.g. 刀剑神域), while item.title/subwork_dir contains the actual movie
    subtitle. Movie scraping must use the concrete title, otherwise several
    one-episode movies can all match the TV show or the same generic movie.
    """
    raw_candidates = [
        item.title,
        subwork_dir,
        _movie_filename_title(item),
        item.original_title,
        item.work_title,
    ]
    for raw in raw_candidates:
        raw = _strip_tmdb_hint(_remove_year_suffix(_strip_sequence_prefix(raw or "")))
        if raw and not _is_generic_title(raw):
            break
    else:
        raw = item.work_title or series_group or item.original_title or ""

    cleaned = _clean_scrape_title(raw, series_group)
    series_key = _title_containment_key(series_group)
    cleaned_key = _title_containment_key(cleaned)
    if (
        series_group
        and series_key
        and series_key not in cleaned_key
        and not series_group.strip().startswith("[")
    ):
        # 目录常写成“剧场版：序列之争”，搜索时补上系列名更稳。
        cleaned = f"{series_group} {cleaned}"

    # “刀剑神域 剧场版：序列之争”比单独“刀剑神域”安全得多；
    # 冒号保留给 TMDB 也可用，空格归一化即可。
    return " ".join(cleaned.split()).strip(" .")


def _title_containment_key(title: str) -> str:
    """用于判断标题包含关系，忽略下划线、空格和标点。"""
    return re.sub(r"[\W_]+", "", (title or "").casefold(), flags=re.UNICODE)


def _is_generic_title(title: str) -> bool:
    """Return True for non-searchable folder labels such as Season 1."""
    normalized = " ".join((title or "").replace("_", " ").split()).strip().lower()
    return normalized in {
        "",
        "season",
        "season 0",
        "season 1",
        "season 2",
        "season 3",
        "season 4",
        "season 5",
        "s0",
        "s1",
        "s2",
        "s3",
        "s4",
        "s5",
        "tv",
        "movie",
        "movies",
        "special",
        "specials",
    }


def _movie_filename_title(item: ImportPlanItem) -> str:
    """Extract a concrete movie title from the video filename.

    This is mainly for bracket-heavy release folders, e.g.
    [BeanSub&FZSD][Jujutsu_Kaisen][...]/[Jujutsu_Kaisen_0][MOVIE]...
    where the work container is not the movie title.
    """
    filename = (item.relative_path or "").replace("\\", "/").split("/")[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", filename)
    if not stem:
        return ""

    for segment in re.findall(r"\[([^\]]+)\]", stem):
        candidate = _clean_movie_filename_fragment(segment)
        if candidate and not _is_technical_movie_fragment(candidate) and not _is_generic_title(candidate):
            return candidate

    stripped = re.sub(r"\[[^\]]+\]", " ", stem)
    stripped = re.sub(r"\([^)]{6,}\)", " ", stripped)
    stripped = _clean_movie_filename_fragment(stripped)
    return "" if _is_technical_movie_fragment(stripped) else stripped


def _clean_movie_filename_fragment(text: str) -> str:
    cleaned = (text or "").replace("_", " ")
    cleaned = re.sub(r"\b(?:BDRip|BluRay|WEB[- ]?DL|WebRip|HEVC|AVC|AAC|FLAC|x26[45]|H\.?26[45])\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:CHS|CHT|JPN|GB|BIG5|MP4|MKV|Ma\d+p|Hi10P)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b\d{3,4}p\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:8bit|10bit|BD|DVD|UHD|HDR|SDR)\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"\([A-Fa-f0-9]{6,}\)", " ", cleaned)
    cleaned = _remove_year_suffix(cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" .-_")


def _is_technical_movie_fragment(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return True
    noise = {
        "movie", "movies", "bdrip", "bluray", "web-dl", "webrip",
        "chs", "cht", "mp4", "mkv", "avc aac", "hevc",
        "beansub&fzsd", "beansub", "fzsd",
    }
    if normalized in noise:
        return True
    return not re.search(r"[A-Za-z\u4e00-\u9fff]", normalized)


def _choose_local_title(item: ImportPlanItem) -> str:
    """Pick a user/search-facing local title without season-folder noise."""
    if item.group_type == "movie":
        subwork_dir = _extract_subwork_dir(item)
        movie_title = _specific_movie_title(item, item.series_group, subwork_dir)
        if movie_title and not _is_generic_title(movie_title):
            return movie_title

    candidates = [
        item.work_title,
        item.series_group,
        item.original_title,
    ]
    for candidate in candidates:
        cleaned = _clean_scrape_title(candidate or "", item.series_group)
        if cleaned and not _is_generic_title(cleaned):
            return cleaned
    return item.series_group or item.work_title or item.original_title or ""


def _extract_scrape_year(item: ImportPlanItem, subwork_dir: str) -> Optional[int]:
    """提取刮削年份"""
    # 电影优先使用具体子作品/文件名年份，避免主系列年份覆盖剧场版年份。
    texts = []
    if item.group_type == "movie":
        texts.extend([subwork_dir, item.title, item.relative_path, item.original_title])
    if item.year:
        texts.append(str(item.year))
    if subwork_dir:
        texts.append(subwork_dir)

    for text in texts:
        if not text:
            continue
        patterns = [
            re.compile(r"[.．](\d{4})"),
            re.compile(r"[(\（](\d{4})[)\）]"),
            re.compile(r"\s(\d{4})(?:\s|$)"),
            re.compile(r"^(\d{4})$"),
        ]
        for pat in patterns:
            m = pat.search(str(text))
            if m:
                year = int(m.group(1))
                if 1900 <= year <= 2099:
                    return year
    return None


def _extract_group_scrape_year(items: List[ImportPlanItem], group_type: str, subwork_dir: str) -> Optional[int]:
    """Infer a target year for a grouped scrape target.

    For multi-season anime folders the root directory often keeps the first
    season year, e.g. "Re:Zero (2016) {tmdb-65942}/Season 3/...2024...".
    Using the root year makes later seasons bind to Season 1, so season targets
    prefer years from their actual video filenames.
    """
    if group_type == "season":
        years = []
        for item in items:
            year = _extract_video_filename_year(item)
            if year:
                years.append(year)
        if years:
            return min(years)

    return _extract_scrape_year(items[0], subwork_dir)


def _extract_video_filename_year(item: ImportPlanItem) -> Optional[int]:
    filename = (item.relative_path or "").replace("\\", "/").split("/")[-1]
    texts = [filename, item.title, item.original_title]
    patterns = [
        re.compile(r"[.．](\d{4})(?=[.．\s_-])"),
        re.compile(r"[(\（](\d{4})[)\）]"),
        re.compile(r"\b(\d{4})\b"),
    ]
    for text in texts:
        if not text:
            continue
        for pat in patterns:
            for m in pat.finditer(str(text)):
                year = int(m.group(1))
                if 1900 <= year <= 2099:
                    return year
    return None


def _common_filename_series_title(items: List[ImportPlanItem]) -> str:
    """Extract a stable show-title alias from episode filenames in one target."""
    counts: Dict[str, int] = {}
    display: Dict[str, str] = {}
    for item in items:
        title = _filename_series_title(item)
        if not title:
            continue
        key = _title_containment_key(title)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        display.setdefault(key, title)

    if not counts:
        return ""
    key, count = max(counts.items(), key=lambda entry: (entry[1], len(entry[0])))
    min_count = 1 if len(items) == 1 else min(3, max(2, len(items) // 3))
    if count < min_count:
        return ""
    return display[key]


def _filename_series_title(item: ImportPlanItem) -> str:
    filename = (item.relative_path or "").replace("\\", "/").split("/")[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", filename)
    if not stem:
        return ""

    patterns = [
        r"^(?P<title>.+?)\s*[-_. ]+\s*S\d+\s*E\d+",
        r"^(?P<title>.+?)\s*[-_. ]+\s*Special\s+\d+",
    ]
    for pattern in patterns:
        match = re.search(pattern, stem, flags=re.IGNORECASE)
        if not match:
            continue
        title = match.group("title")
        title = re.sub(r"^\s*(?:\[[^\]]+\]|【[^】]+】)\s*", "", title).strip()
        title = _clean_scrape_title(title)
        if _is_generic_title(title):
            continue
        # CJK aliases are especially valuable in anime libraries and less likely
        # to be release-group noise than Latin-only prefixes.
        if not re.search(r"[\u3400-\u9fff\u3040-\u30ff]", title):
            continue
        return title
    return ""


def _extract_target_tmdb_hint(
    representative: ImportPlanItem,
    group_type: str,
    season_num: Optional[int],
    subwork_dir: str,
) -> Optional[int]:
    """Return a TMDB hint only when it is safe for this target.

    A parent folder hint is reliable for Season 1 / standalone movies, but it
    can be harmful for later seasons when the external metadata source stores
    each season as a separate TV show.  Later seasons therefore only accept
    hints that are attached to the concrete subwork/title, not inherited from
    the parent relative path.
    """
    explicit_hint = (
        _extract_tmdb_hint(subwork_dir)
        or _extract_tmdb_hint(representative.title)
        or _extract_tmdb_hint(representative.original_title)
        or _extract_tmdb_hint(representative.work_title)
    )
    from app.recognition.verified_titles import match_verified_tmdb_binding

    if group_type == "special" and representative.tmdb_hint_id and representative.tmdb_hint_type == "tv":
        return representative.tmdb_hint_id
    verified_binding = match_verified_tmdb_binding(representative.relative_path)
    if verified_binding:
        return verified_binding.tmdb_id
    if group_type == "season" and (season_num or 1) > 1:
        if explicit_hint:
            return explicit_hint
        if not subwork_dir and representative.tmdb_hint_id:
            return representative.tmdb_hint_id
        return explicit_hint

    return (
        representative.tmdb_hint_id
        or _extract_tmdb_hint(representative.relative_path)
        or explicit_hint
    )


def _infer_asset_dir(media_type: str, group_type: str, target_dir: str) -> Path:
    directory = Path(target_dir)
    if media_type == "tv" and group_type in {"season", "special", "sps"}:
        if re.match(r"^Season\s+\d+$", directory.name, flags=re.IGNORECASE):
            return directory.parent
    return directory


def build_scrape_targets(plan: ImportPlan) -> List[ScrapeTarget]:
    """从 ImportPlan 生成可刮削目标

    只处理 season、special 和 movie，不处理 OP-ED/附属视频。
    按 series_group + local_season_number 聚合。
    """
    # 只取可刮削条目
    scrape_items = [
        it for it in plan.items
        if it.resource_type == "video"
        and it.action == "generate_strm"
        and it.group_type in ("season", "special", "movie")
    ]
    movie_owner_keys = {
        library_card_identity(it)
        for it in scrape_items
        if it.group_type == "movie"
    }
    scrape_items = [
        it for it in scrape_items
        if not (
            it.group_type == "special"
            and it.media_type == "movie"
            and library_card_identity(it) in movie_owner_keys
        )
    ]

    # 按聚合键分组（CP2：聚合键必须包含 canonical ownership——
    # 不同 canonical work 即使 series_group/local_title/季号相同也不得合并）
    groups: Dict[Tuple, List[ImportPlanItem]] = defaultdict(list)
    canonical_of_key: Dict[Tuple, str] = {}
    for item in scrape_items:
        local_title = _choose_local_title(item)
        card_identity = library_card_identity(item)
        canonical = str(getattr(item, "canonical_work_id", "") or "")
        if item.group_type in ("season", "special"):
            season_number = item.season_number
            if item.group_type == "special" and season_number is None:
                season_number = 0
            key = (
                plan.source, plan.plan_id, canonical, item.series_group,
                local_title, item.group_type, season_number, card_identity,
            )
        else:
            # movie: 每个 standalone 独立一个 target
            key = (
                plan.source, plan.plan_id, canonical, item.series_group,
                local_title, "movie", None, card_identity,
            )
        groups[key].append(item)
        if canonical:
            canonical_of_key[key] = canonical

    targets = []
    for key, items in groups.items():
        source, plan_id, canonical, series_group, local_title, group_type, season_num, card_identity = key
        representative = items[0]

        # 提取子作品目录。一个目标如果混合了多个子目录（常见于主系列
        # Season 0 特别篇），不能随便拿第一个子目录当搜索标题或来源上下文。
        subwork_dirs = []
        for it in items:
            d = _extract_subwork_dir(it)
            if d and d not in subwork_dirs:
                subwork_dirs.append(d)
        subwork_dir = subwork_dirs[0] if len(subwork_dirs) == 1 else ""

        # 清理搜索标题
        if group_type == "movie":
            scrape_title = _specific_movie_title(representative, series_group, subwork_dir)
        elif group_type == "special" and representative.card_type == "main_series" and series_group:
            scrape_title = _clean_scrape_title(series_group, series_group)
        elif subwork_dir:
            # 从子作品目录提取更具体的标题
            cleaned_subwork = _strip_tmdb_hint(_strip_sequence_prefix(subwork_dir))
            cleaned_subwork = _remove_year_suffix(cleaned_subwork)
            cleaned_subwork = cleaned_subwork.strip()
            scrape_title = _clean_scrape_title(cleaned_subwork, series_group)
        elif group_type == "movie" and representative.title:
            # movie 使用 item.title（如"CLANNAD总集篇：在那苍绿的树下"）
            scrape_title = _clean_scrape_title(representative.title, series_group)
        else:
            scrape_title = _clean_scrape_title(local_title, series_group)
        if _is_generic_title(scrape_title):
            scrape_title = _choose_local_title(representative)

        # 提取年份
        scrape_year = _extract_group_scrape_year(items, group_type, subwork_dir)

        # scrape_type
        scrape_type = "movie" if group_type == "movie" else "tv"

        # original_title
        filename_alias = _common_filename_series_title(items) if group_type in {"season", "special"} else ""
        original_title = _strip_tmdb_hint(filename_alias or representative.original_title or "")
        tmdb_hint_id = _extract_target_tmdb_hint(representative, group_type, season_num, subwork_dir)

        # target 目录（从第一个 item 的 target_dir 推断）
        target_dir = representative.target_dir or ""
        # 如果没有 target_dir，从 mirror 结构推断
        if not target_dir:
            from app.core.paths import get_mirror_root
            mirror_root = get_mirror_root()
            ns = {"pan115": "115", "openlist": "openlist"}.get(source, source)
            work_dir = f"{series_group} ({representative.year})" if representative.year else series_group
            if group_type in ("season", "special") and season_num is not None:
                target_dir = str(mirror_root / ns / work_dir / f"Season {season_num}")
            else:
                target_dir = str(mirror_root / ns / work_dir)

        # 预设 NFO / 图片路径
        target_nfo_path = str(Path(target_dir) / ("tvshow.nfo" if scrape_type == "tv" else "movie.nfo"))
        asset_dir = _infer_asset_dir(representative.media_type, group_type, target_dir)
        target_poster_path = str(asset_dir / "poster.jpg")
        target_fanart_path = str(asset_dir / "fanart.jpg")
        target_clearlogo_path = str(asset_dir / "clearlogo.png")

        # 构建 target
        target_id = _make_target_id(
            source, plan_id, canonical, series_group, local_title,
            group_type, season_num, scrape_type, card_identity,
        )
        unique_episode_keys = {
            (
                item.season_number if item.season_number is not None else season_num,
                item.episode_number if item.episode_number is not None else item.special_number,
            )
            for item in items
            if item.episode_number is not None or item.special_number is not None
        }

        # needs_review: 无年份或有 warning 的条目
        needs_review = any(it.needs_review for it in items)
        warnings = []
        if scrape_year is None:
            warnings.append("缺少年份")
            needs_review = True
        if subwork_dir:
            warnings.append(f"子作品目录: {subwork_dir}")

        target = ScrapeTarget(
            scrape_target_id=target_id,
            source=source,
            import_plan_id=plan_id,
            work_id=representative.work_id,
            canonical_work_id=canonical,
            card_type=representative.card_type,
            media_type=representative.media_type,
            show_type=representative.show_type,
            group_type=group_type,
            series_group=series_group,
            local_title=local_title,
            original_title=original_title,
            source_subwork_dir=subwork_dir,
            local_year=representative.year,
            local_season_number=season_num,
            scrape_title=scrape_title,
            scrape_year=scrape_year,
            scrape_type=scrape_type,
            tmdb_hint_id=tmdb_hint_id,
            tmdb_hint_type=representative.tmdb_hint_type or ("movie" if scrape_type == "movie" else "tv"),
            target_dir=target_dir,
            target_nfo_path=target_nfo_path,
            target_poster_path=target_poster_path,
            target_fanart_path=target_fanart_path,
            target_clearlogo_path=target_clearlogo_path,
            item_ids=[it.id for it in items],
            local_episode_count=len(unique_episode_keys),
            needs_review=needs_review,
            warnings=warnings,
        )
        targets.append(target)

    # 排序：series_group, group_type, season_number
    targets.sort(key=lambda t: (
        t.series_group, {"season": 0, "special": 1, "movie": 2}.get(t.group_type, 3),
        t.local_season_number or 999,
    ))

    return targets
