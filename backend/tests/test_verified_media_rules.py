# -*- coding: utf-8 -*-
"""基于 TMDB/官方资料核验过的真实目录回归。"""

from app.recognition.media import recognize_media
from app.recognition.verified_titles import match_verified_tmdb_binding, titles_share_verified_alias


def test_clannad_under_green_tree_is_tv_special_not_movie():
    guess = recognize_media(
        "[MAI] Clannad After Story [24].mkv",
        "动画/CLANNAD.S1-S2+SP+OVA/5.CLANNAD总集篇：在那苍绿的树下.2009/"
        "[MAI] Clannad After Story [24].mkv",
    )

    assert guess.group_type == "special"
    assert guess.media_type == "tv"
    assert guess.card_type == "main_series"
    assert guess.series_group == "CLANNAD"
    assert guess.tmdb_hint_id == 24835


def test_lycoris_friends_short_movies_stay_with_main_series():
    guess = recognize_media(
        "Lycoris.Recoil.S00E10.mkv",
        "动画/莉可丽丝：友谊是时间的窃贼/Season 0/Lycoris.Recoil.S00E10.mkv",
    )

    assert guess.group_type == "special"
    assert guess.series_group == "莉可丽丝"
    assert guess.tmdb_hint_id == 154494


def test_gun_gale_spin_off_uses_its_own_series_group():
    guess = recognize_media(
        "[4K_EA] 刀剑神域外传 Gun Gale Online 第1季 01 [简体内嵌].mkv",
        "动画/刀剑神域.S1-S3+剧场版+外传/外传：Gun Gale Online/"
        "[4K_EA] 刀剑神域外传 Gun Gale Online 第1季 01 [简体内嵌].mkv",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 1
    assert guess.series_group == "外传：Gun Gale Online"
    assert guess.belongs_to_series == "刀剑神域"


def test_audit_aliases_accept_translation_and_full_title_variants():
    assert titles_share_verified_alias("再见，拉拉", "再见菈菈")
    assert titles_share_verified_alias("成长秀～向日葵马戏团～", "Grow Up Show ～向日葵马戏团～")
    assert titles_share_verified_alias("拔作岛", "住在拔作岛上的我应该如何是好？")
    assert not titles_share_verified_alias("路人女主的养成方法", "恋爱游戏世界对路人角色很不友好")


def test_verified_binding_ignores_wrong_folder_year_for_violet_movie():
    binding = match_verified_tmdb_binding(
        "动画/紫罗兰永恒花园/Gekijouban Violet Evergarden (2021)/movie.mkv"
    )

    assert binding is not None
    assert binding.tmdb_id == 533514
    assert binding.tmdb_type == "movie"


def test_verified_bindings_cover_confirmed_clannad_and_evangelion_movies():
    cases = (
        ("动画电影/CLANNAD 剧场版/CLANNAD 剧场版.mkv", 16516),
        ("动画电影/福音战士新剧场版：序/电影.mkv", 15137),
        ("动画电影/福音战士新剧场版：破/电影.mkv", 22843),
    )

    for relative_path, expected_tmdb_id in cases:
        binding = match_verified_tmdb_binding(relative_path)
        assert binding is not None
        assert binding.tmdb_id == expected_tmdb_id
        assert binding.tmdb_type == "movie"

    assert match_verified_tmdb_binding("动画/Milky Subway/Season 1/Milky Subway S01E01.mkv") is None
    assert match_verified_tmdb_binding("动画/刀剑神域 II/Gun Gale Online Arc/S02E01.mkv") is None


def test_verified_bindings_cover_pan115_missing_movie_metadata():
    cases = (
        ("动画/BanG Dream! It's MyGO!!!!! 前篇：春日向阳，迷途野猫/movie.mkv", 1231799),
        ("动画/BanG Dream! It's MyGO!!!!! 后篇：歌唱着，由我们所作的歌 & FILM LIVE/movie.mkv", 1233186),
        ("动画/银河特急 银河☆地铁 各站停车前往剧场/movie.mkv", 1598785),
    )

    for relative_path, expected_tmdb_id in cases:
        binding = match_verified_tmdb_binding(relative_path)
        assert binding is not None
        assert binding.tmdb_id == expected_tmdb_id
        assert binding.tmdb_type == "movie"


def test_air_recap_and_ovas_use_tmdb_special_numbers_instead_of_movie_binding():
    cases = (
        ("[4K_AS] 青空&AIR 总集篇.mkv", 1, "总集篇"),
        ("[4K_AS] 青空&AIR OVA 01.mkv", 2, "山路～mountain path～"),
        ("[4K_AS] 青空&AIR OVA 02.mkv", 3, "天地～universe～"),
    )

    for filename, expected_special, expected_title in cases:
        relative_path = f"动画/AIR.2005/{filename}"
        guess = recognize_media(filename, relative_path)
        assert guess.group_type == "special"
        assert guess.tmdb_hint_id == 26201
        assert guess.special_number == expected_special
        assert guess.title == expected_title

    assert match_verified_tmdb_binding("动画/AIR.2005/[4K_AS] 青空&AIR 总集篇.mkv") is None


def test_verified_special_numbers_follow_tmdb_instead_of_resetting_per_folder():
    cases = (
        ("动画/CLANNAD.S1-S2+SP+OVA/1.CLANNAD.[S01].2007/CLANNAD.S01E23.Episode 23.mkv", 24835, 1),
        ("动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.[S01][OVA].另一个世界：智代篇/[MAI] Clannad [24].mkv", 24835, 2),
        ("动画/CLANNAD.S1-S2+SP+OVA/6.CLANNAD.[S02][OVA].另一个世界：杏篇/[MAI] Clannad After Story [25].mkv", 24835, 5),
        ("动画/中二病也要谈恋爱.S1-S2+剧场版/1.中二病也要谈恋爱.[S1].2012/[MAI] Chuunibyou [13].mkv", 45501, 14),
        ("动画/中二病也要谈恋爱.S1-S2+剧场版/1.中二病也要谈恋爱.[S1].2012/[MAI] Chuunibyou [Lite].mkv", 45501, 35),
        ("动画/中二病也要谈恋爱.S1-S2+剧场版/3.中二病也要谈恋爱.[S2].2014/[MAI] Chuunibyou Ren [SP07].mkv", 45501, 28),
        ("动画/中二病也要谈恋爱.S1-S2+剧场版/4.剧场版：Take On Me.2018/[MAI] Take On Me [SP03].mkv", 45501, 32),
        ("动画/轻音少女.S1-S2+剧场版/轻音少女.[S1].2009/[YE] K-ON! [14].mkv", 42253, 9),
        ("动画/轻音少女.S1-S2+剧场版/轻音少女.[S2].2010/[YE] K-ON!! [27].mkv", 42253, 21),
        ("动画/在下坂本，有何贵干？.2016/[MAI] Sakamoto Desu ga [13].mkv", 65944, 1),
        ("动画/[T.H.X&VCB-Studio] Hyouka/[T.H.X&VCB-Studio] Hyouka [11.5].mkv", 65329, 1),
        ("动画/伪恋.S1-S2+OAD/伪恋.NISEKOI.[S2].2014/S2 [OAD].mkv", 62640, 4),
        ("动画/无职转生.S1-S2/无职转生 [S2].2023/[MAI] Mushoku Tensei II [00].mkv", 94664, 2),
        ("动画/刀剑神域.S1-S3+剧场版+外传/3.刀剑神域[S2].2014/SPs/Sword Art Online II [SP01].mkv", 45782, 13),
        ("动画/刀剑神域.S1-S3+剧场版+外传/5.刀剑神域：Alicization篇.[S3].2018/刀剑神域 第3季 爱丽丝篇 18.5.mkv", 45782, 23),
        ("动画/路人女主的养成方法.S1-S2+剧场版/2.路人女主的养成方法b.[S2].2017/Saenai Heroine Flat [00].mkv", 69367, 2),
        ("动画/某科学的超电磁炮.S1-S3/某科学的超电磁炮S.[S2].2013/某科学的超电磁炮S 第2季 OVA.mkv", 30977, 7),
        ("动画/命运石之门.S1-S2+剧场版+OVA/命运石之门.[S1].2011/Steins;Gate [23β].mkv", 42509, 6),
        ("动画/命运石之门.S1-S2+剧场版+OVA/命运石之门番外：聪明睿智的认知计算.2014/Steins;Gate Cognitive Computing(2).mkv", 42509, 3),
        ("动画/辉夜大小姐想让我告白.S1-S4+剧场版/2.辉夜大小姐想让我告白.[S2].2020/Kaguya [OVA].mkv", 83121, 2),
        ("动画/灵能百分百.S1-S3+OVA/灵能百分百 II.2019/Mob Psycho 100 II [OVA].mkv", 67075, 8),
        ("动画/Re：从零开始的异世界生活.S1-S3/5.Re：从零开始的异世界生活.[S3].2024/SPs/Re Zero 3rd Season [SP16].mkv", 65942, 66),
    )

    for relative_path, expected_tmdb_id, expected_special in cases:
        filename = relative_path.rsplit("/", 1)[-1]
        guess = recognize_media(filename, relative_path)
        assert guess.group_type == "special", relative_path
        assert guess.tmdb_hint_id == expected_tmdb_id, relative_path
        assert guess.special_number == expected_special, relative_path


def test_steins_gate_zero_ova_keeps_its_own_tmdb_series_identity():
    relative_path = (
        "动画/命运石之门.S1-S2+剧场版+OVA/命运石之门0.[S2].2018/"
        "[MAI] Steins;Gate 0 [OVA].mkv"
    )
    guess = recognize_media(relative_path.rsplit("/", 1)[-1], relative_path)

    assert guess.group_type == "special"
    assert guess.series_group == "命运石之门0"
    assert guess.tmdb_hint_id == 78102
    assert guess.special_number == 1


def test_preview_and_non_telop_files_are_auxiliary_not_tmdb_specials():
    cases = (
        "动画/[T.H.X&VCB-Studio] Hyouka/SPs/Hyouka [Preview11.5].mkv",
        "动画/灵能百分百.S1-S3+OVA/灵能百分百 I.2016/灵能百分百 I REIGEN.[OVA]/Mob Psycho 100 REIGEN [Non-telop Epilogue].mkv",
    )

    for relative_path in cases:
        filename = relative_path.rsplit("/", 1)[-1]
        guess = recognize_media(filename, relative_path)
        assert guess.group_type == "auxiliary", relative_path


def test_verified_bindings_distinguish_bocchi_recap_halves():
    upper = match_verified_tmdb_binding(
        "动画/剧场总集篇 孤独摇滚！/剧场总集篇 孤独摇滚！Re- (2024)/movie.mkv"
    )
    lower = match_verified_tmdb_binding(
        "动画/剧场总集篇 孤独摇滚！/剧场总集篇 孤独摇滚！Re-Re- (2024)/movie.mkv"
    )

    assert upper is not None and upper.tmdb_id == 1129610
    assert lower is not None and lower.tmdb_id == 1201387
