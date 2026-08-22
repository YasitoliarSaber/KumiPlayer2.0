# -*- coding: utf-8 -*-

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.recognition.media import recognize_media  # noqa: E402


def _plan_item(relative_path: str):
    from app.import_plan.models import ImportPlanItem

    return ImportPlanItem(
        id=relative_path,
        plan_id="demon-plan",
        raw_file_id=relative_path,
        source="pan115",
        relative_path=relative_path,
        real_path=f"H:\\115open\\{relative_path.replace('/', chr(92))}",
        resource_type="video",
        action="generate_strm",
        import_family="anime",
    )


def test_verified_demon_slayer_arcs_use_tmdb_seasons_and_season_episode_numbers():
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    paths = (
        "动画/鬼灭之刃系列/1.立志篇.[S1].2019/[MAI] Kimetsu no Yaiba [01].mkv",
        "动画/鬼灭之刃系列/2.无限列车篇.[S1.1].2020/TV版/[MAI] Kimetsu no Yaiba [27].mkv",
        "动画/鬼灭之刃系列/3.游郭篇(花街篇).[S2].2021/[MAI] Kimetsu No Yaiba [34].mkv",
        "动画/鬼灭之刃系列/4.锻刀村篇.[S3].2023/[MAI] Kimetsu no Yaiba [01].mkv",
        "动画/鬼灭之刃系列/5.柱训练篇.[S4].2024/[MAI] Kimetsu no Yaiba [01].mkv",
    )
    plan = ImportPlan(
        plan_id="demon-plan",
        source="pan115",
        import_family="anime",
        status="draft",
        items=[_plan_item(path) for path in paths],
    )

    recognize_import_plan_media(plan)

    assert [(item.season_number, item.episode_number) for item in plan.items] == [
        (1, 1),
        (2, 1),
        (3, 1),
        (4, 1),
        (5, 1),
    ]
    assert all(item.tmdb_hint_id == 85937 for item in plan.items)


def test_release_metadata_after_episode_marker_is_not_episode_title():
    cases = [
        ("Make.Heroine.ga.Oosugiru.S01E01.2024.1080p.BluRay.Remux.AVC.FLAC.mkv", "动画/败犬女主太多了！ (2024)/Season 1/"),
        ("Lycoris Recoil S01E01-[1080p][JP.BD.Remux].mkv", "动画/莉可丽丝 (2022)/Season 1/"),
        ("Serial.Experiments.Lain.S01E01.1998.JPN.1080p.BluRay.x264.FLAC.2.0-ZeroTV.mkv", "动画/玲音.Serial.Experiments.Lain.1998/"),
        ("See.You.Tomorrow.at.the.Food.Court.S01E01.2025.1080p.BluRay.Remux.AVC.FLAC.2.0.mkv", "动画/明天，美食广场见。 (2025)/Season 1/"),
    ]

    for filename, parent in cases:
        guess = recognize_media(filename, parent + filename, source="pan115")
        assert guess.group_type == "season"
        assert guess.title == ""


