# -*- coding: utf-8 -*-
"""重建已确认受错误识别影响的镜像条目。"""

from __future__ import annotations

import argparse
from pathlib import Path

from app.core.paths import get_mirror_root
from app.import_plan.store import load_import_plan, save_import_plan
from app.library.service import rescan_library
from app.mirror.generator import generate_mirror
from app.recognition.plan_recognizer import recognize_import_plan_media


PLAN_ID = "f584ed78c9411146d418ccebabe48bc8"
PATH_MARKERS = (
    "败犬女主太多了",
    "君主·埃尔梅罗",
    "辉夜大小姐",
    "Spy x Family",
    "莉可丽丝",
    "Serial.Experiments.Lain",
    "See.You.Tomorrow.at.the.Food.Court",
)


def _affected(item) -> bool:
    path = item.relative_path or ""
    return any(marker.casefold() in path.casefold() for marker in PATH_MARKERS)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = load_import_plan(plan_id=PLAN_ID)
    if plan is None:
        raise SystemExit(f"找不到导入计划: {PLAN_ID}")

    affected = [item for item in plan.items if _affected(item)]
    mirror_root = get_mirror_root()
    stale_files: set[Path] = set()
    affected_dirs: set[Path] = set()
    for item in affected:
        if not item.target_strm_path:
            continue
        path = Path(item.target_strm_path)
        if not _inside(path, mirror_root):
            raise SystemExit(f"拒绝处理镜像目录外路径: {path}")
        stale_files.add(path)
        affected_dirs.add(path.parent)

    print(f"计划条目: {len(plan.items)}，受影响条目: {len(affected)}")
    print(f"待重建 STRM: {sum(path.exists() for path in stale_files)}")
    if not args.apply:
        return 0

    for path in stale_files:
        path.unlink(missing_ok=True)
    for directory in affected_dirs:
        for pattern in ("S??E??.nfo", "tvshow.nfo"):
            for path in directory.glob(pattern):
                path.unlink(missing_ok=True)

    # 这部作品曾整卡匹配到错误 TMDB 条目，系列级图片也必须清除。
    for directory in affected_dirs:
        if "君主" not in str(directory):
            continue
        for candidate in (directory, directory.parent):
            for name in ("poster.jpg", "fanart.jpg", "clearlogo.png", "tvshow.nfo"):
                (candidate / name).unlink(missing_ok=True)

    recognize_import_plan_media(plan)
    plan.status = "confirmed"
    save_import_plan(plan)
    mirror_result = generate_mirror(plan)
    index_result = rescan_library(source="pan115")
    print(
        f"镜像: {mirror_result.status}，生成 {mirror_result.generated_count}，"
        f"跳过 {mirror_result.skipped_count}，失败 {mirror_result.failed_count}"
    )
    print(
        f"索引: 作品 {index_result['work_count']}，分集 {index_result['episode_count']}，"
        f"缺失 STRM {index_result['missing_strm_count']}"
    )
    return 0 if mirror_result.failed_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
