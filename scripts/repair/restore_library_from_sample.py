# -*- coding: utf-8 -*-
"""从样例目录树恢复 115 媒体库数据。

用途：
- 当 data/mirror 或 data/cache/library_index.json 丢失时，从 docs/samples/01动画_文件目录.txt
  重新解析、识别、确认、生成镜像并重建媒体库索引。
- 只用于本机恢复，不联网、不刮削、不修改真实网盘源文件。
"""

from __future__ import annotations

import shutil
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import load_config
from app.import_plan.service import build_preview, confirm_plan
from app.import_plan.store import save_import_plan
from app.library.service import rescan_library
from app.mirror.generator import generate_mirror
from app.raw.store import save_raw_snapshot
from app.recognition.plan_recognizer import recognize_import_plan_media
from app.recognition.planner import build_draft_import_plan
from app.sources.pan115 import Pan115Adapter


TREE_FILE = PROJECT_ROOT / "samples" / "根目录20260612200150_目录树.txt"


def _is_under(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _prefer_item(item) -> tuple:
    """多版本冲突时的保守默认选择。

    优先保留已整理/中文命名的文件；字幕组原始发布名作为备选版本暂不导入。
    """
    name = item.relative_path.replace("\\", "/").split("/")[-1]
    starts_with_group_tag = name.startswith("[")
    is_processed = "_processed" in name.lower()
    is_chinese_curated = not starts_with_group_tag
    return (
        0 if is_chinese_curated else 1,
        0 if is_processed else 1,
        len(name),
        name.lower(),
    )


def _resolve_blockers(plan) -> dict:
    """处理无法自动确认的阻断项。

    只做两类安全处理：
    - 必要字段缺失的 generate_strm 视频改为 ignore，避免生成错误位置；
    - 同 work_id/season/episode 的多版本只保留一个，其余 ignore，避免路径冲突。
    """
    ignored_invalid = 0
    ignored_duplicates = 0

    for item in plan.items:
        if item.resource_type != "video" or item.action != "generate_strm":
            continue
        if not item.work_title or not item.group_type:
            item.action = "ignore"
            item.needs_review = False
            item.warnings.append("恢复脚本：缺少必要媒体字段，未导入媒体库")
            ignored_invalid += 1

    groups = defaultdict(list)
    for item in plan.items:
        if (
            item.resource_type == "video"
            and item.action == "generate_strm"
            and item.group_type == "season"
            and item.work_id
            and item.season_number is not None
            and item.episode_number is not None
        ):
            groups[(item.work_id, item.season_number, item.episode_number)].append(item)

    for items in groups.values():
        if len(items) <= 1:
            continue
        kept = sorted(items, key=_prefer_item)[0]
        for item in items:
            if item.id == kept.id:
                item.needs_review = False
                continue
            item.action = "ignore"
            item.needs_review = False
            item.warnings.append("恢复脚本：同一季集存在多版本，暂未导入该备选版本")
            ignored_duplicates += 1

    for item in plan.items:
        if item.resource_type == "video" and item.action == "generate_strm":
            # 剩余可生成条目的 warning 只作为线索，不阻断恢复。
            item.needs_review = False

    return {
        "ignored_invalid": ignored_invalid,
        "ignored_duplicates": ignored_duplicates,
    }


def main() -> int:
    if not TREE_FILE.exists():
        print(f"ERROR tree file not found: {TREE_FILE}")
        return 1

    config = load_config(force_reload=True)
    source_root = config.pan115_root or r"H:\115open"
    mirror_root = Path(config.mirror_dir) if config.mirror_dir else PROJECT_ROOT / "data" / "mirror"

    print(f"TREE_FILE={TREE_FILE}")
    print(f"SOURCE_ROOT={source_root}")
    print(f"MIRROR_ROOT={mirror_root}")

    snapshot = Pan115Adapter().parse(str(TREE_FILE), source_root)
    save_raw_snapshot(snapshot)
    print(f"SNAPSHOT files={snapshot.file_count} videos={snapshot.video_count}")

    plan = build_draft_import_plan(snapshot)
    recognize_import_plan_media(plan)
    before = build_preview(plan)
    print(f"PREVIEW_BEFORE issues={len(before.issues)} summary={before.summary}")

    resolve_summary = _resolve_blockers(plan)
    after = build_preview(plan)
    errors = [issue for issue in after.issues if issue.level == "error"]
    print(f"RESOLVE {resolve_summary}")
    print(f"PREVIEW_AFTER issues={len(after.issues)} errors={len(errors)} summary={after.summary}")
    if errors:
        for issue in errors[:20]:
            print(f"ERROR_ISSUE {issue.code}: {issue.message}")
        return 2

    confirmed, error = confirm_plan(plan)
    if error:
        print(f"CONFIRM_ERROR {error}")
        return 3

    source_mirror_dir = mirror_root / "115"
    if source_mirror_dir.exists():
        if not _is_under(source_mirror_dir, PROJECT_ROOT):
            print(f"ERROR refusing to remove outside project: {source_mirror_dir}")
            return 4
        shutil.rmtree(source_mirror_dir)

    result = generate_mirror(confirmed, str(mirror_root))
    print(
        "MIRROR "
        f"status={result.status} generated={result.generated_count} "
        f"skipped={result.skipped_count} failed={result.failed_count}"
    )
    if result.errors[:20]:
        for err in result.errors[:20]:
            print(f"MIRROR_ERROR {err}")

    # generate_mirror 会把计划标为 executed；已修复扫描逻辑，executed 也可作为索引基准。
    save_import_plan(confirmed)

    library_result = rescan_library("pan115")
    print(f"LIBRARY {library_result}")

    index_path = PROJECT_ROOT / "data" / "cache" / "library_index.json"
    print(f"INDEX_EXISTS={index_path.exists()} INDEX_PATH={index_path}")

    return 0 if library_result.get("work_count", 0) > 0 else 5


if __name__ == "__main__":
    raise SystemExit(main())
