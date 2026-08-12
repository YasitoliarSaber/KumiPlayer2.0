# -*- coding: utf-8 -*-
"""M04S 正片季集识别返修测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# One Room
# ============================================================

def test_one_room_s1():
    """[LP-Raws] One Room 01 (BDRip...).mkv -> S01E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[LP-Raws] One Room 01 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room [Ma10p_1080p]/[LP-Raws] One Room 01 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 1, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_one_room_s2():
    """[LP-Raws] One Room S2 01 (BDRip...).mkv -> S02E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[LP-Raws] One Room S2 01 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room S2 [Ma10p_1080p]/[LP-Raws] One Room S2 01 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 2, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_one_room_s3():
    """[LP-Raws] One Room S3 03 (BDRip...).mkv -> S03E03"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[LP-Raws] One Room S3 03 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room S3 [Ma10p_1080p]/[LP-Raws] One Room S3 03 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 3, f"season: {guess.season_number}"
    assert guess.episode_number == 3, f"episode: {guess.episode_number}"


def test_one_room_s2_00_not_season():
    """[LP-Raws] One Room S2 00 (BDRip...).mkv -> 不得识别为 season episode 0"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[LP-Raws] One Room S2 00 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room S2 [Ma10p_1080p]/[LP-Raws] One Room S2 00 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
    )
    assert guess.group_type != "season" or guess.episode_number != 0, f"00 不应为 season episode 0: {guess.group_type} {guess.episode_number}"


def test_one_room_s2_nced():
    """[LP-Raws] One Room S2 NCED03 (BDRip...).mkv -> op_ed"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[LP-Raws] One Room S2 NCED03 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room S2 [Ma10p_1080p]/[LP-Raws] One Room S2 NCED03 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
    )
    assert guess.group_type == "ignored", f"group_type: {guess.group_type}"


def test_one_room_s2_pv():
    """[LP-Raws] One Room S2 PV2 (BDRip...).mkv -> auxiliary"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[LP-Raws] One Room S2 PV2 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room S2 [Ma10p_1080p]/[LP-Raws] One Room S2 PV2 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
    )
    assert guess.group_type == "auxiliary", f"group_type: {guess.group_type}"
    assert guess.season_number == 0


def test_one_room_s3_menu():
    """[LP-Raws] One Room S3 Menu01 (BDRip...).mkv -> auxiliary"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[LP-Raws] One Room S3 Menu01 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room S3 [Ma10p_1080p]/[LP-Raws] One Room S3 Menu01 (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
    )
    assert guess.group_type == "auxiliary", f"group_type: {guess.group_type}"
    assert guess.season_number == 0


# ============================================================
# Mushoku Tensei
# ============================================================

def test_filename_s2_bracket_episode_is_season_two():
    """文件名明确 S2 且集数为 [01] 时，应识别为第2季。"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[VCB-Studio] Mushoku Tensei S2 ~Isekai Ittara Honki Dasu~ [01][Hi10p_1080p][x264_flac].mkv",
        relative_path="动画/无职转生2/[VCB-Studio] Mushoku Tensei S2 ~Isekai Ittara Honki Dasu~ [01][Hi10p_1080p][x264_flac].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 2, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_title_suffix_number_alone_is_not_season_marker():
    """目录名末尾裸数字 2 可能是标题内容，不能单独推断为第2季。"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[VCB-Studio] Mushoku Tensei [01][Hi10p_1080p][x264_flac].mkv",
        relative_path="动画/无职转生2/[VCB-Studio] Mushoku Tensei [01][Hi10p_1080p][x264_flac].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 1, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


# ============================================================
# Silent Witch
# ============================================================

def test_silent_witch_ep01():
    """Silent Witch - 01 [BDRip...].mkv -> S01E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - 01 [BDRip][HEVC-10bit 1080p][CHI_JPN].mkv",
        relative_path="动画/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto [BDRip][HEVC-10bit FLAC][CHI_JPN]/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - 01 [BDRip][HEVC-10bit 1080p][CHI_JPN].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_silent_witch_ep13():
    """Silent Witch - 13 [BDRip...].mkv -> S01E13"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - 13 [BDRip][HEVC-10bit 1080p][CHI_JPN].mkv",
        relative_path="动画/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto [BDRip][HEVC-10bit FLAC][CHI_JPN]/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - 13 [BDRip][HEVC-10bit 1080p][CHI_JPN].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.episode_number == 13, f"episode: {guess.episode_number}"


def test_silent_witch_nced():
    """Silent Witch - NCED.mkv -> op_ed"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - NCED.mkv",
        relative_path="动画/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto [BDRip][HEVC-10bit FLAC][CHI_JPN]/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - NCED.mkv",
    )
    assert guess.group_type == "ignored", f"group_type: {guess.group_type}"


def test_silent_witch_ncop():
    """Silent Witch - NCOP.mkv -> op_ed"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - NCOP.mkv",
        relative_path="动画/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto [BDRip][HEVC-10bit FLAC][CHI_JPN]/[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto - NCOP.mkv",
    )
    assert guess.group_type == "ignored", f"group_type: {guess.group_type}"


