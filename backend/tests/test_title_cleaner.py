# -*- coding: utf-8 -*-
"""M04R 作品名清洗规则测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# A 类：不误伤测试
# ============================================================

def test_a_air():
    """AIR -> AIR"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("AIR")
    assert r.title == "AIR", f"实际: {r.title}"
    assert not r.changed


def test_a_relife():
    """ReLIFE -> ReLIFE"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("ReLIFE")
    assert r.title == "ReLIFE"


def test_a_paprika():
    """红辣椒.Paprika -> 红辣椒.Paprika"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("红辣椒.Paprika")
    assert r.title == "红辣椒.Paprika", f"实际: {r.title}"


def test_a_lain():
    """玲音.Serial.Experiments.Lain -> 玲音.Serial.Experiments.Lain"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("玲音.Serial.Experiments.Lain")
    assert r.title == "玲音.Serial.Experiments.Lain", f"实际: {r.title}"


def test_a_millennium():
    """千年女优.Millennium.Actress -> 千年女优.Millennium.Actress"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("千年女优.Millennium.Actress")
    assert r.title == "千年女优.Millennium.Actress", f"实际: {r.title}"


def test_a_porco():
    """红猪.Porco.Rosso -> 红猪.Porco.Rosso"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("红猪.Porco.Rosso")
    assert r.title == "红猪.Porco.Rosso", f"实际: {r.title}"


def test_a_b86():
    """B 86-不存在的战区 -> 86-不存在的战区"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("B 86-不存在的战区")
    assert r.title == "86-不存在的战区", f"实际: {r.title}"


def test_a_kimi():
    """败犬女主太多了！ -> 败犬女主太多了！"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("败犬女主太多了！")
    assert r.title == "败犬女主太多了！", f"实际: {r.title}"


def test_a_status_word():
    """败犬女主太多了！.2024（将更新） -> 败犬女主太多了！"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("败犬女主太多了！.2024（将更新）")
    assert r.title == "败犬女主太多了！", f"实际: {r.title}"
    assert r.changed


# ============================================================
# B 类：系列容器测试
# ============================================================

def test_b_clannad():
    """CLANNAD.S1-S2+SP+OVA -> CLANNAD"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("CLANNAD.S1-S2+SP+OVA")
    assert r.title == "CLANNAD", f"实际: {r.title}"
    assert r.changed


def test_b_sao():
    """刀剑神域.S1-S3+剧场版+外传 -> 刀剑神域"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("刀剑神域.S1-S3+剧场版+外传")
    assert r.title == "刀剑神域", f"实际: {r.title}"


def test_b_vinland():
    """冰海战记.S1-S2 -> 冰海战记"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("冰海战记.S1-S2")
    assert r.title == "冰海战记", f"实际: {r.title}"


def test_b_steins_gate():
    """命运石之门.S1-S2+剧场版+OVA -> 命运石之门"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("命运石之门.S1-S2+剧场版+OVA")
    assert r.title == "命运石之门", f"实际: {r.title}"


def test_b_oshinoko():
    """我推的孩子.S1-S3 -> 我推的孩子"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("我推的孩子.S1-S3")
    assert r.title == "我推的孩子", f"实际: {r.title}"


def test_b_my_dressup():
    """更衣人偶坠入爱河.S1-S2 -> 更衣人偶坠入爱河"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("更衣人偶坠入爱河.S1-S2")
    assert r.title == "更衣人偶坠入爱河", f"实际: {r.title}"


def test_b_railgun():
    """某科学的超电磁炮.S1-S3 -> 某科学的超电磁炮"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("某科学的超电磁炮.S1-S3")
    assert r.title == "某科学的超电磁炮", f"实际: {r.title}"


def test_b_saekano():
    """路人女主的养成方法.S1-S2+剧场版 -> 路人女主的养成方法"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("路人女主的养成方法.S1-S2+剧场版")
    assert r.title == "路人女主的养成方法", f"实际: {r.title}"


def test_b_kon():
    """轻音少女.S1-S2+剧场版 -> 轻音少女"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("轻音少女.S1-S2+剧场版")
    assert r.title == "轻音少女", f"实际: {r.title}"


def test_b_index():
    """魔法禁书目录.S1-S3+剧场版 -> 魔法禁书目录"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("魔法禁书目录.S1-S3+剧场版")
    assert r.title == "魔法禁书目录", f"实际: {r.title}"


