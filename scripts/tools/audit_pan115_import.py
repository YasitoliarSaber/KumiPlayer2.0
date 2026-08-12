# -*- coding: utf-8 -*-
"""Audit pan115 directory-tree recognition without generating mirror files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.config import load_config  # noqa: E402
from app.recognition.planner import build_draft_import_plan  # noqa: E402
from app.recognition.plan_recognizer import recognize_import_plan_media  # noqa: E402
from app.scrape.target_builder import build_scrape_targets  # noqa: E402
from app.sources.registry import get_source_adapter  # noqa: E402


AUX_PAT = re.compile(
    r"(?i)(?:^|[\s._\-\[\(])(?:PV|CM|MENU|TRAILER|EYECATCH|NCOP|NCED|OP|ED)(?:\d+)?(?:$|[\s._\-\]\)])"
    r"|菜单|预告|花絮"
)


def _path_parts(path: str) -> list[str]:
    return [part for part in (path or "").replace("\\", "/").split("/") if part]


def _stem(path: str) -> str:
    parts = _path_parts(path)
    return Path(parts[-1] if parts else path).stem


def _remove_leading_release_brackets(title: str) -> str:
    cleaned = title.strip()
    while True:
        new = re.sub(r"^\s*(?:\[[^\]]+\]|【[^】]+】)\s*", "", cleaned, count=1)
        if new == cleaned:
            return cleaned or title.strip()
        cleaned = new.strip()


def _special_display_expected(relative_path: str) -> str:
    return _remove_leading_release_brackets(_stem(relative_path))


def _title_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", (value or "").casefold())


def _safe_sample_path(name: str | None = None) -> Path:
    if name:
        for candidate in (Path(name), ROOT / name, ROOT / "docs" / "samples" / name):
            if candidate.exists():
                return candidate
    samples = sorted((ROOT / "docs" / "samples").glob("*.txt"), key=lambda p: p.stat().st_size)
    for sample in samples:
        if "目录树" in sample.name:
            return sample
    if samples:
        return samples[0]
    raise FileNotFoundError("No sample txt files found under docs/samples/")


def _category(path: str, source: str) -> str:
    parts = _path_parts(path)
    categories = {"动画", "新番", "刮削好的动画", "动画电影", "电影", "剧集", "番剧", "TV", "TV动画"}
    if source == "local":
        for part in parts[:2]:
            if part in categories:
                return part
        return ""
    return parts[0] if parts and parts[0] in categories else ""


def _work_container(path: str, source: str) -> str:
    parts = _path_parts(path)
    if not parts:
        return ""
    if source != "local" and _category(path, source):
        return parts[1] if len(parts) > 1 else ""
    return parts[0]


def _source_subwork(path: str, source: str) -> str:
    parts = _path_parts(path)
    base = 2 if source != "local" and _category(path, source) else 1
    if len(parts) > base + 1:
        return parts[base]
    return ""


def _scrape_target_dict(target) -> dict:
    return {
        "series_group": target.series_group,
        "local_title": target.local_title,
        "source_subwork_dir": getattr(target, "source_subwork_dir", ""),
        "card_type": getattr(target, "card_type", ""),
        "group_type": target.group_type,
        "scrape_type": target.scrape_type,
        "local_season_number": target.local_season_number,
        "item_count": len(getattr(target, "item_ids", []) or []),
        "sample_paths": " ; ".join(list(getattr(target, "sample_paths", []) or [])[:2]),
    }


def _top(counter: Counter, limit: int = 20) -> str:
    return ", ".join(f"{key}: {value}" for key, value in counter.most_common(limit)) or "-"


def _rows(items: Iterable[dict], headers: list[str], limit: int = 40) -> list[str]:
    values = list(items)
    rows = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for item in values[:limit]:
        cells = []
        for header in headers:
            cell = str(item.get(header, "")).replace("|", "\\|").replace("\n", " ")
            cells.append(cell[:180])
        rows.append("| " + " | ".join(cells) + " |")
    if len(values) > limit:
        rows.append("| " + f"... omitted {len(values) - limit} rows" + " | " * (len(headers) - 1) + "|")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", default="", help="sample txt path or filename")
    parser.add_argument("--source-root", default="", help="pan115 mounted root used for real_path composition")
    parser.add_argument("--output", default=str(ROOT / "logs" / "pan115_import_audit.md"))
    args = parser.parse_args()

    sample = _safe_sample_path(args.sample or None)
    config = load_config(force_reload=True)
    adapter = get_source_adapter("pan115")
    snapshot = adapter.parse(str(sample), source_root=args.source_root or config.pan115_root or "")
    snapshot.import_family = "anime"

    plan = build_draft_import_plan(snapshot)
    plan.import_family = "anime"
    for item in plan.items:
        item.import_family = "anime"
    recognize_import_plan_media(plan)
    targets = build_scrape_targets(plan)

    video_items = [item for item in plan.items if item.resource_type == "video"]
    generated = [item for item in video_items if item.action == "generate_strm"]

    action_counts = Counter(item.action for item in video_items)
    group_counts = Counter(item.group_type or "<empty>" for item in video_items)
    generated_group_counts = Counter(item.group_type or "<empty>" for item in generated)
    show_counts = Counter(item.show_type or "<empty>" for item in generated)
    category_counts = Counter(_category(item.relative_path, item.source) or "<none>" for item in generated)
    target_counts = Counter(f"{target.scrape_type}/{target.group_type}" for target in targets)

    aux_not_ignored = []
    special_name_mismatches = []
    season_missing_numbers = []
    category_mismatch = []
    duplicate_episode_keys = defaultdict(list)
    standalone_parent_leaks = []

    for item in video_items:
        text = f"{item.relative_path} {item.title}"
        if AUX_PAT.search(text) and item.action != "ignore":
            aux_not_ignored.append({
                "path": item.relative_path,
                "action": item.action,
                "group": item.group_type,
                "title": item.title,
            })
        if item.action == "generate_strm" and item.group_type == "special":
            expected = _special_display_expected(item.relative_path)
            actual = item.title or ""
            if expected and actual and expected != actual and actual not in expected:
                special_name_mismatches.append({
                    "path": item.relative_path,
                    "expected_display": expected,
                    "current_title": actual,
                    "work": item.work_title,
                })
        if item.action == "generate_strm" and item.group_type == "season" and (
            item.season_number is None or item.episode_number is None
        ):
            season_missing_numbers.append({
                "path": item.relative_path,
                "work": item.work_title,
                "season": item.season_number,
                "episode": item.episode_number,
            })
        if item.action == "generate_strm" and item.group_type == "season":
            duplicate_episode_keys[(item.work_id, item.season_number, item.episode_number)].append(item)
        cat = _category(item.relative_path, item.source)
        if item.action == "generate_strm" and cat == "动画电影" and item.show_type != "anime_movie":
            category_mismatch.append({
                "path": item.relative_path,
                "show_type": item.show_type,
                "media_type": item.media_type,
                "group": item.group_type,
            })
        if item.action == "generate_strm" and cat in {"动画", "番剧", "TV", "TV动画"} and item.group_type != "movie" and item.show_type == "anime_movie":
            category_mismatch.append({
                "path": item.relative_path,
                "show_type": item.show_type,
                "media_type": item.media_type,
                "group": item.group_type,
            })

    duplicate_rows = []
    for key, items in duplicate_episode_keys.items():
        if len(items) > 1:
            duplicate_rows.append({
                "work": items[0].work_title,
                "season": key[1],
                "episode": key[2],
                "count": len(items),
                "sample": " ; ".join(item.relative_path for item in items[:3]),
            })

    for target in targets:
        local = (target.local_title or "").strip()
        group = (target.series_group or "").strip()
        subwork = (getattr(target, "source_subwork_dir", "") or "").strip()
        local_key = _title_key(local)
        group_key = _title_key(group)
        title_is_unrelated = bool(local_key and group_key and group_key not in local_key and local_key not in group_key)
        if target.card_type == "standalone" and title_is_unrelated and not subwork:
            standalone_parent_leaks.append(_scrape_target_dict(target))

    target_rows = [_scrape_target_dict(target) for target in targets[:80]]
    generated_samples = []
    for item in generated[:120]:
        generated_samples.append({
            "category": _category(item.relative_path, item.source),
            "container": _work_container(item.relative_path, item.source),
            "subwork": _source_subwork(item.relative_path, item.source),
            "work": item.work_title,
            "series_group": item.series_group,
            "group": item.group_type,
            "season": item.season_number,
            "episode": item.episode_number or item.special_number,
            "title": item.title,
            "path": item.relative_path,
        })

    report = [
        "# pan115 Import Audit",
        "",
        "## Summary",
        f"- sample: `{sample.name}`",
        f"- raw files: {len(snapshot.files)}",
        f"- videos: {len(video_items)}",
        f"- generated videos: {len(generated)}",
        f"- scrape targets: {len(targets)}",
        f"- video actions: {_top(action_counts)}",
        f"- video groups: {_top(group_counts)}",
        f"- generated groups: {_top(generated_group_counts)}",
        f"- generated show types: {_top(show_counts)}",
        f"- generated categories: {_top(category_counts)}",
        f"- scrape target types: {_top(target_counts)}",
        "",
        "## Suspicions",
        f"- auxiliary-like videos not ignored: {len(aux_not_ignored)}",
        f"- generated specials whose title does not preserve original stem: {len(special_name_mismatches)}",
        f"- generated seasons missing S/E numbers: {len(season_missing_numbers)}",
        f"- generated category/show_type mismatches: {len(category_mismatch)}",
        f"- duplicate generated season episode keys: {len(duplicate_rows)}",
        f"- standalone scrape targets with suspicious parent leakage: {len(standalone_parent_leaks)}",
        "",
    ]

    sections = [
        ("Auxiliary Not Ignored", aux_not_ignored, ["path", "action", "group", "title"]),
        ("Special Title Mismatch", special_name_mismatches, ["path", "expected_display", "current_title", "work"]),
        ("Season Missing Numbers", season_missing_numbers, ["path", "work", "season", "episode"]),
        ("Category Mismatch", category_mismatch, ["path", "show_type", "media_type", "group"]),
        ("Duplicate Generated Episodes", duplicate_rows, ["work", "season", "episode", "count", "sample"]),
        (
            "Standalone Parent Leakage",
            standalone_parent_leaks,
            ["series_group", "local_title", "source_subwork_dir", "group_type", "scrape_type", "local_season_number", "item_count", "sample_paths"],
        ),
        (
            "Scrape Target Samples",
            target_rows,
            ["series_group", "local_title", "source_subwork_dir", "card_type", "group_type", "scrape_type", "local_season_number", "item_count", "sample_paths"],
        ),
        (
            "Generated Video Samples",
            generated_samples,
            ["category", "container", "subwork", "work", "series_group", "group", "season", "episode", "title", "path"],
        ),
    ]
    for title, rows, headers in sections:
        report.append(f"## {title}")
        report.extend(_rows(rows, headers, limit=40))
        report.append("")

    payload = {
        "summary": {
            "sample": sample.name,
            "raw_files": len(snapshot.files),
            "videos": len(video_items),
            "generated_videos": len(generated),
            "scrape_targets": len(targets),
            "video_actions": dict(action_counts),
            "video_groups": dict(group_counts),
            "generated_groups": dict(generated_group_counts),
            "generated_show_types": dict(show_counts),
            "generated_categories": dict(category_counts),
            "scrape_target_types": dict(target_counts),
        },
        "suspicions": {
            "auxiliary_not_ignored": len(aux_not_ignored),
            "special_name_mismatches": len(special_name_mismatches),
            "season_missing_numbers": len(season_missing_numbers),
            "category_mismatch": len(category_mismatch),
            "duplicate_generated_episode_keys": len(duplicate_rows),
            "standalone_parent_leaks": len(standalone_parent_leaks),
        },
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(report), encoding="utf-8")
    output.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
