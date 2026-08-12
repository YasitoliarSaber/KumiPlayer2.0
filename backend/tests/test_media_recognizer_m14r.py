# -*- coding: utf-8 -*-
"""M14 真实样本识别返修测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_baidu_animation_movie_category_is_movie():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="蓦然回首 (2024) 2160p.mkv",
        relative_path="动画电影/蓦然回首 (2024)/蓦然回首 (2024) 2160p.mkv",
        source="baidu",
    )

    assert guess.group_type == "movie"
    assert guess.card_type == "standalone"
    assert guess.media_type == "movie"
    assert guess.work_title == "蓦然回首"
    assert guess.year == 2024


def test_pan115_year_container_without_episode_is_movie():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[SRENIX] 100.Meters.2025.[2160P E-AC3 6.0].mkv",
        relative_path="动画/百米.2025/[SRENIX] 100.Meters.2025.[2160P E-AC3 6.0].mkv",
        source="pan115",
    )

    assert guess.group_type == "movie"
    assert guess.card_type == "standalone"
    assert guess.media_type == "movie"
    assert guess.work_title == "百米"
    assert guess.year == 2025


def test_year_container_with_release_tags_is_movie_even_without_year_in_filename():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[MAI] Paprika [Ma10p_2160p][x265_flac5.1_2ass].mkv",
        relative_path="动画/红辣椒.Paprika.2006/[MAI] Paprika [Ma10p_2160p][x265_flac5.1_2ass].mkv",
        source="pan115",
    )

    assert guess.group_type == "movie"
    assert guess.card_type == "standalone"
    assert guess.year == 2006


def test_one_room_plain_pv_is_auxiliary():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[LP-Raws] One Room PV (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        relative_path="动画/[LP-Raws] One Room/[LP-Raws] One Room [Ma10p_1080p]/Bonus/[LP-Raws] One Room PV (BDRip 1080p HEVC-YUV420P10 FLAC).mkv",
        source="pan115",
    )

    assert guess.group_type == "auxiliary"
    assert guess.season_number == 0


def test_movie_menu_video_is_auxiliary():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="BD_MENU.mkv",
        relative_path="动画电影/红猪.Porco.Rosso.1992/BD_MENU.mkv",
        source="pan115",
    )

    assert guess.group_type == "auxiliary"
    assert guess.season_number == 0


def test_animation_movie_folder_extras_share_movie_card_and_keep_distinct_titles():
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.recognition.plan_recognizer import recognize_import_plan_media

    def make_item(filename: str, relative_path: str) -> ImportPlanItem:
        return ImportPlanItem(
            id=f"id-{filename}",
            plan_id="plan-1",
            raw_file_id=f"raw-{filename}",
            source="pan115",
            relative_path=relative_path,
            real_path=f"H:/115open/{relative_path}",
            resource_type="video",
            action="generate_strm",
            confidence="high",
        )

    main = make_item(
        filename="[VCB-Studio] Koe no Katachi [Ma10p_1080p][x265_flac].mkv",
        relative_path="动画电影/声之形.2016/[VCB-Studio] Koe no Katachi [Ma10p_1080p][x265_flac].mkv",
    )
    pv = make_item(
        filename="[VCB-Studio] Koe no Katachi PV01 [Ma10p_1080p][x265_flac].mkv",
        relative_path="动画电影/声之形.2016/[VCB-Studio] Koe no Katachi PV01 [Ma10p_1080p][x265_flac].mkv",
    )
    m5 = make_item(
        filename="[VCB-Studio] Koe no Katachi M5 [Ma10p_1080p][x265_flac].mkv",
        relative_path="动画电影/声之形.2016/[VCB-Studio] Koe no Katachi M5 [Ma10p_1080p][x265_flac].mkv",
    )

    plan = ImportPlan(
        plan_id="plan-test-1",
        source="pan115",
        source_snapshot_id="snap-test-1",
        status="draft",
        items=[main, pv, m5],
    )
    recognize_import_plan_media(plan)

    assert main.group_type == "movie"
    assert main.card_type == "standalone"
    assert pv.group_type == "auxiliary"
    assert m5.group_type == "special"
    assert pv.work_id == main.work_id
    assert m5.work_id == main.work_id
    assert pv.card_type == "standalone"
    assert m5.card_type == "standalone"
    assert pv.show_type == "anime_movie"
    assert m5.show_type == "anime_movie"
    assert pv.title != m5.title
    assert "PV01" in pv.title
    assert "M5" in m5.title


def test_yuru_camp_heya_camp_pv_and_menu_do_not_enter_s00():
    from app.recognition.media import recognize_media

    for filename in (
        "[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp PV [Ma10p_1080p][x265_flac].mkv",
        "[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp Menu [Ma10p_1080p][x265_flac].mkv",
    ):
        guess = recognize_media(
            filename=filename,
            relative_path=f"[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [Ma10p_1080p]/SPs/{filename}",
            source="local",
        )

        assert guess.work_title == "Heya Camp"
        assert guess.series_group == "Yuru Camp"
        assert guess.card_type == "standalone"
        assert guess.group_type == "auxiliary"
        assert guess.season_number == 0


def test_beta_episode_is_sps():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[MAI] Steins;Gate [23β][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/命运石之门.S1-S2+剧场版+OVA/命运石之门.[S1].2011/[MAI] Steins;Gate [23β][Ma10p_2160p][x265_flac_ass].mkv",
        source="pan115",
    )

    assert guess.group_type == "special"


def test_s00e_is_sps():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="ReZero.S00E49.2021.2160P.BDRIP.mkv",
        relative_path="刮削好的动画/ReZero (2016) {tmdb-65942}/Season 0/ReZero.S00E49.2021.2160P.BDRIP.mkv",
        source="baidu",
    )

    assert guess.group_type == "special"


def test_spin_off_tv_episode_is_standalone_season_not_movie():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[4K_EA] 刀剑神域外传 Gun Gale Online 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/外传：Gun Gale Online/[4K_EA] 刀剑神域外传 Gun Gale Online 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        source="pan115",
    )

    assert guess.card_type == "standalone"
    assert guess.relation_type == "spin_off"
    assert guess.group_type == "season"
    assert guess.media_type == "tv"
    assert guess.work_title == "外传：Gun Gale Online"
    assert guess.series_group == "外传：Gun Gale Online"
    assert guess.belongs_to_series == "刀剑神域"
    assert guess.season_number == 1
    assert guess.episode_number == 1


def test_series_container_plus_spin_off_does_not_turn_main_season_into_spin_off():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/1.刀剑神域.[S1].2012/[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        source="pan115",
    )

    assert guess.card_type == "main_series"
    assert guess.relation_type == ""
    assert guess.group_type == "season"
    assert guess.work_title == "刀剑神域"
    assert guess.series_group == "刀剑神域"


def test_bracket_episode_with_version_suffix():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Sakurato] Watashi o Tabetai, Hitodenashi [01v2][HEVC-10bit 1080P AAC][CHS&CHT].mkv",
        relative_path="动画/[Sakurato][202509] Watashi o Tabetai, Hitodenashi [01-13 Fin][1080P][简繁内封]/[Sakurato] Watashi o Tabetai, Hitodenashi [01v2][HEVC-10bit 1080P AAC][CHS&CHT].mkv",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 1
    assert guess.episode_number == 1


def test_numbered_subtitle_group_folder_infers_second_season_for_bracket_episode():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 2 [06][Ma10p_1080p][x265_flac_aac].mkv",
        relative_path=(
            "动画/[VCB-Studio] KonoSuba/"
            "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 2 [Ma10p_1080p]/"
            "[VCB-Studio] Kono Subarashii Sekai ni Shukufuku wo! 2 [06][Ma10p_1080p][x265_flac_aac].mkv"
        ),
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 2
    assert guess.episode_number == 6


def test_chinese_season_number_beats_parent_s_marker():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[4K_EA] 刀剑神域 第4季 爱丽丝篇 异界战争 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/6.刀剑神域：Alicization篇 War of Underworld.[S3].2019/[4K_EA] 刀剑神域 第4季 爱丽丝篇 异界战争 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 4
    assert guess.episode_number == 1


def test_local_source_uses_first_directory_as_work_container():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="上伊那牡丹醉姿如百合09.mp4",
        relative_path="上伊那牡丹醉姿如百合/上伊那牡丹醉姿如百合09.mp4",
        source="local",
    )

    assert guess.work_title == "上伊那牡丹醉姿如百合"
    assert guess.group_type == "season"
    assert guess.episode_number == 9


def test_local_collection_uses_second_directory_as_work_container():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [01][Ma10p_1080p][x265_flac].mkv",
        relative_path="[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [Ma10p_1080p]/[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [01][Ma10p_1080p][x265_flac].mkv",
        source="local",
    )

    assert guess.work_title == "Heya Camp"
    assert guess.series_group == "Yuru Camp"
    assert guess.card_type == "standalone"
    assert guess.belongs_to_series == "Yuru Camp"
    assert guess.relation_type == "spin_off"
    assert guess.group_type == "season"
    assert guess.episode_number == 1


def test_local_collection_plain_season_folder_stays_in_main_series():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp S2 - 01 [Ma10p_1080p][x265_flac].mkv",
        relative_path="[VCB-Studio] Yuru Camp/Season 2/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp S2 - 01 [Ma10p_1080p][x265_flac].mkv",
        source="local",
    )

    assert guess.work_title == "Yuru Camp"
    assert guess.series_group == "Yuru Camp"
    assert guess.card_type == "main_series"
    assert guess.group_type == "season"
    assert guess.season_number == 2
    assert guess.episode_number == 1


def test_local_collection_chinese_season_folder_stays_in_main_series():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="摇曳露营△ 第2季 01.mkv",
        relative_path="摇曳露营△/第2季/摇曳露营△ 第2季 01.mkv",
        source="local",
    )

    assert guess.work_title == "摇曳露营△"
    assert guess.series_group == "摇曳露营△"
    assert guess.card_type == "main_series"
    assert guess.group_type == "season"
    assert guess.season_number == 2
    assert guess.episode_number == 1


def test_season_directory_number_is_not_episode_number():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Mystery Camp][Ma10p_1080p][x265_flac].mkv",
        relative_path="[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Ma10p_1080p]/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Mystery Camp][Ma10p_1080p][x265_flac].mkv",
        source="local",
    )

    assert guess.group_type == "special"
    assert guess.episode_number is None


def test_local_heya_camp_ep00_is_sps():
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Heya Camp EP00][Ma10p_1080p][x265_flac].mkv",
        relative_path="[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Ma10p_1080p]/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Heya Camp EP00][Ma10p_1080p][x265_flac].mkv",
        source="local",
    )

    assert guess.group_type == "special"