# ============================================================
# 某科学的超电磁炮
# ============================================================

def test_railgun_s1():
    """[4K_NW] 某科学的超电磁炮 第1季 01.mkv -> S01E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[4K_NW] 某科学的超电磁炮 第1季 01.mkv",
        relative_path="动画/某科学的超电磁炮.S1-S3（将更新）/某科学的超电磁炮.[S1].2009/[4K_NW] 某科学的超电磁炮 第1季 01.mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 1, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_railgun_s2():
    """[4K_NW] 某科学的超电磁炮S 第2季 01 .mkv -> S02E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[4K_NW] 某科学的超电磁炮S 第2季 01 .mkv",
        relative_path="动画/某科学的超电磁炮.S1-S3（将更新）/某科学的超电磁炮S.[S2].2013/[4K_NW] 某科学的超电磁炮S 第2季 01 .mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 2, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_railgun_s3():
    """[4K_NW] 某科学的超电磁炮T 第3季 01.mkv -> S03E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[4K_NW] 某科学的超电磁炮T 第3季 01.mkv",
        relative_path="动画/某科学的超电磁炮.S1-S3（将更新）/某科学的超电磁炮T.[S3].2020/[4K_NW] 某科学的超电磁炮T 第3季 01.mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 3, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


# ============================================================
# 魔法禁书目录
# ============================================================

def test_index_s1():
    """目录：魔法禁书目录 I.2008 / 文件：[CZ] Toaru Majutsu no Index [01]...mkv -> S01E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[CZ] Toaru Majutsu no Index [01][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/魔法禁书目录.S1-S3+剧场版/魔法禁书目录 I.2008/[CZ] Toaru Majutsu no Index [01][Ma10p_2160p][x265_flac_ass].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 1, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_index_s2():
    """目录：魔法禁书目录 II.2010 / 文件：[CZ] Toaru Majutsu no Index II [01]...mkv -> S02E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[CZ] Toaru Majutsu no Index II [01][Ma10p_2160][]x265_flac_ass].mkv",
        relative_path="动画/魔法禁书目录.S1-S3+剧场版/魔法禁书目录 II.2010/[CZ] Toaru Majutsu no Index II [01][Ma10p_2160][]x265_flac_ass].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 2, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_index_s3():
    """目录：魔法禁书目录 III.2018 / 文件：[CZ] Toaru Majutsu no Index III [01]...mkv -> S03E01"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[CZ] Toaru Majutsu no Index III [01][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/魔法禁书目录.S1-S3+剧场版/魔法禁书目录 III.2018/[CZ] Toaru Majutsu no Index III [01][Ma10p_2160p][x265_flac_ass].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 3, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


# ============================================================
# Dandadan / Seihantai
# ============================================================

def test_dandadan():
    """Dandadan - 13 [...].mkv -> season, episode=13"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[Nekomoe kissaten&LoliHouse] Dandadan - 13 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
        relative_path="动画/[Nekomoe kissaten&LoliHouse] Dandadan [13-24][WebRip 1080p HEVC-10bit AAC]/[Nekomoe kissaten&LoliHouse] Dandadan - 13 [WebRip 1080p HEVC-10bit AAC ASSx2].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.episode_number == 13, f"episode: {guess.episode_number}"


def test_seihantai():
    """Seihantai na Kimi to Boku - 01 [...].mkv -> season, episode=1"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[SweetSub] Seihantai na Kimi to Boku - 01 [WebRip 1080p HEVC-10bit AAC CHS&CHT].mkv",
        relative_path="动画/[SweetSub] Seihantai na Kimi to Boku [01-12][WebRip][1080P][HEVC 10bit][CHS&CHT]/[SweetSub] Seihantai na Kimi to Boku - 01 [WebRip 1080p HEVC-10bit AAC CHS&CHT].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


# ============================================================
# OP/ED / SPs 优先级保持
# ============================================================

def test_ncop_priority():
    """NCOP 继续是 OP/ED"""
    from app.recognition.media import recognize_media
    guess = recognize_media(filename="NCOP01.mkv", relative_path="动画/test/NCOP01.mkv")
    assert guess.group_type == "ignored"


def test_pv_priority():
    """PV 优先识别为附属视频，不进入 SPs"""
    from app.recognition.media import recognize_media
    guess = recognize_media(filename="PV01.mkv", relative_path="动画/test/PV01.mkv")
    assert guess.group_type == "auxiliary"
    assert guess.season_number == 0


def test_real_auxiliary_batch_does_not_need_review():
    """真实样本中的 Menu/PV/CM/Trailer 不应进入人工确认。"""
    from app.import_plan.models import ImportPlanItem, ImportPlan
    from app.import_plan.service import build_preview
    from app.recognition.plan_recognizer import recognize_import_plan_media

    filenames = [
        "[CZ&MAI] Vinland Saga [Menu01][Ma10p_2160p][x265_flac].mkv",
        "[CZ&MAI] Vinland Saga [PV01][Ma10p_2160p][x265_flac].mkv",
        "[CZ&MAI] Vinland Saga S2 [CM][Ma10p_2160p][x265_flac].mkv",
        "[VCB-Studio] Sword Art Online Extra Edition [Menu][Hi10p_1080p][x264_flac].mkv",
        "[VCB-Studio] Sword Art Online [02 Long Trailer][Ma10p_1080p][x265_flac].mkv",
        "[UHA-WINGS&RATH&VCB-Studio] Sword Art Online Alicization [PV02][Ma10p_1080p][x265_flac].mkv",
    ]
    items = [
        ImportPlanItem(
            id=f"v{idx}",
            plan_id="p1",
            raw_file_id=f"r{idx}",
            source="pan115",
            relative_path=f"动画/自动附属视频/{filename}",
            real_path=f"H:\\115open\\动画\\自动附属视频\\{filename}",
            resource_type="video",
            action="generate_strm",
        )
        for idx, filename in enumerate(filenames, start=1)
    ]
    plan = ImportPlan(plan_id="p1", source="pan115", status="draft", items=items)
    recognize_import_plan_media(plan)

    assert all(item.group_type == "auxiliary" for item in items)
    assert all(item.needs_review is False for item in items)
    preview = build_preview(plan)
    assert "needs_review" not in [issue.code for issue in preview.issues]
    assert "missing_group_type" not in [issue.code for issue in preview.issues]


def test_sp_priority():
    """SP 继续是 SPs"""
    from app.recognition.media import recognize_media
    guess = recognize_media(filename="SP01.mkv", relative_path="动画/test/SP01.mkv")
    assert guess.group_type == "special"


# ============================================================
# 重复集号检测
# ============================================================

def test_duplicate_episode_air():
    """AIR S01E05：正片目录一份 season，OP＆ED 目录一份 op_ed → 不再重复"""
    from app.import_plan.models import ImportPlanItem, ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        ImportPlanItem(
            id="v1", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/AIR.2005/AIR.S01E05.翼.mkv",
            real_path="H:\\115open\\动画\\AIR.2005\\AIR.S01E05.翼.mkv",
            resource_type="video", action="generate_strm",
        ),
        ImportPlanItem(
            id="v2", plan_id="p1", raw_file_id="r2", source="pan115",
            relative_path="动画/AIR.2005/OP＆ED/AIR.S01E05.翼.mkv",
            real_path="H:\\115open\\动画\\AIR.2005\\OP＆ED\\AIR.S01E05.翼.mkv",
            resource_type="video", action="generate_strm",
        ),
    ]
    plan = ImportPlan(plan_id="p1", source="pan115", status="draft", items=items)
    recognize_import_plan_media(plan)

    # 正片目录的条目应为 season
    assert items[0].group_type == "season", f"v1 group_type: {items[0].group_type}"
    # OP＆ED 目录的条目应为 op_ed（路径上下文优先）
    assert items[1].group_type == "ignored", f"v2 group_type: {items[1].group_type}"
    # 不再是同一季集的重复
    assert items[0].group_type != items[1].group_type


