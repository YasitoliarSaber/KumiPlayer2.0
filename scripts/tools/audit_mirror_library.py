"""全量只读审计当前镜像库、来源基线与刮削映射。

默认把每个来源的最新全量计划与仍有效的作品级追更计划合并，避免用单次
追更切片审计整库。报告只写 JSON，不修改镜像、NFO 或 ScrapeMap。
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.atomic_json import write_json_atomic
from app.core.paths import get_mirror_root
from app.db.database import init_db
from app.import_plan.store import load_import_plan, load_latest_confirmed_import_plan
from app.scrape.integrity import audit_scrape_integrity
from app.scrape.store import load_scrape_map
from app.scrape.target_builder import build_scrape_targets
from app.tracking.store import list_tracking_bindings


def _path_key(value: str | Path) -> str:
    return str(Path(value).resolve()).casefold()


def _active_plans() -> list:
    plans = []
    seen_ids: set[str] = set()
    for source in ("pan115", "baidu", "local"):
        plan = load_latest_confirmed_import_plan(source)
        if plan and plan.plan_id not in seen_ids:
            plans.append(plan)
            seen_ids.add(plan.plan_id)

    init_db()
    for binding in list_tracking_bindings():
        if binding.tracking_state == "archived" or not binding.baseline_plan_id:
            continue
        plan = load_import_plan(plan_id=binding.baseline_plan_id)
        if plan and plan.plan_id not in seen_ids:
            plans.append(plan)
            seen_ids.add(plan.plan_id)
    return plans


def audit() -> dict:
    mirror_root = get_mirror_root().resolve()
    plans = _active_plans()
    expected_items = [
        item
        for plan in plans
        for item in plan.items
        if item.action == "generate_strm" and item.target_strm_path
    ]
    expected_by_path = {_path_key(item.target_strm_path): item for item in expected_items}
    actual_paths = list(mirror_root.rglob("*.strm")) if mirror_root.exists() else []
    actual_by_path = {_path_key(path): path for path in actual_paths}

    missing = [
        item.target_strm_path
        for key, item in expected_by_path.items()
        if key not in actual_by_path
    ]
    extra = [
        str(path)
        for key, path in actual_by_path.items()
        if key not in expected_by_path
    ]
    content_mismatches = []
    for key in expected_by_path.keys() & actual_by_path.keys():
        item = expected_by_path[key]
        try:
            content = actual_by_path[key].read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            content = ""
        if content != (item.real_path or "").strip():
            content_mismatches.append({
                "path": str(actual_by_path[key]),
                "expected": item.real_path,
                "actual": content,
            })

    nfo_counts: Counter[str] = Counter()
    invalid_nfo = []
    for nfo_path in mirror_root.rglob("*.nfo") if mirror_root.exists() else []:
        try:
            root = ET.parse(nfo_path).getroot()
            nfo_counts[root.tag] += 1
            if not (root.findtext("tmdbid") or "").strip():
                nfo_counts["missing_tmdbid"] += 1
        except (ET.ParseError, OSError) as exc:
            invalid_nfo.append({"path": str(nfo_path), "error": str(exc)})

    targets = []
    seen_target_paths: set[str] = set()
    for plan in plans:
        for target in build_scrape_targets(plan):
            target_key = _path_key(target.target_nfo_path) if target.target_nfo_path else target.scrape_target_id
            if target_key in seen_target_paths:
                continue
            seen_target_paths.add(target_key)
            targets.append(target)
    integrity = audit_scrape_integrity(targets, load_scrape_map())
    issue_categories = Counter(issue["code"] for issue in integrity["issues"])

    all_files = [path for path in mirror_root.rglob("*") if path.is_file()]
    extension_counts = Counter(path.suffix.casefold() or "<none>" for path in all_files)
    return {
        "mirror_root": str(mirror_root),
        "active_plans": [
            {
                "plan_id": plan.plan_id,
                "source": plan.source,
                "scope": plan.import_scope,
                "item_count": len(plan.items),
            }
            for plan in plans
        ],
        "mirror": {
            "file_count": len(all_files),
            "extension_counts": dict(extension_counts),
            "expected_strm_count": len(expected_by_path),
            "actual_strm_count": len(actual_by_path),
            "missing_strm": missing,
            "unowned_strm": extra,
            "content_mismatches": content_mismatches,
        },
        "nfo": {
            "root_tag_counts": dict(nfo_counts),
            "invalid": invalid_nfo,
        },
        "scrape_integrity": integrity,
        "manual_review": {
            "counts": dict(issue_categories),
            "items": integrity["issues"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "audits" / "mirror_library_audit_latest.json",
    )
    args = parser.parse_args()
    report = audit()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(args.output, report)
    mirror_summary = report["mirror"]
    print(json.dumps({
        "output": str(args.output),
        "mirror": {
            "file_count": mirror_summary["file_count"],
            "extension_counts": mirror_summary["extension_counts"],
            "expected_strm_count": mirror_summary["expected_strm_count"],
            "actual_strm_count": mirror_summary["actual_strm_count"],
            "missing_strm_count": len(mirror_summary["missing_strm"]),
            "unowned_strm_count": len(mirror_summary["unowned_strm"]),
            "content_mismatch_count": len(mirror_summary["content_mismatches"]),
        },
        "nfo": report["nfo"],
        "integrity_summary": report["scrape_integrity"]["summary"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