def test_b_status_word():
    """我推的孩子.S1-S3（更新中） -> 我推的孩子"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("我推的孩子.S1-S3（更新中）")
    assert r.title == "我推的孩子", f"实际: {r.title}"


# ============================================================
# C 类：字幕组目录测试
# ============================================================

def test_c_lpraws():
    """[LP-Raws] One Room -> One Room"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[LP-Raws] One Room")
    assert r.title == "One Room", f"实际: {r.title}"


def test_c_hyouka():
    """[T.H.X&VCB-Studio] Hyouka [Ma10p_1080p] -> Hyouka"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[T.H.X&VCB-Studio] Hyouka [Ma10p_1080p]")
    assert r.title == "Hyouka", f"实际: {r.title}"


def test_c_jjk():
    """[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4] -> Jujutsu_Kaisen"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]")
    assert r.title == "Jujutsu_Kaisen", f"实际: {r.title}"


def test_c_silent_witch():
    """[Haruhana] Silent Witch - ... -> Silent Witch - Chinmoku no Majo no Kakushigoto"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Haruhana] Silent Witch - Chinmoku no Majo no Kakushigoto [BDRip][HEVC-10bit FLAC][CHI_JPN]")
    assert r.title == "Silent Witch - Chinmoku no Majo no Kakushigoto", f"实际: {r.title}"


def test_c_youkoso():
    """[DMG&VCB-Studio] Youkoso ... -> Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[DMG&VCB-Studio] Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e [Ma10p_1080p]")
    assert r.title == "Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e", f"实际: {r.title}"


def test_c_kaguya():
    """[LoliHouse] Kaguya-sama ... -> Kaguya-sama wa Kokurasetai - Otona e no Kaidan"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[LoliHouse] Kaguya-sama wa Kokurasetai - Otona e no Kaidan [WebRip 1080p HEVC-10bit AAC]")
    assert r.title == "Kaguya-sama wa Kokurasetai - Otona e no Kaidan", f"实际: {r.title}"


def test_c_dandadan():
    """[Nekomoe kissaten&LoliHouse] Dandadan ... -> Dandadan"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Nekomoe kissaten&LoliHouse] Dandadan [13-24][WebRip 1080p HEVC-10bit AAC]")
    assert r.title == "Dandadan", f"实际: {r.title}"


def test_c_watashi():
    """[Sakurato][202509] Watashi o Tabetai ... -> Watashi o Tabetai, Hitodenashi"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Sakurato][202509] Watashi o Tabetai, Hitodenashi [01-13 Fin][1080P][简繁内封]")
    assert r.title == "Watashi o Tabetai, Hitodenashi", f"实际: {r.title}"


def test_c_seihantai():
    """[SweetSub] Seihantai na Kimi to Boku ... -> Seihantai na Kimi to Boku"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[SweetSub] Seihantai na Kimi to Boku [01-12][WebRip][1080P][HEVC 10bit][CHS&CHT]")
    assert r.title == "Seihantai na Kimi to Boku", f"实际: {r.title}"


# ============================================================
# D 类：字幕组 token 精确匹配反例（旧子串逻辑误杀）
# ============================================================

def test_d_mai_hime_not_fansub():
    """[Mai-HiME][VCB-Studio] -> Mai-HiME

    旧的 'kw in lower' 子串逻辑会把 'Mai-HiME' 当作字幕组
    （'mai' 命中），与 'VCB-Studio' 一起被过滤，再退化选最长
    得到 'VCB-Studio'，污染作品标题。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Mai-HiME][VCB-Studio]")
    assert r.title == "Mai-HiME", f"实际: {r.title}"


def test_d_sweet_home_bracket_only():
    """[Sweet Home] -> Sweet Home

    'mai'/'sweet' 是字幕组短关键词，但不能误命中 'Sweet Home'。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Sweet Home]")
    assert r.title == "Sweet Home", f"实际: {r.title}"


def test_d_sweet_home_with_tech_bracket():
    """[Sweet Home][Ma10p_1080p] -> Sweet Home

    'Sweet Home' 与技术标签混排时，'Sweet Home' 应作为标题保留，
    'ma10p_1080p' 作为技术标签过滤。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Sweet Home][Ma10p_1080p]")
    assert r.title == "Sweet Home", f"实际: {r.title}"


def test_d_combined_fansub_filtered():
    """[Nekomoe kissaten&LoliHouse][Some Anime] -> Some Anime

    & 连接的复合字幕组名仍应被识别并过滤：按 '&' 拆分后
    'lolihouse' 精确命中关键词，整体 token 视为字幕组。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Nekomoe kissaten&LoliHouse][Some Anime]")
    assert r.title == "Some Anime", f"实际: {r.title}"


