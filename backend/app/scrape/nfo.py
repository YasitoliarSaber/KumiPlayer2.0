# -*- coding: utf-8 -*-
"""NFO 生成器

生成 tvshow.nfo / movie.nfo / episode NFO。
按 TMDB 规范补全字段：rating, genre, studio, premiered, runtime 等。
"""

import html
from pathlib import Path
from typing import List, Optional


def _escape_xml(text: str) -> str:
    """XML 转义"""
    if not text:
        return ""
    return html.escape(str(text), quote=True)


def generate_tvshow_nfo(
    title: str,
    original_title: str = "",
    year: Optional[int] = None,
    plot: str = "",
    tmdb_id: Optional[int] = None,
    season: Optional[int] = None,
    rating: float = 0.0,
    genres: Optional[List[str]] = None,
    studios: Optional[List[str]] = None,
    premiered: str = "",
    runtime: int = 0,
    certification: str = "",
    certification_country: str = "",
    cast: Optional[List[dict]] = None,
) -> str:
    """生成 tvshow.nfo 内容

    参数:
        title: 标题
        original_title: 原始标题
        year: 年份
        plot: 简介
        tmdb_id: TMDB ID
        season: 季号
        rating: 评分（vote_average）
        genres: 类型列表，如 ["Animation", "Drama"]
        studios: 制作公司列表
        premiered: 首播日期，如 "2012-04-22"
        runtime: 单集时长（分钟）
    """
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    lines.append("<tvshow>")
    lines.append(f"  <title>{_escape_xml(title)}</title>")

    if original_title:
        lines.append(f"  <originaltitle>{_escape_xml(original_title)}</originaltitle>")
    if year:
        lines.append(f"  <year>{year}</year>")
    if plot:
        lines.append(f"  <plot>{_escape_xml(plot)}</plot>")
    if tmdb_id:
        lines.append(f"  <tmdbid>{tmdb_id}</tmdbid>")
        lines.append(f'  <uniqueid type="tmdb" default="true">{tmdb_id}</uniqueid>')
    if season is not None:
        lines.append(f"  <season>{season}</season>")
    if rating > 0:
        lines.append(f"  <rating>{rating:.1f}</rating>")
    if certification:
        lines.append(f"  <mpaa>{_escape_xml(certification)}</mpaa>")
    if certification_country:
        lines.append(f"  <certificationcountry>{_escape_xml(certification_country)}</certificationcountry>")
    if premiered:
        lines.append(f"  <premiered>{_escape_xml(premiered)}</premiered>")
    if runtime > 0:
        lines.append(f"  <runtime>{runtime}</runtime>")

    # 类型（多个 genre 标签）
    if genres:
        for genre in genres:
            if genre:
                lines.append(f"  <genre>{_escape_xml(genre)}</genre>")

    # 制作公司（多个 studio 标签）
    if studios:
        for studio in studios:
            if studio:
                lines.append(f"  <studio>{_escape_xml(studio)}</studio>")

    if cast:
        for person in cast:
            name = str(person.get("name") or "").strip()
            if not name:
                continue
            lines.append("  <actor>")
            lines.append(f"    <name>{_escape_xml(name)}</name>")
            if person.get("role"):
                lines.append(f"    <role>{_escape_xml(person['role'])}</role>")
            if person.get("profile_path"):
                lines.append(f"    <thumb>{_escape_xml(person['profile_path'])}</thumb>")
            lines.append("  </actor>")

    lines.append("</tvshow>")
    return "\n".join(lines) + "\n"


