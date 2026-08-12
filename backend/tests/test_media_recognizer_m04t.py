# -*- coding: utf-8 -*-
"""M04T 路径上下文特殊内容优先级返修测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_clannad_s01_ova():
    """CLANNAD S01 OVA：路径含 [OVA] → sps，不进 Season 1"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[MAI] Clannad [24][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.[S01][OVA].另一个世界：智代篇/[MAI] Clannad [24][Ma10p_2160p][x265_flac_ass].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"
    assert guess.card_type == "main_series", f"card_type: {guess.card_type}"
    assert guess.episode_number is None, f"episode_number 应为空: {guess.episode_number}"


def test_clannad_s02_sp():
    """CLANNAD S02 SP：路径含 [SP] → sps，不进 Season 2"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[MAI] Clannad After Story [23][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/4.CLANNAD.[S02][SP].一年前的事/[MAI] Clannad After Story [23][Ma10p_2160p][x265_flac_ass].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"
    assert guess.episode_number is None, f"episode_number 应为空: {guess.episode_number}"


def test_clannad_recap():
    """CLANNAD 总集篇已由 TMDB 核实为主系列 S00E04。"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[MAI] Clannad After Story [24][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/5.CLANNAD总集篇：在那苍绿的树下.2009/[MAI] Clannad After Story [24][Ma10p_2160p][x265_flac_ass].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"
    assert guess.card_type == "main_series", f"card_type: {guess.card_type}"
    assert guess.media_type == "tv", f"media_type: {guess.media_type}"
    assert guess.special_number == 4
    assert guess.year == 2009, f"year: {guess.year}"
    # 标题不能只剩 CLANNAD
    assert "苍绿" in guess.title, f"title: {guess.title}"


def test_clannad_s02_ova():
    """CLANNAD S02 OVA：路径含 [OVA] → sps，不进 Season 2"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[MAI] Clannad After Story [25][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/6.CLANNAD.[S02][OVA].另一个世界：杏篇/[MAI] Clannad After Story [25][Ma10p_2160p][x265_flac_ass].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"
    assert guess.episode_number is None, f"episode_number 应为空: {guess.episode_number}"


def test_clannad_s02_normal():
    """CLANNAD Season 2 正片不被误伤"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="CLANNAD.S02E22.小小的手心.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/3.CLANNAD After Story.[S02].2008/CLANNAD.S02E22.小小的手心.mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 2, f"season: {guess.season_number}"
    assert guess.episode_number == 22, f"episode: {guess.episode_number}"


def test_recap_preserves_title():
    """总集篇标题保留刮削线索"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[MAI] Clannad After Story [24].mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/5.CLANNAD总集篇：在那苍绿的树下.2009/[MAI] Clannad After Story [24].mkv",
    )
    assert guess.group_type == "special"
    assert guess.title != "", f"title 不应为空"
    assert guess.original_title != "", f"original_title 不应为空"
    assert guess.year == 2009


def test_op_ed_still_highest():
    """OP/ED 仍然优先级最高"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="NCOP01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.[S01][OVA].另一个世界：智代篇/NCOP01.mkv",
    )
    assert guess.group_type == "ignored", f"OP/ED 应最高优先级: {guess.group_type}"


def test_sps_dir_keyword():
    """目录含 Special → sps"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="ep01.mkv",
        relative_path="动画/test/special/ep01.mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_fanwai_dir():
    """目录含 番外 → sps"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[Test] Demo [01].mkv",
        relative_path="动画/Demo.S1+SP/2.Demo番外篇/[Test] Demo [01].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_tedian_dir():
    """目录含 特典 → sps"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[Test] Demo [01].mkv",
        relative_path="动画/Demo.S1+SP/2.Demo特典/[Test] Demo [01].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_recap_original_title():
    """总集篇 original_title 保留子作品目录"""
    from app.recognition.media import recognize_media
    guess = recognize_media(
        filename="[MAI] Clannad After Story [24].mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/5.CLANNAD总集篇：在那苍绿的树下.2009/[MAI] Clannad After Story [24].mkv",
    )
    assert guess.original_title == "5.CLANNAD总集篇：在那苍绿的树下.2009", f"original_title: {guess.original_title}"
    assert guess.title == "在那苍绿的树下", f"title: {guess.title}"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_clannad_s01_ova, test_clannad_s02_sp, test_clannad_recap,
        test_clannad_s02_ova, test_clannad_s02_normal, test_recap_preserves_title,
        test_op_ed_still_highest, test_sps_dir_keyword,
        test_fanwai_dir, test_tedian_dir, test_recap_original_title,
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