def test_d_multiword_fansub_filtered():
    """[Nekomoe kissaten][Some Anime] -> Some Anime

    多词字幕组的完整名称应精确命中；不能依赖后面恰好还有另一个
    由 &/+ 连接的已知字幕组组件。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Nekomoe kissaten][Some Anime]")
    assert r.title == "Some Anime", f"实际: {r.title}"


def test_d_beansub_fzsd_filtered():
    """[BeanSub&FZSD][Anime Title] -> Anime Title

    回归 & 拆分：'BeanSub' 和 'FZSD' 都精确命中关键词。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[BeanSub&FZSD][Anime Title]")
    assert r.title == "Anime Title", f"实际: {r.title}"


def test_d_real_fansub_mai_still_filtered():
    """[MAI][Some Anime Title] -> Some Anime Title

    'MAI' 是真实字幕组，应仍被精确命中并过滤（不退化）。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[MAI][Some Anime Title]")
    assert r.title == "Some Anime Title", f"实际: {r.title}"


def test_d_real_fansub_sweetsub_still_filtered():
    """[SweetSub][Anime Title] -> Anime Title

    'SweetSub' 是真实字幕组（整体精确命中），应仍被过滤。
    'Sweet' 短关键词单独不能再命中 'Sweet Home'（见 test_d_sweet_home_*）。
    """
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[SweetSub][Anime Title]")
    assert r.title == "Anime Title", f"实际: {r.title}"


# ============================================================
# 边界测试
# ============================================================

def test_empty():
    """空容器名"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("")
    assert r.needs_review


def test_only_brackets():
    """全是方括号，无剩余文本"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("[Group][BDRip][1080P]")
    # 应该从中选一个，或标记 needs_review
    assert r.title != ""


def test_no_change():
    """简单作品名不被改变"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("冰菓")
    assert r.title == "冰菓"
    assert not r.changed


def test_single_letter_numeric_prefix_removed():
    """B 86-不存在的战区 -> 86-不存在的战区"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("B 86-不存在的战区")
    assert r.title == "86-不存在的战区"
    assert r.changed
    assert any("单字母分区前缀" in rule for rule in r.applied_rules)


def test_single_letter_real_title_not_removed():
    """A Channel 这种真实标题不应被误伤"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("A Channel")
    assert r.title == "A Channel"


def test_tmdb_hint_removed_from_title():
    """Re: 从零开始的异世界生活 (2016) {tmdb-65942} -> Re: 从零开始的异世界生活"""
    from app.recognition.title_cleaner import clean_work_title_container
    r = clean_work_title_container("Re: 从零开始的异世界生活 (2016) {tmdb-65942}")
    assert r.title == "Re: 从零开始的异世界生活"
    assert any("TMDB ID" in rule for rule in r.applied_rules)


# ============================================================
# 集成测试：recognize_media 使用清洗后的 work_title
# ============================================================

def test_integration_series_container():
    """recognize_media 对系列容器返回清洗后的 work_title"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD.S01E01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/CLANNAD.S01E01.mkv",
    )
    assert guess.work_title == "CLANNAD", f"实际: {guess.work_title}"


def test_integration_fansub():
    """recognize_media 对字幕组目录返回清洗后的 work_title"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="One Room.S01E01.mkv",
        relative_path="动画/[LP-Raws] One Room/One Room.S01E01.mkv",
    )
    assert guess.work_title == "One Room", f"实际: {guess.work_title}"


def test_original_title_preserved():
    """original_title 保存清洗前的容器名"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD.S01E01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/CLANNAD.S01E01.mkv",
    )
    assert guess.original_title == "CLANNAD.S1-S2+SP+OVA", f"实际: {guess.original_title}"


def test_clean_warnings_passed():
    """清洗 warning 传递到 MediaGuess"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Jujutsu_Kaisen.S01E01.mkv",
        relative_path="动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]/Jujutsu_Kaisen.S01E01.mkv",
    )
    # 字幕组 token 选择可能有 warning
    assert guess.work_title == "Jujutsu_Kaisen", f"实际: {guess.work_title}"


def test_subwork_dir_extracted():
    """子作品目录作为刮削线索进入 reasons"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD.After.Story.S01E01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.After.Story.2008/CLANNAD.After.Story.S01E01.mkv",
    )
    # 子作品目录应出现在 reasons 中
    assert any("子作品目录" in r for r in guess.reasons), f"reasons: {guess.reasons}"
    assert any("CLANNAD.After.Story" in r for r in guess.reasons), f"reasons: {guess.reasons}"


def test_subwork_year_extracted():
    """子作品目录的年份应作为 year 候选"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD.After.Story.S01E01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.After.Story.2008/CLANNAD.After.Story.S01E01.mkv",
    )
    assert guess.year == 2008, f"year 应为 2008，实际: {guess.year}"
    assert any("年份来自子作品目录" in r for r in guess.reasons), f"reasons: {guess.reasons}"


