# -*- coding: utf-8 -*-
"""逐文件只读核验 115 样本、ImportPlan、镜像、NFO 与 TMDB 身份。

该工具只写审计报告，不修改 ImportPlan、镜像、NFO、ScrapeMap 或播放记录。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.atomic_json import write_json_atomic  # noqa: E402
from app.core.config import load_config  # noqa: E402
from app.import_plan.store import load_latest_confirmed_import_plan  # noqa: E402
from app.recognition.planner import _build_summary, build_draft_import_plan  # noqa: E402
from app.recognition.plan_recognizer import recognize_import_plan_media  # noqa: E402
from app.scrape.store import load_scrape_map  # noqa: E402
from app.scrape.target_builder import build_scrape_targets  # noqa: E402
from app.scrape.tmdb_client import TMDBClient, TMDBClientError  # noqa: E402
from app.sources.registry import get_source_adapter  # noqa: E402


PLACEHOLDER_TITLE_RE = re.compile(r"^第\s*\d+\s*集$")


def _path_key(value: str | Path) -> str:
    try:
        return str(Path(value).resolve()).casefold()
    except OSError:
        return str(Path(value)).casefold()


def _read_nfo(path: Path) -> dict:
    result = {
        "path": str(path),
        "exists": path.exists(),
        "valid": False,
        "root": "",
        "title": "",
        "original_title": "",
        "tmdb_id": None,
        "season": None,
        "episode": None,
        "error": "",
    }
    if not path.exists():
        return result
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        result["error"] = str(exc)
        return result
    result.update({
        "valid": True,
        "root": root.tag,
        "title": (root.findtext("title") or "").strip(),
        "original_title": (root.findtext("originaltitle") or "").strip(),
        "tmdb_id": _int_or_none(root.findtext("tmdbid")),
        "season": _int_or_none(root.findtext("season")),
        "episode": _int_or_none(root.findtext("episode")),
    })
    return result


def _int_or_none(value: str | None) -> int | None:
    text = (value or "").strip()
    return int(text) if text.isdigit() else None


def _episode_nfo_path(item) -> Path | None:
    target_dir = Path(item.target_dir or "")
    if item.group_type == "season" and item.season_number is not None and item.episode_number is not None:
        return target_dir / f"S{item.season_number:02d}E{item.episode_number:02d}.nfo"
    if item.group_type == "special" and item.special_number is not None:
        return target_dir / f"S00E{item.special_number:02d}.nfo"
    if item.group_type == "movie":
        return target_dir / "movie.nfo"
    return None


def _audit_file(item, sample_paths: set[str]) -> dict:
    issues: list[str] = []
    source_present = item.relative_path in sample_paths
    if not source_present:
        issues.append("source_not_in_sample")

    strm_path = Path(item.target_strm_path) if item.target_strm_path else None
    strm_exists = bool(strm_path and strm_path.exists())
    strm_content_matches = None
    if item.action == "generate_strm":
        if not strm_exists:
            issues.append("missing_strm")
        else:
            try:
                strm_content_matches = strm_path.read_text(encoding="utf-8").strip() == (item.real_path or "").strip()
            except (OSError, UnicodeError):
                strm_content_matches = False
            if not strm_content_matches:
                issues.append("strm_target_mismatch")

    episode_nfo_path = _episode_nfo_path(item) if item.action == "generate_strm" else None
    episode_nfo = _read_nfo(episode_nfo_path) if episode_nfo_path else None
    if episode_nfo and item.group_type in {"season", "special"}:
        if not episode_nfo["exists"]:
            issues.append("missing_episode_nfo")
        elif not episode_nfo["valid"]:
            issues.append("invalid_episode_nfo")
        else:
            expected_season = 0 if item.group_type == "special" else item.season_number
            expected_episode = item.special_number if item.group_type == "special" else item.episode_number
            if episode_nfo["season"] != expected_season or episode_nfo["episode"] != expected_episode:
                issues.append("episode_nfo_number_mismatch")
            if not episode_nfo["title"] or PLACEHOLDER_TITLE_RE.fullmatch(episode_nfo["title"]):
                issues.append("placeholder_episode_title")

    return {
        "id": item.id,
        "relative_path": item.relative_path,
        "real_path": item.real_path,
        "resource_type": item.resource_type,
        "action": item.action,
        "work_title": item.work_title,
        "series_group": item.series_group,
        "group_type": item.group_type,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "special_number": item.special_number,
        "target_strm_path": item.target_strm_path,
        "source_present": source_present,
        "strm_exists": strm_exists,
        "strm_content_matches": strm_content_matches,
        "episode_nfo": episode_nfo,
        "issues": issues,
    }


def _compact_tv_detail(detail: dict) -> dict:
    return {
        "id": detail.get("id"),
        "type": "tv",
        "title": detail.get("name") or "",
        "original_title": detail.get("original_name") or "",
        "year": (detail.get("first_air_date") or "")[:4],
        "seasons": [
            {
                "season_number": season.get("season_number"),
                "episode_count": season.get("episode_count"),
                "name": season.get("name") or "",
            }
            for season in detail.get("seasons") or []
        ],
    }


def _compact_movie_detail(detail: dict) -> dict:
    return {
        "id": detail.get("id"),
        "type": "movie",
        "title": detail.get("title") or "",
        "original_title": detail.get("original_title") or "",
        "year": (detail.get("release_date") or "")[:4],
    }


def _remote_catalog(scrape_items: list, online: bool) -> tuple[dict[str, dict], list[dict]]:
    if not online:
        return {}, []
    identities = sorted({
        (int(item.tmdb_id), item.tmdb_type)
        for item in scrape_items
        if item.tmdb_id and item.tmdb_type in {"tv", "movie"}
    })
    catalog: dict[str, dict] = {}
    errors: list[dict] = []
    client = TMDBClient(language="zh-CN")
    try:
        for tmdb_id, tmdb_type in identities:
            key = f"{tmdb_type}:{tmdb_id}"
            try:
                if tmdb_type == "tv":
                    catalog[key] = _compact_tv_detail(client.get_tv_detail(tmdb_id))
                else:
                    catalog[key] = _compact_movie_detail(client.get_movie_detail(tmdb_id))
            except (TMDBClientError, OSError, ValueError) as exc:
                errors.append({"tmdb_id": tmdb_id, "tmdb_type": tmdb_type, "error": str(exc)})
    finally:
        client.close()
    return catalog, errors


def _target_records(plan, remote_catalog: dict[str, dict]) -> tuple[list[dict], list]:
    scrape_map = load_scrape_map()
    maps_by_id = {item.scrape_target_id: item for item in scrape_map.items}
    maps_by_path = {_path_key(item.nfo_path): item for item in scrape_map.items if item.nfo_path}
    plan_items_by_id = {item.id: item for item in plan.items}
    records = []
    matched_maps = []
    for target in build_scrape_targets(plan):
        map_item = maps_by_id.get(target.scrape_target_id) or maps_by_path.get(_path_key(target.target_nfo_path))
        if map_item:
            matched_maps.append(map_item)
        nfo = _read_nfo(Path(target.target_nfo_path))
        issues: list[str] = []
        if not nfo["exists"]:
            issues.append("missing_metadata")
        elif not nfo["valid"]:
            issues.append("invalid_target_nfo")
        if map_item and nfo["tmdb_id"] and int(map_item.tmdb_id or 0) != nfo["tmdb_id"]:
            issues.append("stale_map")

        tmdb_type = map_item.tmdb_type if map_item else ""
        tmdb_id = int(map_item.tmdb_id) if map_item and map_item.tmdb_id else None
        remote = remote_catalog.get(f"{tmdb_type}:{tmdb_id}") if tmdb_id else None
        remote_season = None
        if remote and tmdb_type == "tv" and map_item.tmdb_season_number is not None:
            remote_season = next(
                (season for season in remote["seasons"] if season["season_number"] == map_item.tmdb_season_number),
                None,
            )
            if remote_season is None:
                issues.append("remote_season_missing")
            elif target.local_episode_count > int(remote_season.get("episode_count") or 0):
                issues.append("local_count_exceeds_remote_season")

        records.append({
            **asdict(target),
            "map": asdict(map_item) if map_item else None,
            "nfo": nfo,
            "remote": remote,
            "remote_season": remote_season,
            "source_paths": [
                plan_items_by_id[item_id].relative_path
                for item_id in target.item_ids
                if item_id in plan_items_by_id
            ],
            "issues": issues,
        })
    return records, matched_maps


def _duplicate_keys(plan) -> list[dict]:
    groups: dict[tuple, list] = defaultdict(list)
    for item in plan.items:
        if item.resource_type != "video" or item.action != "generate_strm":
            continue
        if item.group_type == "season":
            key = (item.series_group, "season", item.season_number, item.episode_number)
        elif item.group_type == "special":
            key = (item.series_group, "special", 0, item.special_number)
        else:
            continue
        groups[key].append(item)
    return [
        {
            "series_group": key[0],
            "group_type": key[1],
            "season_number": key[2],
            "episode_number": key[3],
            "count": len(items),
            "paths": [item.relative_path for item in items],
            "nfo_path": str(_episode_nfo_path(items[0]) or ""),
        }
        for key, items in groups.items()
        if len(items) > 1
    ]


def _mirror_inventory(plan) -> list[dict]:
    mirror_root = ROOT / "data" / "mirror" / "115"
    owners = {
        _path_key(item.target_strm_path): item.id
        for item in plan.items
        if item.target_strm_path
    }
    return [
        {
            "path": str(path),
            "extension": path.suffix.casefold(),
            "size": path.stat().st_size,
            "owner_item_id": owners.get(_path_key(path), "") if path.suffix.casefold() == ".strm" else "",
        }
        for path in sorted(mirror_root.rglob("*"))
        if path.is_file()
    ]


def _render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# 115 网盘刮削逐文件审计",
        "",
        "> 本报告由只读审计生成；未修改镜像、NFO、ScrapeMap 或播放记录。完整逐文件结论见同名 JSON。",
        "",
        "## 范围",
        "",
        f"- 目录树来源条目：{summary['sample_file_count']}。",
        f"- 当前 ImportPlan 条目：{summary['plan_item_count']}。",
        f"- 视频条目：{summary['video_count']}，生成 `.strm`：{summary['generated_video_count']}。",
        f"- 刮削目标：{summary['target_count']}。",
        f"- 115 镜像文件：{summary['mirror_file_count']}。",
        "",
        "## 自动核验结论",
        "",
        f"- 目录树未进入计划：{summary['sample_only_count']}；计划中找不到来源：{summary['plan_only_count']}。",
        f"- 缺失 `.strm`：{summary['missing_strm_count']}；内容指向错误：{summary['strm_target_mismatch_count']}。",
        f"- 缺失或无效分集 NFO：{summary['episode_nfo_problem_count']}。",
        f"- 占位分集标题：{summary['placeholder_episode_title_count']}。",
        f"- 重复季集 / Special 编号组：{summary['duplicate_key_count']}。",
        f"- 缺作品级元数据目标：{summary['missing_metadata_count']}。",
        f"- TMDB 在线核验失败：{summary['remote_error_count']}。",
        "",
        "## 重复季集与 Special 编号",
        "",
        "| 作品 | 类型 | 季 | 集 / SP | 文件数 | 共用 NFO |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for item in report["duplicate_episode_keys"]:
        lines.append(
            f"| {item['series_group']} | {item['group_type']} | {item['season_number']} | "
            f"{item['episode_number']} | {item['count']} | `{item['nfo_path']}` |"
        )
    lines.extend([
        "",
        "## 有问题的刮削目标",
        "",
        "| 作品 | 类型 | 本地季 | 文件数 | TMDB | 问题 |",
        "| --- | --- | ---: | ---: | --- | --- |",
    ])
    for target in report["targets"]:
        if not target["issues"]:
            continue
        map_item = target["map"] or {}
        tmdb = f"{map_item.get('tmdb_type', '')}:{map_item.get('tmdb_id', '')}".strip(":") or "-"
        lines.append(
            f"| {target['local_title']} | {target['group_type']} | {target['local_season_number']} | "
            f"{len(target['item_ids'])} | {tmdb} | {', '.join(target['issues'])} |"
        )
    lines.extend([
        "",
        "## 文件级问题统计",
        "",
        "| 问题代码 | 数量 |",
        "| --- | ---: |",
    ])
    for code, count in sorted(report["file_issue_counts"].items()):
        lines.append(f"| `{code}` | {count} |")
    lines.extend([
        "",
        "## 说明",
        "",
        "- JSON 的 `files` 数组覆盖当前计划的每一个来源文件。",
        "- JSON 的 `mirror_files` 数组覆盖当前 115 镜像中的每一个实际文件。",
        "- JSON 的 `targets` 数组列出每个刮削目标、ScrapeMap、NFO、TMDB 条目和来源路径。",
        "- `identity_unverified` 一类标题差异需结合已核实 TMDB 强绑定判断，不能仅凭中文译名不同删除元数据。",
        "",
    ])
    return "\n".join(lines)


def audit(sample: Path, online: bool) -> dict:
    config = load_config(force_reload=True)
    adapter = get_source_adapter("pan115")
    snapshot = adapter.parse(str(sample), source_root=config.pan115_root or "")
    sample_paths = {raw.relative_path for raw in snapshot.files}

    plan = load_latest_confirmed_import_plan("pan115")
    if not plan:
        raise RuntimeError("没有找到当前可用的 115 ImportPlan")
    plan_paths = {item.relative_path for item in plan.items}

    fresh_snapshot = adapter.parse(str(sample), source_root=config.pan115_root or "")
    fresh_snapshot.import_family = "anime"
    fresh_plan = build_draft_import_plan(fresh_snapshot)
    fresh_plan.import_family = "anime"
    for item in fresh_plan.items:
        item.import_family = "anime"
    recognize_import_plan_media(fresh_plan)
    fresh_plan.summary = _build_summary(fresh_plan.items, fresh_plan.import_family)
    fresh_targets = build_scrape_targets(fresh_plan)
    fresh_generated_videos = sum(
        item.resource_type == "video" and item.action == "generate_strm"
        for item in fresh_plan.items
    )

    scrape_map_items = [item for item in load_scrape_map().items if item.source == "pan115"]
    remote_catalog, remote_errors = _remote_catalog(scrape_map_items, online)
    targets, _ = _target_records(plan, remote_catalog)
    files = [_audit_file(item, sample_paths) for item in plan.items]
    mirror_files = _mirror_inventory(plan)
    duplicate_keys = _duplicate_keys(plan)
    file_issue_counts = Counter(code for item in files for code in item["issues"])

    video_files = [item for item in files if item["resource_type"] == "video"]
    generated = [item for item in video_files if item["action"] == "generate_strm"]
    summary = {
        "sample": str(sample),
        "plan_id": plan.plan_id,
        "sample_file_count": len(snapshot.files),
        "plan_item_count": len(plan.items),
        "video_count": len(video_files),
        "generated_video_count": len(generated),
        "target_count": len(targets),
        "mirror_file_count": len(mirror_files),
        "sample_only_count": len(sample_paths - plan_paths),
        "plan_only_count": len(plan_paths - sample_paths),
        "missing_strm_count": file_issue_counts["missing_strm"],
        "strm_target_mismatch_count": file_issue_counts["strm_target_mismatch"],
        "episode_nfo_problem_count": sum(
            file_issue_counts[code]
            for code in ("missing_episode_nfo", "invalid_episode_nfo", "episode_nfo_number_mismatch")
        ),
        "placeholder_episode_title_count": file_issue_counts["placeholder_episode_title"],
        "duplicate_key_count": len(duplicate_keys),
        "missing_metadata_count": sum("missing_metadata" in target["issues"] for target in targets),
        "remote_error_count": len(remote_errors),
    }
    return {
        "summary": summary,
        "fresh_preview_summary": {
            **fresh_plan.summary,
            "generated_video_count": fresh_generated_videos,
            "scrape_target_count": len(fresh_targets),
        },
        "file_issue_counts": dict(file_issue_counts),
        "duplicate_episode_keys": duplicate_keys,
        "remote_catalog": remote_catalog,
        "remote_errors": remote_errors,
        "targets": targets,
        "files": files,
        "mirror_files": mirror_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sample",
        type=Path,
        default=ROOT / "docs" / "samples" / "根目录20260703203700_目录树.txt",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "audits" / "pan115_scrape_semantics_latest.json",
    )
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    report = audit(args.sample, online=not args.offline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(args.output, report)
    markdown_path = args.output.with_suffix(".md")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8", newline="\n")
    print(json.dumps({"json": str(args.output), "markdown": str(markdown_path), **report["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