def test_numbered_series_subwork_infers_fourth_season():
    filename = "[YE] Kaguya-sama wa Kokurasetai First Kiss wa Owaranai [01][1080P].mkv"
    guess = recognize_media(
        filename,
        "动画/辉夜大小姐想让我告白.S1-S4+剧场版/"
        "4.辉夜大小姐想让我告白：初吻不会结束.2022/" + filename,
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 4
    assert guess.episode_number == 1


def test_known_anime_movie_without_movie_keyword_is_standalone_movie():
    filename = "[FM Zombie Sub]Zombie Land Saga Yumeginga Paradise[BDRip][1080p][HEVC_Opus][CHS-JPN][Softsub].mkv"
    guess = recognize_media(
        filename,
        f"动画/佐贺偶像是传奇 梦幻银河乐园/{filename}",
        source="pan115",
    )

    assert guess.group_type == "movie"
    assert guess.media_type == "movie"
    assert guess.card_type == "standalone"
    assert guess.work_title == "佐贺偶像是传奇 梦幻银河乐园"


def test_bare_episode_with_terminal_end_is_regular_episode():
    filename = "[SAIO-Raws] Hellsing Ultimate 10 END [BD 1920x1080 HEVC-10bit OPUS ASSx2].mkv"
    guess = recognize_media(
        filename,
        f"动画/Hellsing Ultimate/{filename}",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.media_type == "tv"
    assert guess.season_number == 1
    assert guess.episode_number == 10
    assert guess.card_type == "main_series"


def test_fullwidth_bracket_episode_uses_parent_season_dir():
    filename = "【Top o Nerae! GunBuster】【01】【BDrip】【HEVC 2880x2160p FLAC】.mkv"
    guess = recognize_media(
        filename,
        f"动画/飞跃巅峰 内封中字/S1/{filename}",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.media_type == "tv"
    assert guess.season_number == 1
    assert guess.episode_number == 1
    assert guess.needs_review is False


def test_fullwidth_bracket_episode_keeps_second_season_context():
    filename = "【Top o Nerae2! DieBuster】【01】【BDrip】【HEVC 3840x2160p FLAC】.mkv"
    guess = recognize_media(
        filename,
        f"动画/飞跃巅峰 内封中字/S2/{filename}",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.media_type == "tv"
    assert guess.season_number == 2
    assert guess.episode_number == 1
    assert guess.needs_review is False


def test_fullwidth_bracket_episode_with_end_suffix_is_regular_episode():
    filename = "【Top o Nerae! GunBuster】【06 END】【BDrip】【HEVC 2880x2160p FLAC】.mkv"
    guess = recognize_media(
        filename,
        f"动画/飞跃巅峰 内封中字/S1/{filename}",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.media_type == "tv"
    assert guess.season_number == 1
    assert guess.episode_number == 6
    assert guess.needs_review is False


def test_plain_season_dir_overrides_wrong_sxx_marker():
    filename = "奇巧出租车 - S02E01 - 奇怪的司机.mkv"
    guess = recognize_media(
        filename,
        f"刮削好的动画/奇巧计程车/Season 1/{filename}",
        source="baidu",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 1
    assert guess.episode_number == 1
    assert any("覆盖文件名中的 S02" in reason for reason in guess.reasons)


def test_subwork_season_context_still_beats_generic_season_folder():
    filename = "Yuru Camp Season 2 S02E01.mkv"
    guess = recognize_media(
        filename,
        f"[VCB-Studio] Yuru Camp/Yuru Camp Season 2/Season 1/{filename}",
        source="local",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 2
    assert guess.episode_number == 1


def test_known_special_subtitle_is_grouped_under_main_series():
    filename = "辉夜大小姐想让我告白 通向大人的阶梯.S01E01.藤原千花想吓人.mkv"
    guess = recognize_media(
        filename,
        f"刮削好的动画/辉夜大小姐想让我告白 登上大人的阶梯/{filename}",
        source="baidu",
    )

    assert guess.group_type == "special"
    assert guess.season_number == 0
    assert guess.work_title == "辉夜大小姐想让我告白"
    assert guess.series_group == "辉夜大小姐想让我告白"


def test_movie_folder_sp_file_is_true_special_not_movie():
    filename = "[MAI] Chuunibyou demo Koi ga Shitai!  -Take On Me- [SP02][Ma10p_2160p][x265_flac_ass].mkv"
    guess = recognize_media(
        filename,
        f"动画/中二病也要谈恋爱.S1-S2+剧场版/4.剧场版：Take On Me.2018/{filename}",
        source="pan115",
    )

    assert guess.group_type == "special"
    assert guess.media_type == "tv"
    assert guess.season_number == 0
    assert guess.special_number == 31
    assert guess.needs_review is False


def test_verified_tv_special_keeps_series_identity_inside_movie_folder():
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media
    from app.scrape.target_builder import build_scrape_targets

    folder = "动画/中二病也要谈恋爱.S1-S2+剧场版/4.剧场版：Take On Me.2018"
    movie = _plan_item(f"{folder}/[MAI] Chuunibyou demo Koi ga Shitai! -Take On Me- [Movie].mkv")
    special = _plan_item(
        f"{folder}/[MAI] Chuunibyou demo Koi ga Shitai! -Take On Me- [SP02].mkv"
    )
    plan = ImportPlan(
        plan_id="take-on-me-plan",
        source="pan115",
        source_snapshot_id="take-on-me-snapshot",
        import_family="anime",
        status="draft",
        items=[movie, special],
    )

    recognize_import_plan_media(plan)

    assert special.group_type == "special"
    assert special.special_number == 31
    assert special.media_type == "tv"
    assert special.show_type == "anime_series"
    assert special.series_group == "中二病也要谈恋爱"
    assert special.tmdb_hint_id == 45501

    special_target = next(target for target in build_scrape_targets(plan) if target.group_type == "special")
    assert special_target.tmdb_hint_id == 45501
    assert special_target.tmdb_hint_type == "tv"


def test_generic_movie_folder_uses_release_title_instead_of_generic_folder_name():
    """“剧场版”只是分类目录，不能成为独立卡片标题。"""
    cases = [
        (
            "[MAI] Kimetsu no Yaiba Mugen Ressha-hen [Ma10p_2160p][x265_flac_ass].mkv",
            "动画/鬼灭之刃系列/2.无限列车篇.[S1.1].2020/剧场版/",
            "Kimetsu no Yaiba Mugen Ressha-hen",
        ),
        (
            "[MAI] Gekijouban Violet Evergarden [Ma10p_2160p][x265_flac_ass].mkv",
            "动画/紫罗兰永恒花园.TV版+外传+剧场版/3.剧场版.2021/",
            "Gekijouban Violet Evergarden",
        ),
    ]

    for filename, parent, expected_title in cases:
        guess = recognize_media(filename, parent + filename, source="pan115")

        assert guess.group_type == "movie"
        assert guess.card_type == "standalone"
        assert guess.title == expected_title
        assert guess.work_title == expected_title
        assert guess.title != "剧场版"


def test_gunbuster_collection_uses_verified_main_series_tmdb_binding():
    """《飞跃巅峰》主系列不能被 Bangumi 模糊候选绑定成科学讲座。"""
    filename = "【Top o Nerae2! DieBuster】【01】【BDrip】.mkv"
    guess = recognize_media(
        filename,
        f"动画/飞跃巅峰 内封中字/S2/{filename}",
        source="pan115",
    )

    assert guess.tmdb_hint_id == 66931
    assert guess.tmdb_hint_type == "tv"


def test_chuunibyou_lite_release_is_special():
    filename = "[MAI] Chuunibyou demo Koi ga Shitai! [Lite][Ma10p_2160p][x265_flac_ass].mkv"
    guess = recognize_media(
        filename,
        f"动画/中二病也要谈恋爱.S1-S2+剧场版/1.中二病也要谈恋爱.[S1].2012/{filename}",
        source="pan115",
    )

    assert guess.group_type == "special"
    assert guess.media_type == "tv"
    assert guess.season_number == 0
    assert guess.needs_review is False


def test_bracketed_op_original_is_ignored():
    filename = "[MAI] Chuunibyou demo Koi ga Shitai! [OP Original][Ma10p_2160p][x265_flac].mkv"
    guess = recognize_media(
        filename,
        f"动画/中二病也要谈恋爱.S1-S2+剧场版/1.中二病也要谈恋爱.[S1].2012/{filename}",
        source="pan115",
    )

    assert guess.group_type == "ignored"
    assert guess.needs_review is False


def test_mv_collection_file_is_auxiliary():
    filename = "[MAI] Spy x Family [MV Breeze ~(K)NoW_NAME~][Ma10p_2160p][x265_flac].mkv"
    guess = recognize_media(
        filename,
        f"动画/间谍过家家.S1-S2+剧场版/MV合集/{filename}",
        source="pan115",
    )

    assert guess.group_type == "auxiliary"
    assert guess.media_type == "tv"
    assert guess.needs_review is False


def test_side_story_release_without_episode_is_standalone_movie():
    filename = "[MAI] Violet Evergarden Side Story Gaiden Eien to Jidou Shuki Ningyou [Ma10p_1608p][x265_TureHD5.1_ass].mkv"
    guess = recognize_media(
        filename,
        f"动画/紫罗兰永恒花园.TV版+外传+剧场版/2.外传：永远与自动手记人偶.2020/{filename}",
        source="pan115",
    )

    assert guess.group_type == "movie"
    assert guess.media_type == "movie"
    assert guess.card_type == "standalone"
    assert guess.needs_review is False


def test_series_container_metadata_is_removed_from_work_title():
    """Catalog labels must not become part of the title sent to metadata providers."""
    violet = recognize_media(
        "[MAI] Violet Evergarden - 01 [Ma10p_2160p][x265_flac_ass].mkv",
        "动画/紫罗兰永恒花园.TV版+外传+剧场版/"
        "1.紫罗兰永恒花园.2018/[MAI] Violet Evergarden - 01 [Ma10p_2160p][x265_flac_ass].mkv",
        source="pan115",
    )
    gunbuster = recognize_media(
        "【Top o Nerae! GunBuster】【01】【BDrip】【HEVC 2880x2160p FLAC】.mkv",
        "动画/飞跃巅峰 内封中字/S1/【Top o Nerae! GunBuster】【01】【BDrip】【HEVC 2880x2160p FLAC】.mkv",
        source="pan115",
    )

    assert violet.work_title == "紫罗兰永恒花园"
    assert violet.series_group == "紫罗兰永恒花园"
    assert gunbuster.work_title == "飞跃巅峰"


def test_existing_work_title_with_year_still_recognizes_single_file_movie():
    """单文件电影目录树：existing_work_title 携带年份，不得因丢年份误判 needs_review。

    回归背景：DiscoveryEngine._process_boundary 的证据步只传 boundary 相对路径
    （文件名）+ existing_work_title（作品目录名）。目录名里的年份是电影兜底识别的
    唯一证据，旧实现直接赋值 work_title 不解析年份，导致「东京教父.Tokyo.Godfathers.2003」
    这类单文件电影全部落 needs_review。
    """
    cases = [
        (
            "[Noxia-AI] 东京教父.2003.2160p.BDRip.AV1.10-bit.DTS-HD5.1.PGS.mkv",
            "东京教父.Tokyo.Godfathers.2003",
        ),
        (
            "Porco.Rosso.1992.2160p.WEB-DL.H265.DDP5.1.2Audio-DreamHD.mkv",
            "红猪.Porco.Rosso.1992",
        ),
        (
            "BanG Dream! It's MyGO!!!!! 前篇：春日向阳，迷途野猫 (2024) -1080p.mkv",
            "BanG Dream! It's MyGO!!!!! 前篇：春日向阳，迷途野猫 (2024)",
        ),
    ]
    for filename, boundary_name in cases:
        guess = recognize_media(
            filename,
            filename,
            source="pan115",
            existing_work_title=boundary_name,
        )
        assert guess.group_type == "movie", filename
        assert guess.media_type == "movie", filename
        assert guess.needs_review is False, filename
        assert guess.year is not None, filename
