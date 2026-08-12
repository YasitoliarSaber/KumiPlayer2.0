"""Read-only audit for directory-tree recognition and scrape targets."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.import_plan.service import build_preview
from app.scrape.target_builder import build_scrape_targets
from app.sources.baidu import BaiduAdapter
from app.sources.pan115 import Pan115Adapter
from app.api.sources import _build_and_recognize_plan
from app.core.atomic_json import write_json_atomic


NAMING_PATTERNS = {
    "sxxeyy": re.compile(r"(?i)\bS\d{1,3}E\d{1,4}\b"),
    "ep": re.compile(r"(?i)(?:^|[\s._\-\[])EP?\s*\d+(?:\.5)?(?:\D|$)"),
    "bracket_number": re.compile(r"[\[【(（]\s*\d{1,4}(?:\.5)?\s*[\]】)）]"),
    "half_episode": re.compile(r"(?<!\d)\d+\.5(?!\d)"),
    "end": re.compile(r"(?i)(?:^|[\s._\-\[])END(?:[\s._\-\]]|$)"),
    "ova_oad_sp": re.compile(r"(?i)(?:^|\W)(?:OVA|OAD|SP)(?:\W|$)"),
    "ncop_nced": re.compile(r"(?i)(?:NCOP|NCED)"),
    "pv_cm": re.compile(r"(?i)(?:^|\W)(?:PV|CM)(?:\W|$)"),
}


def audit(source: str, input_path: Path, series_filter: str = "") -> dict:
    adapter = Pan115Adapter() if source == "pan115" else BaiduAdapter()
    snapshot = adapter.parse(str(input_path), "X:\\audit")
    snapshot.import_family = "anime"
    plan = _build_and_recognize_plan(snapshot)
    preview = build_preview(plan)
    targets = build_scrape_targets(plan)

    video_items = [item for item in plan.items if item.resource_type == "video"]
    generated = [item for item in video_items if item.action == "generate_strm"]
    missing_episode = [
        item for item in generated
        if item.group_type == "season" and item.episode_number is None
    ]
    generic_titles = {
        "", "season", "season 0", "season 1", "season 2", "special", "specials", "movie"
    }
    generic_targets = [
        target for target in targets
        if " ".join((target.scrape_title or "").replace("_", " ").split()).casefold() in generic_titles
    ]
    titled_episodes = [
        item for item in generated
        if item.group_type in {"season", "special"} and (item.title or "").strip()
    ]

    season_groups: dict[str, set[int]] = defaultdict(set)
    for item in generated:
        if item.group_type == "season" and item.season_number is not None:
            season_groups[item.series_group or item.work_title].add(int(item.season_number))

    issue_counts = Counter(issue.code for issue in preview.issues)
    review_samples = [
        {
            "path": item.relative_path,
            "work_title": item.work_title,
            "series_group": item.series_group,
            "group_type": item.group_type,
            "season": item.season_number,
            "episode": item.episode_number,
            "warnings": item.warnings,
        }
        for item in video_items if item.needs_review
    ][:30]

    normalized_filter = series_filter.casefold().strip()
    target_details = []
    for target in targets:
        searchable = " ".join(
            filter(None, (target.series_group, target.local_title, target.scrape_title))
        ).casefold()
        if normalized_filter and normalized_filter not in searchable:
            continue
        target_items = [item for item in plan.items if item.id in set(target.item_ids)]
        episode_numbers = [
            item.episode_number or item.special_number
            for item in target_items
            if (item.episode_number or item.special_number) is not None
        ]
        target_details.append({
            "series_group": target.series_group,
            "local_title": target.local_title,
            "scrape_title": target.scrape_title,
            "scrape_year": target.scrape_year,
            "scrape_type": target.scrape_type,
            "group_type": target.group_type,
            "local_season": target.local_season_number,
            "source_subwork_dir": target.source_subwork_dir,
            "item_count": len(target_items),
            "unique_episode_count": target.local_episode_count,
            "episode_numbers": sorted(episode_numbers),
            "duplicate_episode_numbers": sorted(
                number for number, count in Counter(episode_numbers).items() if count > 1
            ),
            "items": [
                {
                    "relative_path": item.relative_path,
                    "season": item.season_number,
                    "episode": item.episode_number,
                    "title": item.title,
                    "target_filename": item.target_filename,
                }
                for item in target_items[:4]
            ],
        })

    return {
        "source": source,
        "input": str(input_path),
        "snapshot": {"files": snapshot.file_count, "videos": snapshot.video_count},
        "items": {
            "videos": len(video_items),
            "generate_strm": len(generated),
            "needs_review": preview.summary.get("needs_review_count", 0),
            "missing_episode": len(missing_episode),
            "actions": Counter(item.action for item in video_items),
            "group_types": Counter(item.group_type or "<empty>" for item in video_items),
            "card_types": Counter(item.card_type or "<empty>" for item in generated),
            "explicit_episode_titles": len(titled_episodes),
        },
        "preview_issues": issue_counts,
        "naming_patterns": {
            name: sum(1 for item in video_items if pattern.search(Path(item.relative_path).name))
            for name, pattern in NAMING_PATTERNS.items()
        },
        "targets": {
            "count": len(targets),
            "types": Counter(target.group_type for target in targets),
            "without_year": sum(1 for target in targets if target.scrape_year is None),
            "generic_titles": [target.scrape_title for target in generic_targets],
        },
        "multi_season_series": {
            title: sorted(seasons)
            for title, seasons in season_groups.items()
            if len(seasons) > 1
        },
        "review_samples": review_samples,
        "missing_episode_samples": [item.relative_path for item in missing_episode[:30]],
        "target_details": target_details,
        "suspicious_long_seasons": [
            {
                "series_group": target.series_group,
                "season": target.local_season_number,
                "item_count": len(target.item_ids),
                "scrape_title": target.scrape_title,
            }
            for target in targets
            if target.group_type == "season" and len(target.item_ids) > 30
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", choices=("pan115", "baidu"))
    parser.add_argument("input", type=Path)
    parser.add_argument("--series", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(args.source, args.input, args.series)
    rendered = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        default=dict,
    )
    if args.output:
        write_json_atomic(args.output, report)
        print(args.output)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