def test_duplicate_episode_kon():
    """轻音少女 S01E12：双版本 → 自动保留一个，跳过重复版本"""
    from app.import_plan.models import ImportPlanItem, ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        ImportPlanItem(
            id="v1", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/轻音少女.S1-S2+剧场版/轻音少女 - S01E12 - 轻音！.mkv",
            real_path="H:\\115open\\动画\\轻音少女.S1-S2+剧场版\\轻音少女 - S01E12 - 轻音！.mkv",
            resource_type="video", action="generate_strm",
        ),
        ImportPlanItem(
            id="v2", plan_id="p1", raw_file_id="r2", source="pan115",
            relative_path="动画/轻音少女.S1-S2+剧场版/[YE] K-ON! [12][Ma10p_1080p].mkv",
            real_path="H:\\115open\\动画\\轻音少女.S1-S2+剧场版\\[YE] K-ON! [12][Ma10p_1080p].mkv",
            resource_type="video", action="generate_strm",
        ),
    ]
    plan = ImportPlan(plan_id="p1", source="pan115", status="draft", items=items)
    recognize_import_plan_media(plan)

    kept = [item for item in items if item.action == "generate_strm"]
    ignored = [item for item in items if item.action == "ignore"]
    assert len(kept) == 1
    assert len(ignored) == 1
    assert any("已自动保留" in w for w in kept[0].warnings), f"kept warnings: {kept[0].warnings}"
    assert any("已自动跳过" in w for w in ignored[0].warnings), f"ignored warnings: {ignored[0].warnings}"
    assert kept[0].needs_review is False
    assert ignored[0].needs_review is False