def generate_movie_nfo(
    title: str,
    original_title: str = "",
    year: Optional[int] = None,
    plot: str = "",
    tmdb_id: Optional[int] = None,
    rating: float = 0.0,
    genres: Optional[List[str]] = None,
    studios: Optional[List[str]] = None,
    releasedate: str = "",
    runtime: int = 0,
    certification: str = "",
    certification_country: str = "",
    cast: Optional[List[dict]] = None,
) -> str:
    """生成 movie.nfo 内容

    参数:
        title: 标题
        original_title: 原始标题
        year: 年份
        plot: 简介
        tmdb_id: TMDB ID
        rating: 评分（vote_average）
        genres: 类型列表
        studios: 制作公司列表
        releasedate: 上映日期，如 "2006-09-02"
        runtime: 时长（分钟）
    """
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    lines.append("<movie>")
    lines.append(f"  <title>{_escape_xml(title)}</title>")

    if original_title:
        lines.append(f"  <originaltitle>{_escape_xml(original_title)}</originaltitle>")
    if year:
        lines.append(f"  <year>{year}</year>")
    if plot:
        lines.append(f"  <plot>{_escape_xml(plot)}</plot>")
    if tmdb_id:
        lines.append(f"  <tmdbid>{tmdb_id}</tmdbid>")
        lines.append(f'  <uniqueid type="tmdb" default="true">{tmdb_id}</uniqueid>')
    if rating > 0:
        lines.append(f"  <rating>{rating:.1f}</rating>")
    if certification:
        lines.append(f"  <mpaa>{_escape_xml(certification)}</mpaa>")
    if certification_country:
        lines.append(f"  <certificationcountry>{_escape_xml(certification_country)}</certificationcountry>")
    if releasedate:
        lines.append(f"  <releasedate>{_escape_xml(releasedate)}</releasedate>")
    elif year:
        lines.append(f"  <releasedate>{year}</releasedate>")
    if runtime > 0:
        lines.append(f"  <runtime>{runtime}</runtime>")

    # 类型
    if genres:
        for genre in genres:
            if genre:
                lines.append(f"  <genre>{_escape_xml(genre)}</genre>")

    # 制作公司
    if studios:
        for studio in studios:
            if studio:
                lines.append(f"  <studio>{_escape_xml(studio)}</studio>")

    if cast:
        for person in cast:
            name = str(person.get("name") or "").strip()
            if not name:
                continue
            lines.append("  <actor>")
            lines.append(f"    <name>{_escape_xml(name)}</name>")
            if person.get("role"):
                lines.append(f"    <role>{_escape_xml(person['role'])}</role>")
            if person.get("profile_path"):
                lines.append(f"    <thumb>{_escape_xml(person['profile_path'])}</thumb>")
            lines.append("  </actor>")

    lines.append("</movie>")
    return "\n".join(lines) + "\n"


def generate_episode_nfo(
    title: str,
    season: int,
    episode: int,
    plot: str = "",
    runtime: int = 0,
    aired: str = "",
    tmdb_id: Optional[int] = None,
    thumb: str = "",
    metadata_pending: bool = False,
) -> str:
    """生成 episode NFO 内容

    参数:
        title: 集标题
        season: 季号
        episode: 集号
        plot: 集简介
        runtime: 时长（分钟）
        aired: 播出日期
        tmdb_id: TMDB ID
        thumb: 缩略图路径
    """
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    lines.append("<episodedetails>")
    lines.append(f"  <title>{_escape_xml(title)}</title>")
    lines.append(f"  <season>{season}</season>")
    lines.append(f"  <episode>{episode}</episode>")

    if plot:
        lines.append(f"  <plot>{_escape_xml(plot)}</plot>")
    if runtime > 0:
        lines.append(f"  <runtime>{runtime}</runtime>")
    if aired:
        lines.append(f"  <aired>{_escape_xml(aired)}</aired>")
    if tmdb_id:
        lines.append(f"  <tmdbid>{tmdb_id}</tmdbid>")
        lines.append(f'  <uniqueid type="tmdb" default="true">{tmdb_id}</uniqueid>')
    if thumb:
        lines.append(f"  <thumb>{_escape_xml(thumb)}</thumb>")
    if metadata_pending:
        lines.append("  <metadatapending>true</metadatapending>")

    lines.append("</episodedetails>")
    return "\n".join(lines) + "\n"


def write_nfo(target_dir: str, filename: str, content: str) -> str:
    """写入 NFO 文件（固定文件名，防止路径注入）"""
    allowed = {"tvshow.nfo", "movie.nfo"}
    # episode NFO 允许 S01E01.nfo 格式
    if filename.endswith(".nfo") and filename not in allowed:
        # 检查是否为 episode NFO 格式（如 S01E01.nfo）
        import re
        if not re.match(r"^S\d{2}E\d{2,3}\.nfo$", filename, re.IGNORECASE):
            raise ValueError(f"不允许的 NFO 文件名: {filename}")

    path = Path(target_dir) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return str(path)
