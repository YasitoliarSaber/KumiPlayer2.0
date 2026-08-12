# -*- coding: utf-8 -*-
"""使用已确认的 TMDB 映射重写受影响作品的 NFO。"""

from app.import_plan.store import load_import_plan
from app.library.service import rescan_library
from app.scrape.service import execute_scrape
from app.scrape.target_builder import build_scrape_targets
from app.scrape.tmdb_client import TMDBClient


MAPPINGS = {
    "君主·埃尔梅罗二世事件簿 -魔眼收集列车 Grace note": (85368, "tv"),
    "明天，美食广场见。": (280366, "tv"),
    "玲音.Serial.Experiments.Lain": (1087, "tv"),
    "莉可丽丝": (154494, "tv"),
    "莉可丽丝：友谊是时间的窃贼": (154494, "tv"),
    "败犬女主太多了！": (241535, "tv"),
    "辉夜大小姐想让我告白": (83121, "tv"),
    "间谍过家家": (120089, "tv"),
}


def main() -> int:
    plan = load_import_plan(source="pan115")
    if plan is None:
        raise SystemExit("找不到 115 导入计划")
    targets = build_scrape_targets(plan)
    client = TMDBClient()
    succeeded = 0
    failed = []
    try:
        for target in targets:
            mapping = MAPPINGS.get(target.local_title)
            if mapping is None:
                continue
            # 当前 TMDB 条目尚无辉夜第 4 季数据，保留本地结构等待后续更新。
            if target.local_title == "辉夜大小姐想让我告白" and (target.local_season_number or 0) > 3:
                continue
            tmdb_id, tmdb_type = mapping
            season = target.local_season_number if tmdb_type == "tv" else None
            try:
                execute_scrape(
                    target=target,
                    tmdb_id=tmdb_id,
                    tmdb_type=tmdb_type,
                    tmdb_season_number=season,
                    selected_by="repair",
                    tmdb_client=client,
                    include_episode=True,
                    rescan_after=False,
                    artwork_mode="remote",
                )
                succeeded += 1
                print(f"完成: {target.local_title} / Season {season}")
            except Exception as exc:
                failed.append((target.local_title, season, str(exc)))
                print(f"失败: {target.local_title} / Season {season}: {exc}")
    finally:
        client.close()

    result = rescan_library(source="pan115")
    print(f"刮削完成 {succeeded}，失败 {len(failed)}；索引作品 {result['work_count']}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