def test_duplicate_episode_does_not_cross_distinct_work_roots():
    """同名作品位于不同作品根时，各目录都必须保留自己的正片。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.recognition.plan_recognizer import _auto_resolve_duplicate_episodes

    items = [
        ImportPlanItem(
            id="copy-a", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/作品副本 A/Show.S01E01.mkv",
            resource_type="video", action="generate_strm", card_type="main_series",
            group_type="season", work_id="same-recognition-id",
            season_number=1, episode_number=1,
        ),
        ImportPlanItem(
            id="copy-b", plan_id="p1", raw_file_id="r2", source="pan115",
            relative_path="动画/作品副本 B/Show.S01E01.mkv",
            resource_type="video", action="generate_strm", card_type="main_series",
            group_type="season", work_id="same-recognition-id",
            season_number=1, episode_number=1,
        ),
    ]
    plan = ImportPlan(plan_id="p1", source="pan115", status="draft", items=items)

    _auto_resolve_duplicate_episodes(plan)

    assert [item.action for item in items] == ["generate_strm", "generate_strm"]


def test_duplicate_episode_uses_shared_work_root_across_legacy_work_ids():
    """同一作品根内的旧 work_id 漂移不能绕过重复集去重。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.recognition.plan_recognizer import _auto_resolve_duplicate_episodes

    items = [
        ImportPlanItem(
            id="version-a", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/同一作品/Show.S01E01.1080p.mkv",
            resource_type="video", action="generate_strm", card_type="main_series",
            group_type="season", work_id="legacy-id-a",
            season_number=1, episode_number=1,
        ),
        ImportPlanItem(
            id="version-b", plan_id="p1", raw_file_id="r2", source="pan115",
            relative_path="动画/同一作品/Show.S01E01.720p.mkv",
            resource_type="video", action="generate_strm", card_type="main_series",
            group_type="season", work_id="legacy-id-b",
            season_number=1, episode_number=1,
        ),
    ]
    plan = ImportPlan(plan_id="p1", source="pan115", status="draft", items=items)

    _auto_resolve_duplicate_episodes(plan)

    assert len([item for item in items if item.action == "generate_strm"]) == 1
    assert len([item for item in items if item.action == "ignore"]) == 1


def test_no_false_duplicate_index():
    """魔法禁书目录 I/II/III 不应因季号塌陷产生 S01 重复"""
    from app.import_plan.models import ImportPlanItem, ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        ImportPlanItem(
            id="v1", plan_id="p1", raw_file_id="r1", source="pan115",
            relative_path="动画/魔法禁书目录.S1-S3+剧场版/魔法禁书目录 I.2008/[CZ] Index [01].mkv",
            real_path="H:\\115open\\动画\\魔法禁书目录.S1-S3+剧场版\\魔法禁书目录 I.2008\\[CZ] Index [01].mkv",
            resource_type="video", action="generate_strm",
        ),
        ImportPlanItem(
            id="v2", plan_id="p1", raw_file_id="r2", source="pan115",
            relative_path="动画/魔法禁书目录.S1-S3+剧场版/魔法禁书目录 II.2010/[CZ] Index II [01].mkv",
            real_path="H:\\115open\\动画\\魔法禁书目录.S1-S3+剧场版\\魔法禁书目录 II.2010\\[CZ] Index II [01].mkv",
            resource_type="video", action="generate_strm",
        ),
    ]
    plan = ImportPlan(plan_id="p1", source="pan115", status="draft", items=items)
    recognize_import_plan_media(plan)

    # 不应有重复 warning（不同季号）
    assert not any("多个来源文件" in w for w in items[0].warnings), f"v1 不应有重复 warning: {items[0].warnings}"
    assert not any("多个来源文件" in w for w in items[1].warnings), f"v2 不应有重复 warning: {items[1].warnings}"


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_one_room_s1, test_one_room_s2, test_one_room_s3,
        test_one_room_s2_00_not_season, test_one_room_s2_nced, test_one_room_s2_pv, test_one_room_s3_menu,
        test_silent_witch_ep01, test_silent_witch_ep13, test_silent_witch_nced, test_silent_witch_ncop,
        test_railgun_s1, test_railgun_s2, test_railgun_s3,
        test_index_s1, test_index_s2, test_index_s3,
        test_dandadan, test_seihantai,
        test_ncop_priority, test_pv_priority, test_sp_priority,
        test_duplicate_episode_air, test_duplicate_episode_kon, test_no_false_duplicate_index,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