def test_fansub_not_series_group():
    """字幕组目录不应被误判为系列容器"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Jujutsu_Kaisen.S01E01.mkv",
        relative_path="动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]/Jujutsu_Kaisen.S01E01.mkv",
    )
    assert guess.series_group == "Jujutsu_Kaisen", f"series_group: {guess.series_group}"
    assert "BeanSub" not in guess.series_group, f"series_group 含字幕组标签: {guess.series_group}"
    assert "BDRip" not in guess.series_group, f"series_group 含技术标签: {guess.series_group}"
    assert "01-47" not in guess.series_group, f"series_group 含集数范围: {guess.series_group}"
    assert "BeanSub" not in (guess.belongs_to_series or ""), f"belongs_to_series 含字幕组标签"


def test_bracket_movie_file_uses_concrete_title():
    """同一根目录混放 TV + MOVIE 时，电影卡应取文件名里的具体标题。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[BeanSub&FZSD][Jujutsu_Kaisen_0][MOVIE][BDRip][CHS][1080P][AVC_AAC](FA6841CF).mp4",
        relative_path=(
            "动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]/"
            "[BeanSub&FZSD][Jujutsu_Kaisen_0][MOVIE][BDRip][CHS][1080P][AVC_AAC](FA6841CF).mp4"
        ),
        source="pan115",
    )

    assert guess.group_type == "movie"
    assert guess.media_type == "movie"
    assert guess.card_type == "standalone"
    assert guess.work_title == "Jujutsu Kaisen 0"
    assert guess.title == "Jujutsu Kaisen 0"
    assert guess.series_group == "Jujutsu_Kaisen"


def test_subwork_year_in_work_id():
    """子作品年份应影响 work_id 生成（work_id 用有效年份算）"""
    from app.recognition.media import recognize_media

    # 有子作品年份
    guess_with = recognize_media(
        filename="CLANNAD.After.Story.S01E01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.After.Story.2008/CLANNAD.After.Story.S01E01.mkv",
    )
    # 无子作品年份
    guess_without = recognize_media(
        filename="CLANNAD.S01E01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/CLANNAD.S01E01.mkv",
    )
    # work_id 应不同（因为年份不同）
    assert guess_with.work_id != guess_without.work_id, "work_id 应因年份不同而不同"
    assert guess_with.year == 2008
    assert guess_without.year is None


def test_subwork_year_no_false_warning():
    """有子作品年份时不应出现警告，年份来源写入 reasons"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD.After.Story.S01E01.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.After.Story.2008/CLANNAD.After.Story.S01E01.mkv",
    )
    assert guess.year == 2008
    assert not any("未识别到年份" in w for w in guess.warnings), f"不应有'未识别到年份'警告: {guess.warnings}"
    assert any("年份来自子作品目录" in r for r in guess.reasons), f"应说明年份来源: {guess.reasons}"


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        # A 类
        test_a_air, test_a_relife, test_a_paprika, test_a_lain,
        test_a_millennium, test_a_porco, test_a_b86, test_a_kimi, test_a_status_word,
        # B 类
        test_b_clannad, test_b_sao, test_b_vinland, test_b_steins_gate,
        test_b_oshinoko, test_b_my_dressup, test_b_railgun, test_b_saekano,
        test_b_kon, test_b_index, test_b_status_word,
        # C 类
        test_c_lpraws, test_c_hyouka, test_c_jjk, test_c_silent_witch,
        test_c_youkoso, test_c_kaguya, test_c_dandadan, test_c_watashi, test_c_seihantai,
        # D 类：字幕组 token 精确匹配反例
        test_d_mai_hime_not_fansub, test_d_sweet_home_bracket_only,
        test_d_sweet_home_with_tech_bracket, test_d_combined_fansub_filtered,
        test_d_multiword_fansub_filtered, test_d_beansub_fzsd_filtered,
        test_d_real_fansub_mai_still_filtered,
        test_d_real_fansub_sweetsub_still_filtered,
        # 边界
        test_empty, test_only_brackets, test_no_change,
        # 集成
        test_integration_series_container, test_integration_fansub,
        test_original_title_preserved, test_clean_warnings_passed, test_subwork_dir_extracted,
        test_subwork_year_extracted, test_fansub_not_series_group,
        test_subwork_year_in_work_id, test_subwork_year_no_false_warning,
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
