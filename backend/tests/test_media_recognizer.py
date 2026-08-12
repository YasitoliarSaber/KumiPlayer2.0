# -*- coding: utf-8 -*-
"""M04 媒体识别基础规则 单元测试

覆盖 M03 设计说明要求的全部测试项。
优先级：OP/ED > 独立卡片 > SPs > Season。
"""

import sys
from pathlib import Path

# 确保可以导入 app 模块
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# 辅助函数
# ============================================================

def _make_video_item(
    filename: str,
    relative_path: str = "",
    source: str = "pan115",
):
    """构造视频 ImportPlanItem 用于测试"""
    from app.import_plan.models import ImportPlanItem

    if not relative_path:
        relative_path = f"动画/test/{filename}"

    return ImportPlanItem(
        id=f"id-{filename}",
        plan_id="plan-1",
        raw_file_id=f"raw-{filename}",
        source=source,
        relative_path=relative_path,
        real_path=rf"H:\115open\{relative_path.replace('/', chr(92))}",
        resource_type="video",
        action="generate_strm",
        confidence="high",
    )


def _make_plan_from_items(items):
    """构造 ImportPlan 用于测试"""
    from app.import_plan.models import ImportPlan

    return ImportPlan(
        plan_id="plan-test-1",
        source="pan115",
        source_snapshot_id="snap-test-1",
        status="draft",
        items=items,
    )


def test_explicit_second_season_absolute_numbers_are_normalized():
    """明确 S2 目录中的 26-37 应映射成该季 1-12。"""
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        _make_video_item(
            f"[MAI] Spy x Family [{episode}][1080p].mkv",
            "动画/间谍过家家.S1-S2+剧场版/3.Spy x Family.[S2].2023/"
            f"[MAI] Spy x Family [{episode}][1080p].mkv",
        )
        for episode in range(26, 38)
    ]
    plan = _make_plan_from_items(items)

    recognize_import_plan_media(plan)

    assert [item.season_number for item in plan.items] == [2] * 12
    assert [item.episode_number for item in plan.items] == list(range(1, 13))


def test_single_absolute_episode_is_not_normalized_without_group_evidence():
    """只有单个文件时不能贸然改集号。"""
    from app.recognition.plan_recognizer import recognize_import_plan_media

    item = _make_video_item(
        "[MAI] Spy x Family [26][1080p].mkv",
        "动画/间谍过家家.S1-S2+剧场版/3.Spy x Family.[S2].2023/"
        "[MAI] Spy x Family [26][1080p].mkv",
    )
    plan = _make_plan_from_items([item])

    recognize_import_plan_media(plan)

    assert item.season_number == 2
    assert item.episode_number == 26


def test_implicit_numbered_season_moves_to_special_when_explicit_season_conflicts():
    """编号子作品与明确 S04 撞季时，明确季保留，旧四集篇章进入特别篇。"""
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = []
    for episode in range(1, 5):
        items.append(_make_video_item(
            f"First Kiss [{episode:02d}][1080p].mkv",
            "动画/辉夜大小姐想让我告白.S1-S4+剧场版/"
            f"4.辉夜大小姐想让我告白：初吻不会结束.2022/First Kiss [{episode:02d}][1080p].mkv",
        ))
    for episode in range(1, 3):
        items.append(_make_video_item(
            f"Kaguya.2026.S04E{episode:02d}.1080p.mkv",
            "动画/辉夜大小姐想让我告白.S1-S4+剧场版/"
            f"5.辉夜大小姐想让我告白：通往大人的阶梯.[S4].2026/Kaguya.2026.S04E{episode:02d}.1080p.mkv",
        ))
    plan = _make_plan_from_items(items)

    recognize_import_plan_media(plan)

    first_kiss = plan.items[:4]
    new_season = plan.items[4:]
    assert [(item.group_type, item.season_number, item.special_number) for item in first_kiss] == [
        ("special", 0, episode) for episode in range(1, 5)
    ]
    assert [(item.group_type, item.season_number, item.episode_number) for item in new_season] == [
        ("season", 4, 1), ("season", 4, 2)
    ]


def test_implicit_season_collision_does_not_cross_distinct_work_roots():
    """同名但不同作品根的显式季度不能改写另一张卡的季度结构。"""
    from app.recognition.plan_recognizer import _move_implicit_season_collision_to_specials

    implicit = _make_video_item(
        "Show [01].mkv",
        "动画/作品副本 A/篇章四/Show [01].mkv",
    )
    explicit = _make_video_item(
        "Show.S04E01.mkv",
        "动画/作品副本 B/Season 4/Show.S04E01.mkv",
    )
    for item in (implicit, explicit):
        item.work_id = "same-recognition-id"
        item.work_title = "同名作品"
        item.series_group = "同名作品"
        item.card_type = "main_series"
        item.group_type = "season"
        item.season_number = 4
        item.episode_number = 1
        item.action = "generate_strm"
    plan = _make_plan_from_items([implicit, explicit])

    _move_implicit_season_collision_to_specials(plan)

    assert implicit.group_type == "season"
    assert implicit.season_number == 4
    assert implicit.episode_number == 1


def test_special_numbering_is_scoped_to_each_work_root():
    """不同作品根即使旧 work_id 相同，特别篇也各自从 1 编号。"""
    from app.recognition.plan_recognizer import _normalize_special_titles

    items = [
        _make_video_item("SP.mkv", "动画/作品副本 A/SP.mkv"),
        _make_video_item("SP.mkv", "动画/作品副本 B/SP.mkv"),
    ]
    for item in items:
        item.work_id = "same-recognition-id"
        item.work_title = "同名作品"
        item.card_type = "main_series"
        item.group_type = "special"
        item.season_number = 0
        item.episode_number = None
        item.special_number = None
        item.action = "generate_strm"
    plan = _make_plan_from_items(items)

    _normalize_special_titles(plan)

    assert [item.special_number for item in items] == [1, 1]


def test_duplicate_explicit_special_numbers_are_made_unique_within_work_root():
    """多季度各自携带 SP01 时，合卡前必须得到稳定且唯一的特别篇编号。"""
    from app.recognition.plan_recognizer import _normalize_special_titles

    items = [
        _make_video_item("Season1 SP01.mkv", "动画/同一作品/Season 1/SP01.mkv"),
        _make_video_item("Season2 SP01.mkv", "动画/同一作品/Season 2/SP01.mkv"),
        _make_video_item("Season1 SP02.mkv", "动画/同一作品/Season 1/SP02.mkv"),
    ]
    for item, number in zip(items, (1, 1, 2), strict=True):
        item.work_id = "same-work"
        item.work_title = "同一作品"
        item.card_type = "main_series"
        item.group_type = "special"
        item.season_number = 0
        item.episode_number = None
        item.special_number = number
        item.action = "generate_strm"
    plan = _make_plan_from_items(items)

    _normalize_special_titles(plan)

    assert sorted(item.special_number for item in items) == [1, 2, 3]


# ============================================================
# 作品名和年份识别测试
# ============================================================

def test_work_title_and_year():
    """作品年份：AIR.2005 → work_title=AIR, year=2005"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="AIR.S01E01.微风～breeze～.mkv",
        relative_path="动画/AIR.2005/AIR.S01E01.微风～breeze～.mkv",
    )
    assert guess.work_title == "AIR", f"work_title: {guess.work_title}"
    assert guess.year == 2005, f"year: {guess.year}"


def test_work_title_chinese():
    """中文作品名：冰菓.2012 → work_title=冰菓, year=2012"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="冰菓.S01E01.重生.mkv",
        relative_path="动画/冰菓.2012/冰菓.S01E01.重生.mkv",
    )
    assert guess.work_title == "冰菓", f"work_title: {guess.work_title}"
    assert guess.year == 2012, f"year: {guess.year}"


def test_work_title_mixed():
    """中英文混合：红辣椒.Paprika.2006"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="红辣椒.Paprika.S01E01.mkv",
        relative_path="动画/红辣椒.Paprika.2006/红辣椒.Paprika.S01E01.mkv",
    )
    assert guess.work_title == "红辣椒.Paprika", f"work_title: {guess.work_title}"
    assert guess.year == 2006, f"year: {guess.year}"


def test_work_title_with_status_word():
    """带状态词：败犬女主太多了！.2024（将更新）"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="败犬女主.S01E01.mkv",
        relative_path="动画/败犬女主太多了！.2024（将更新）/败犬女主.S01E01.mkv",
    )
    assert guess.work_title == "败犬女主太多了！", f"work_title: {guess.work_title}"
    assert guess.year == 2024, f"year: {guess.year}"


# ============================================================
# 正片季集识别测试
# ============================================================

def test_standard_sxxexx():
    """标准 SxxExx：AIR.S01E01.微风.mkv → season=1, episode=1"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="AIR.S01E01.微风～breeze～.mkv",
        relative_path="动画/AIR.2005/AIR.S01E01.微风～breeze～.mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 1, f"season_number: {guess.season_number}"
    assert guess.episode_number == 1, f"episode_number: {guess.episode_number}"
    assert guess.card_type == "main_series", f"card_type: {guess.card_type}"


def test_standard_s02e05():
    """S02E05 → season=2, episode=5"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD.S02E05.mkv",
        relative_path="动画/CLANNAD.2007/CLANNAD.S02E05.mkv",
    )
    assert guess.season_number == 2
    assert guess.episode_number == 5


def test_standard_sxxexx_preserves_local_episode_title():
    """S02E18 后面带本地标题时，应保留给分集 NFO 兜底使用。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD.After.Story.S02E18.大地的尽头.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/3.CLANNAD After Story.[S02].2008/CLANNAD.After.Story.S02E18.大地的尽头.mkv",
        source="pan115",
    )

    assert guess.season_number == 2
    assert guess.episode_number == 18
    assert guess.title == "大地的尽头"


def test_parent_s02_makes_bare_episode_high_confidence_second_season():
    """父目录明确 [S02] 时，裸 02 应按第2季第2集处理。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="CLANNAD After Story 02.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/3.CLANNAD After Story.[S02].2008/CLANNAD After Story 02.mkv",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 2
    assert guess.episode_number == 2
    assert guess.confidence == "high"


def test_three_digit_episode_can_infer_season_without_parent_marker():
    """无父目录季号时，203 这类标准三位格式也应拆成第2季第3集。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Some Anime 203.mkv",
        relative_path="动画/Some Anime.2024/Some Anime 203.mkv",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 2
    assert guess.episode_number == 3


def test_three_digit_episode_102_means_s01e02_without_parent_marker():
    """102 这种三位格式不是第102集，而是 S01E02。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Some Anime 102.mkv",
        relative_path="动画/Some Anime.2024/Some Anime 102.mkv",
        source="pan115",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 1
    assert guess.episode_number == 2


def test_chinese_season_episode():
    """中文季集：第1季 01 → season=1, episode=1"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[4K_EA] 刀剑神域 第1季 01 [简体内嵌].mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/[4K_EA] 刀剑神域 第1季 01 [简体内嵌].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 1, f"season_number: {guess.season_number}"
    assert guess.episode_number == 1, f"episode_number: {guess.episode_number}"


def test_bracket_episode():
    """方括号集数：[01] → episode=1，season 可推断"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[MAI] EIGHTY SIX [01][Ma10p_2160p].mkv",
        relative_path="动画/EIGHTY SIX.2021/[MAI] EIGHTY SIX [01][Ma10p_2160p].mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.episode_number == 1, f"episode_number: {guess.episode_number}"


# ============================================================
# 半集 / SPs 识别测试
# ============================================================

def test_half_episode_bracket():
    """半集：[11.5] → group_type=sps"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[MAI] EIGHTY SIX [11.5][Ma10p_2160p].mkv",
        relative_path="动画/EIGHTY SIX.2021/[MAI] EIGHTY SIX [11.5][Ma10p_2160p].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_half_episode_chinese():
    """中文半集：第2季 14.5 → sps"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[4K_EA] 刀剑神域 第2季 14.5 [简体内嵌].mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/[4K_EA] 刀剑神域 第2季 14.5 [简体内嵌].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_sps_special_dir():
    """SP 目录：special/11.5.mkv → sps"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Ygm]Hyouka [11.5].mkv",
        relative_path="动画/冰菓.2012/special/[Ygm]Hyouka [11.5].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_sps_ova():
    """OVA 关键词 → sps"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[4K_AS] 青空&AIR OVA 01.mkv",
        relative_path="动画/AIR.2005/[4K_AS] 青空&AIR OVA 01.mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_zero_episode():
    """第0集 → sps"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[4K_EA] 刀剑神域 第4季 异界战争 00 [简体内嵌].mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/[4K_EA] 刀剑神域 第4季 异界战争 00 [简体内嵌].mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


# ============================================================
# OP/ED 识别测试
# ============================================================

def test_op_ed_ncop():
    """OPED 文件：NCOP01.mkv → op_ed"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[MAI] EIGHTY SIX [NCOP01][Ma10p_2160p].mkv",
        relative_path="动画/EIGHTY SIX.2021/[MAI] EIGHTY SIX [NCOP01][Ma10p_2160p].mkv",
    )
    assert guess.group_type == "ignored", f"group_type: {guess.group_type}"
    assert guess.card_type == "main_series", f"card_type: {guess.card_type}"


def test_op_ed_directory():
    """OPED 目录：OP＆ED/NCOP01.mkv → op_ed"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="NCOP01.mkv",
        relative_path="动画/AIR.2005/OP＆ED/NCOP01.mkv",
    )
    assert guess.group_type == "ignored", f"group_type: {guess.group_type}"


def test_op_ed_priority_over_season():
    """OP/ED 优先于 Season：NCOP01 即使包含 01 也必须是 op_ed"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="NCOP01.mkv",
        relative_path="动画/test/NCOP01.mkv",
    )
    assert guess.group_type == "ignored", f"NCOP01 应为 op_ed，实际: {guess.group_type}"
    assert guess.episode_number is None, f"OP/ED 不应有 episode_number"


def test_op_ed_priority_over_sps():
    """OP/ED 优先于 SPs：NCED01 不应被 SP 规则捕获"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="NCED01.mkv",
        relative_path="动画/test/NCED01.mkv",
    )
    assert guess.group_type == "ignored", f"NCED01 应为 op_ed，实际: {guess.group_type}"


def test_no_false_positive_ed_in_redline():
    """Redline.mkv 不应被 ED 子串误判为 op_ed"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Redline.mkv",
        relative_path="动画/Redline.2009/Redline.mkv",
    )
    assert guess.group_type != "op_ed", f"Redline 不应被识别为 op_ed，实际: {guess.group_type}"


def test_no_false_positive_sp_in_spy_family():
    """SPY x FAMILY.S01E01.mkv 不应被 SP 子串误判为 sps"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="SPY x FAMILY.S01E01.mkv",
        relative_path="动画/SPY x FAMILY.2022/SPY x FAMILY.S01E01.mkv",
    )
    assert guess.group_type == "season", f"SPY x FAMILY 应为 season，实际: {guess.group_type}"
    assert guess.season_number == 1
    assert guess.episode_number == 1


def test_no_false_positive_ova_in_casanova():
    """Casanova.S01E01.mkv 不应被 OVA 子串误判为 sps。

    'Casanova' 包含子串 'ova'，旧的裸 OVA 正则会误判特别篇。
    """
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Casanova.S01E01.mkv",
        relative_path="动画/Casanova.2005/Casanova.S01E01.mkv",
    )
    assert guess.group_type == "season", f"Casanova 应为 season，实际: {guess.group_type}"
    assert guess.season_number == 1
    assert guess.episode_number == 1


def test_no_false_positive_oad_in_roadshow():
    """Roadshow.S01E01.mkv 不应被 OAD 子串误判为 sps。

    'Roadshow' 包含子串 'oad'，旧的裸 OAD 正则会误判特别篇。
    """
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Roadshow.S01E01.mkv",
        relative_path="动画/Roadshow.2018/Roadshow.S01E01.mkv",
    )
    assert guess.group_type == "season", f"Roadshow 应为 season，实际: {guess.group_type}"
    assert guess.season_number == 1
    assert guess.episode_number == 1


def test_no_false_positive_special_in_specialized():
    """Specialized.S01E01.mkv 不应被 SPECIAL 子串误判为 sps。

    'Specialized' 以 'Special' 开头，旧的裸 SPECIAL 正则会误判特别篇。
    """
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Specialized.S01E01.mkv",
        relative_path="动画/Specialized.2020/Specialized.S01E01.mkv",
    )
    assert guess.group_type == "season", f"Specialized 应为 season，实际: {guess.group_type}"
    assert guess.season_number == 1
    assert guess.episode_number == 1


def test_no_false_positive_ova_in_casanova_movie_file():
    """'Casanova' 出现在文件名而非目录时同样不应被误判。

    覆盖独立卡片优先于 SPs 的链路：Casanova 作为单电影文件名，
    不应落入 SPs。
    """
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Casanova.2005.mkv",
        relative_path="动画/Casanova.2005/Casanova.2005.mkv",
    )
    # 单文件电影：不应因 'ova' 子串被截胡为 Special
    assert guess.group_type != "special", f"Casanova 不应是 special，实际: {guess.group_type}"


def test_sps_ova_no_space_still_detected():
    """'OVA01' 无空格写法仍应识别为 sps。

    字母边界（而非 \\b）允许 OVA 后跟数字，保留对 'OVA01' 的检测。
    """
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Group] Sample Show OVA01 [1080p].mkv",
        relative_path="动画/Sample Show.2024/[Group] Sample Show OVA01 [1080p].mkv",
    )
    assert guess.group_type == "special", f"OVA01 应为 special，实际: {guess.group_type}"


def test_sps_oad_keyword():
    """OAD 关键词 → sps。锁定 OAD 边界修复后正例不退化。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Group] Sample Show OAD 01 [1080p].mkv",
        relative_path="动画/Sample Show.2024/[Group] Sample Show OAD 01 [1080p].mkv",
    )
    assert guess.group_type == "special", f"OAD 应为 special，实际: {guess.group_type}"


def test_sps_special_keyword_still_detected():
    """'Special 01' 写法仍应识别为 sps。锁定 SPECIAL 边界修复后正例不退化。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Group] Sample Show Special 01 [1080p].mkv",
        relative_path="动画/Sample Show.2024/[Group] Sample Show Special 01 [1080p].mkv",
    )
    assert guess.group_type == "special", f"Special 应为 special，实际: {guess.group_type}"


def test_sps_specials_plural_still_detected():
    """'Specials' 复数写法仍应识别为 sps。

    SPECIALS? 保留可选 's' 后缀以匹配复数形式，
    但 'Specialized'（后跟 'i'）不匹配。
    """
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Group] Sample Show Specials 01 [1080p].mkv",
        relative_path="动画/Sample Show.2024/[Group] Sample Show Specials 01 [1080p].mkv",
    )
    assert guess.group_type == "special", f"Specials 应为 special，实际: {guess.group_type}"


def test_non_credit_ed():
    """Non-Credit ED.mkv 应识别为 op_ed"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Non-Credit ED.mkv",
        relative_path="动画/test/Non-Credit ED.mkv",
    )
    assert guess.group_type == "ignored", f"Non-Credit ED 应为 op_ed，实际: {guess.group_type}"


def test_no_subtitle_ed():
    """无字幕 ED.mkv 应识别为 op_ed"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="无字幕 ED.mkv",
        relative_path="动画/test/无字幕 ED.mkv",
    )
    assert guess.group_type == "ignored", f"无字幕 ED 应为 op_ed，实际: {guess.group_type}"


def test_op_with_chinese_title_quotes_is_ignored():
    """OP「标题」这类文件应识别为 OP/ED，不应进入镜像或刮削。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="OP「芽吹くとき」-1080P 高清-AVC.mp4",
        relative_path="上伊那牡丹醉姿如百合/OP「芽吹くとき」-1080P 高清-AVC.mp4",
        source="local",
    )

    assert guess.group_type == "ignored"
    assert guess.episode_number is None


# ============================================================
# 独立卡片识别测试
# ============================================================

def test_standalone_movie():
    """剧场版：剧场版：序列之争.2017 → standalone movie"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="剧场版：序列之争.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/4.剧场版：序列之争.2017/剧场版：序列之争.mkv",
    )
    assert guess.card_type == "standalone", f"card_type: {guess.card_type}"
    assert guess.media_type == "movie", f"media_type: {guess.media_type}"
    assert guess.relation_type == "movie", f"relation_type: {guess.relation_type}"


def test_local_collection_movie_release_title_is_cleaned():
    """本地合集电影目录应保留干净电影标题，而不是字幕组/压制目录名。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p][x265_flac].mkv",
        relative_path=(
            "[VCB-Studio] Yuru Camp/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p][x265_flac].mkv"
        ),
        source="local",
    )

    assert guess.card_type == "standalone"
    assert guess.group_type == "movie"
    assert guess.title == "Yuru Camp Movie"
    assert guess.work_title == "Yuru Camp Movie"


def test_standalone_recap():
    """总集篇 → standalone recap"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="总集篇.mkv",
        relative_path="动画/CLANNAD.S1-S2+SP+OVA/5.CLANNAD总集篇.2009/总集篇.mkv",
    )
    assert guess.card_type == "standalone", f"card_type: {guess.card_type}"
    assert guess.relation_type == "recap", f"relation_type: {guess.relation_type}"


def test_standalone_spin_off():
    """外传 → standalone spin_off"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="外传.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/外传：Gun Gale Online/外传.mkv",
    )
    assert guess.card_type == "standalone", f"card_type: {guess.card_type}"
    assert guess.relation_type == "spin_off", f"relation_type: {guess.relation_type}"
    assert guess.media_type == "tv", f"media_type: {guess.media_type}"


def test_year_named_movie_file_not_attached_episode():
    """声之形.2016/声之形.2016.mkv 应识别为电影，不应把 2016 当第 16 集"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="声之形.2016.mkv",
        relative_path="动画/声之形.2016/声之形.2016.mkv",
    )

    assert guess.group_type == "movie", f"group_type: {guess.group_type}"
    assert guess.media_type == "movie", f"media_type: {guess.media_type}"
    assert guess.card_type == "standalone", f"card_type: {guess.card_type}"
    assert guess.work_title == "声之形"
    assert guess.year == 2016


def test_movie_sps_folder_inherits_parent_movie_card():
    """电影目录下带明确特典标识的视频保留为 special，不生成剧集卡片。"""
    from app.recognition.plan_recognizer import recognize_import_plan_media

    main = _make_video_item(
        "声之形.2016.mkv",
        "动画/声之形.2016/声之形.2016.mkv",
    )
    special = _make_video_item(
        "映像特典 01.mkv",
        "动画/声之形.2016/SPs/映像特典 01.mkv",
    )
    plan = _make_plan_from_items([main, special])

    recognize_import_plan_media(plan)

    assert main.group_type == "movie"
    assert main.show_type == "anime_movie"
    assert special.action == "generate_strm"
    assert special.group_type == "special"
    assert special.media_type == "movie"
    assert special.show_type == "anime_movie"
    assert special.work_id == main.work_id
    assert special.work_title == "声之形"


def test_series_container_movie_markers_do_not_turn_episodes_into_movie_specials():
    """系列容器里的 +SP/+剧场版 只是范围说明，不能污染下层正片。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    item = _make_video_item(
        "[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/1.刀剑神域.[S1].2012/[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
    )
    plan = ImportPlan(
        plan_id="plan-sao",
        source="pan115",
        source_snapshot_id="snap-sao",
        import_family="anime",
        status="draft",
        items=[item],
    )

    recognize_import_plan_media(plan)

    assert item.action == "generate_strm"
    assert item.group_type == "season"
    assert item.media_type == "tv"
    assert item.show_type == "anime_series"
    assert item.card_type == "main_series"
    assert item.season_number == 1
    assert item.episode_number == 1


def test_special_like_directories_are_ignored_without_dropping_sibling_main_episodes():
    """SPs/Menu/CD/Specials 这类目录整层跳过，同级正片保留。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        _make_video_item(
            "[VCB-Studio] Sword Art Online [Sword Art Offline 01][Ma10p_1080p][x265_flac].mkv",
            relative_path="动画/刀剑神域.S1-S3+剧场版+外传/1.刀剑神域.[S1].2012/SPs/[VCB-Studio] Sword Art Online [Sword Art Offline 01][Ma10p_1080p][x265_flac].mkv",
        ),
        _make_video_item(
            "[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
            relative_path="动画/刀剑神域.S1-S3+剧场版+外传/1.刀剑神域.[S1].2012/[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        ),
    ]
    plan = ImportPlan(
        plan_id="plan-sao-sps",
        source="pan115",
        source_snapshot_id="snap-sao-sps",
        import_family="anime",
        status="draft",
        items=items,
    )

    recognize_import_plan_media(plan)

    assert items[0].action == "ignore"
    assert items[0].group_type == "auxiliary"
    assert items[1].action == "generate_strm"
    assert items[1].group_type == "season"
    assert items[1].episode_number == 1


def test_series_container_movie_subwork_does_not_fold_sibling_season_into_movie():
    """同一合集里的电影子作品不能把其他季度正片折叠成电影。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    season = _make_video_item(
        "[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/1.刀剑神域.[S1].2012/[4K_EA] 刀剑神域 第1季 01 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
    )
    movie = _make_video_item(
        "[4K_EA] 刀剑神域 剧场版(2013) Extra Edition [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/2.刀剑神域 Extra Edition 总集篇.2013/[4K_EA] 刀剑神域 剧场版(2013) Extra Edition [简体内嵌]【Bilibili_AYWDXNH】.mkv",
    )
    plan = ImportPlan(
        plan_id="plan-sao-mixed",
        source="pan115",
        source_snapshot_id="snap-sao-mixed",
        import_family="anime",
        status="draft",
        items=[season, movie],
    )

    recognize_import_plan_media(plan)

    assert season.group_type == "season"
    assert season.media_type == "tv"
    assert season.show_type == "anime_series"
    assert movie.group_type == "movie"
    assert movie.media_type == "movie"
    assert movie.show_type == "anime_movie"


def test_movie_can_be_at_category_root_work_root_or_series_subdir():
    """电影可能在分类根、作品根，也可能在系列合集的电影子目录。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        _make_video_item(
            "声之形.2016.mkv",
            relative_path="动画电影/声之形.2016.mkv",
        ),
        _make_video_item(
            "声之形.2016.mkv",
            relative_path="动画/声之形.2016/声之形.2016.mkv",
        ),
        _make_video_item(
            "声之形.2016.mkv",
            relative_path="声之形.2016/声之形.2016.mkv",
        ),
        _make_video_item(
            "[VCB-Studio] Sword Art Online -Ordinal Scale- [Ma10p_1080p][x265_flac].mkv",
            relative_path="动画/刀剑神域.S1-S3+剧场版+外传/4.剧场版：序列之争.2017/[VCB-Studio] Sword Art Online -Ordinal Scale- [Ma10p_1080p][x265_flac].mkv",
        ),
    ]
    plan = ImportPlan(
        plan_id="plan-movie-shapes",
        source="pan115",
        source_snapshot_id="snap-movie-shapes",
        import_family="anime",
        status="draft",
        items=items,
    )

    recognize_import_plan_media(plan)

    assert all(item.action == "generate_strm" for item in items)
    assert all(item.group_type == "movie" for item in items)
    assert all(item.media_type == "movie" for item in items)
    assert all(item.show_type == "anime_movie" for item in items)
    assert all(item.card_type == "standalone" for item in items)


def test_import_plan_keeps_explicit_specials_and_plain_main_episodes():
    """导入执行保留正片/电影/明确 special；PV/Menu 等附属视频跳过。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        _make_video_item(
            f"[Group] Sample Show - {episode:02d} [1080p].mkv",
            relative_path=f"动画/Sample Show.2024/[Group] Sample Show - {episode:02d} [1080p].mkv",
        )
        for episode in range(1, 9)
    ]
    items.extend([
        _make_video_item(
            "[Group] Sample Show - 11.5 [1080p].mkv",
            relative_path="动画/Sample Show.2024/[Group] Sample Show - 11.5 [1080p].mkv",
        ),
        _make_video_item(
            "[Group] Sample Show OVA 01 [1080p].mkv",
            relative_path="动画/Sample Show.2024/[Group] Sample Show OVA 01 [1080p].mkv",
        ),
        _make_video_item(
            "[Group] Sample Show [PV01][1080p].mkv",
            relative_path="动画/Sample Show.2024/[Group] Sample Show [PV01][1080p].mkv",
        ),
        _make_video_item(
            "[Group] Sample Show Menu01 [1080p].mkv",
            relative_path="动画/Sample Show.2024/Menu/[Group] Sample Show Menu01 [1080p].mkv",
        ),
    ])
    plan = ImportPlan(
        plan_id="plan-main-only",
        source="pan115",
        source_snapshot_id="snap-main-only",
        import_family="anime",
        status="draft",
        items=items,
    )

    recognize_import_plan_media(plan)

    main_items = items[:8]
    special_items = items[8:10]
    skipped_items = items[10:]
    assert all(item.action == "generate_strm" for item in main_items)
    assert [item.episode_number for item in main_items] == list(range(1, 9))
    assert all(item.group_type == "season" for item in main_items)
    assert all(item.show_type == "anime_series" for item in main_items)
    assert all(item.action == "generate_strm" for item in special_items)
    assert all(item.group_type == "special" for item in special_items)
    assert all(item.action == "ignore" for item in skipped_items)
    assert all(item.group_type == "auxiliary" for item in skipped_items)


def test_air_recap_in_same_work_folder_does_not_fold_season_episodes_into_specials():
    """同作品目录里有总集篇时，SxxExx 正片仍必须保留为 season。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    season = _make_video_item(
        "AIR.S01E01.微风～breeze～.mkv",
        relative_path="动画/AIR.2005/AIR.S01E01.微风～breeze～.mkv",
    )
    movie = _make_video_item(
        "[4K_AS] 青空&AIR 总集篇.mkv",
        relative_path="动画/AIR.2005/[4K_AS] 青空&AIR 总集篇.mkv",
    )
    ova = _make_video_item(
        "[4K_AS] 青空&AIR OVA 01.mkv",
        relative_path="动画/AIR.2005/[4K_AS] 青空&AIR OVA 01.mkv",
    )
    plan = ImportPlan(
        plan_id="plan-air-mixed",
        source="pan115",
        source_snapshot_id="snap-air-mixed",
        import_family="anime",
        status="draft",
        items=[season, movie, ova],
    )

    recognize_import_plan_media(plan)

    assert season.action == "generate_strm"
    assert season.group_type == "season"
    assert season.media_type == "tv"
    assert season.show_type == "anime_series"
    assert season.season_number == 1
    assert season.episode_number == 1
    assert movie.group_type == "special"
    assert movie.special_number == 1
    assert ova.group_type == "special"
    assert ova.media_type == "tv"
    assert ova.show_type == "anime_series"
    assert ova.work_id == season.work_id


def test_release_folder_with_movie_word_does_not_fold_numbered_episodes_into_specials():
    """发布目录含 MOVIE 时，[01] 到 [47] 仍按正片识别，电影条目单独成 movie。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    episode = _make_video_item(
        "[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01][CHS][1080P][AVC_AAC](6CAAF467).mp4",
        relative_path=(
            "动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]/"
            "[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01][CHS][1080P][AVC_AAC](6CAAF467).mp4"
        ),
    )
    movie = _make_video_item(
        "[BeanSub&FZSD][Jujutsu_Kaisen_0][BDRip][CHS][1080P][AVC_AAC](F4F1E97B).mp4",
        relative_path=(
            "动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]/"
            "[BeanSub&FZSD][Jujutsu_Kaisen_0][BDRip][CHS][1080P][AVC_AAC](F4F1E97B).mp4"
        ),
    )
    plan = ImportPlan(
        plan_id="plan-jjk-mixed",
        source="pan115",
        source_snapshot_id="snap-jjk-mixed",
        import_family="anime",
        status="draft",
        items=[episode, movie],
    )

    recognize_import_plan_media(plan)

    assert episode.action == "generate_strm"
    assert episode.group_type == "season"
    assert episode.media_type == "tv"
    assert episode.show_type == "anime_series"
    assert episode.episode_number == 1
    assert movie.group_type == "movie"


def test_multiple_movie_files_in_same_folder_remain_movies_not_specials():
    """同一电影目录下多个 movie 文件不能把非首个版本改成 special。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    first = _make_video_item(
        "[VCB-Studio] Sword Art Online -Ordinal Scale- [Ma10p_1080p][x265_flac].mkv",
        relative_path=(
            "动画/刀剑神域.S1-S3+剧场版+外传/4.剧场版：序列之争.2017/"
            "[VCB-Studio] 剧场版 刀剑神域 序列之争  10-bit 1080p HEVC BDRip/"
            "[VCB-Studio] Sword Art Online -Ordinal Scale- [Ma10p_1080p][x265_flac].mkv"
        ),
    )
    second = _make_video_item(
        "[4K_EA] 刀剑神域 剧场版(2017) 序列之争 [简体内嵌]【Bilibili_AYWDXNH】.mkv",
        relative_path=(
            "动画/刀剑神域.S1-S3+剧场版+外传/4.剧场版：序列之争.2017/"
            "[4K_EA] 刀剑神域 剧场版(2017) 序列之争 [简体内嵌]【Bilibili_AYWDXNH】.mkv"
        ),
    )
    plan = ImportPlan(
        plan_id="plan-multi-movie",
        source="pan115",
        source_snapshot_id="snap-multi-movie",
        import_family="anime",
        status="draft",
        items=[first, second],
    )

    recognize_import_plan_media(plan)

    assert first.group_type == "movie"
    assert second.group_type == "movie"
    assert first.media_type == "movie"
    assert second.media_type == "movie"


def test_special_title_preserves_original_stem_except_leading_release_group():
    """特别篇展示名保留原文件主体，只移除最前面的字幕组/压制组括号。"""
    from app.import_plan.models import ImportPlan
    from app.recognition.plan_recognizer import recognize_import_plan_media

    special = _make_video_item(
        "[MAI] EIGHTY SIX [11.5][Ma10p_2160p][x265_flac_ass].mkv",
        relative_path="动画/B 86-不存在的战区.2021/special/[MAI] EIGHTY SIX [11.5][Ma10p_2160p][x265_flac_ass].mkv",
    )
    plan = ImportPlan(
        plan_id="plan-special-title",
        source="pan115",
        source_snapshot_id="snap-special-title",
        import_family="anime",
        status="draft",
        items=[special],
    )

    recognize_import_plan_media(plan)

    assert special.group_type == "special"
    assert special.title == "EIGHTY SIX [11.5][Ma10p_2160p][x265_flac_ass]"



# ============================================================
# 非视频不识别测试
# ============================================================

def test_non_video_not_recognized():
    """非视频 item 不被媒体识别（work_title 等保持空）"""
    from app.import_plan.models import ImportPlanItem
    from app.recognition.plan_recognizer import recognize_import_plan_media

    video_item = _make_video_item("视频.mkv", "动画/test/视频.mkv")
    sub_item = ImportPlanItem(
        id="sub-1",
        plan_id="plan-1",
        raw_file_id="raw-sub",
        source="pan115",
        relative_path="动画/test/字幕.ass",
        resource_type="subtitle",
        action="attach_only",
    )
    nfo_item = ImportPlanItem(
        id="nfo-1",
        plan_id="plan-1",
        raw_file_id="raw-nfo",
        source="pan115",
        relative_path="动画/test/信息.nfo",
        resource_type="nfo",
        action="ignore",
    )

    plan = _make_plan_from_items([video_item, sub_item, nfo_item])
    recognize_import_plan_media(plan)

    # 字幕 item 不应被识别
    assert sub_item.work_title == "", f"字幕不应有 work_title: {sub_item.work_title}"
    assert sub_item.season_number is None, f"字幕不应有 season_number"
    assert sub_item.group_type == "", f"字幕不应有 group_type"

    # nfo item 不应被识别
    assert nfo_item.work_title == "", f"nfo 不应有 work_title"
    assert nfo_item.group_type == "", f"nfo 不应有 group_type"


# ============================================================
# 不填 target 测试
# ============================================================

def test_no_target_fields():
    """任意视频不填 target_dir / target_filename / target_strm_path"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="AIR.S01E01.微风.mkv",
        relative_path="动画/AIR.2005/AIR.S01E01.微风.mkv",
    )
    # MediaGuess 没有 target 字段，所以天然满足
    # 通过 plan_recognizer 应用后也不应填
    from app.recognition.plan_recognizer import recognize_import_plan_media

    item = _make_video_item("AIR.S01E01.微风.mkv", "动画/AIR.2005/AIR.S01E01.微风.mkv")
    plan = _make_plan_from_items([item])
    recognize_import_plan_media(plan)

    assert item.target_dir == "", f"不应填 target_dir: {item.target_dir}"
    assert item.target_filename == "", f"不应填 target_filename: {item.target_filename}"
    assert item.target_strm_path == "", f"不应填 target_strm_path: {item.target_strm_path}"


# ============================================================
# 系列容器测试
# ============================================================

def test_series_container():
    """系列容器：刀剑神域.S1-S3+剧场版+外传 → series_group=刀剑神域"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="1.刀剑神域.[S1].2012.S01E01.mkv",
        relative_path="动画/刀剑神域.S1-S3+剧场版+外传/1.刀剑神域.[S1].2012/1.刀剑神域.[S1].2012.S01E01.mkv",
    )
    assert guess.series_group == "刀剑神域", f"series_group: {guess.series_group}"
    assert guess.belongs_to_series == "刀剑神域", f"belongs_to_series: {guess.belongs_to_series}"


def test_local_collection_uses_root_as_series_group():
    """本地合集目录：不同季度应归到第一层合集系列名。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [01][Ma10p_1080p][x265_flac].mkv",
        relative_path=(
            "[VCB-Studio] Yuru Camp/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Ma10p_1080p]/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [01][Ma10p_1080p][x265_flac].mkv"
        ),
        source="local",
    )
    assert guess.work_title == "Yuru Camp Season 2"
    assert guess.series_group == "Yuru Camp"
    assert guess.card_type == "main_series"
    assert guess.group_type == "season"
    assert guess.season_number == 2
    assert guess.episode_number == 1


# ============================================================
# 置信度测试
# ============================================================

def test_confidence_high():
    """高置信度：明确 SxxExx + 作品名 + 年份"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="AIR.S01E01.微风.mkv",
        relative_path="动画/AIR.2005/AIR.S01E01.微风.mkv",
    )
    assert guess.confidence == "high", f"confidence: {guess.confidence}"


def test_confidence_low_no_group():
    """低置信度：无法识别分组"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="random_video.mkv",
        relative_path="动画/AIR.2005/random_video.mkv",
    )
    assert guess.confidence == "low", f"confidence: {guess.confidence}"
    assert guess.needs_review is True, f"needs_review: {guess.needs_review}"


# ============================================================
# 集成测试：plan_recognizer
# ============================================================

def test_plan_recognizer_integration():
    """集成测试：plan_recognizer 处理多种类型的 item"""
    from app.recognition.plan_recognizer import recognize_import_plan_media

    items = [
        _make_video_item("AIR.S01E01.微风.mkv", "动画/AIR.2005/AIR.S01E01.微风.mkv"),
        _make_video_item("NCOP01.mkv", "动画/AIR.2005/OP＆ED/NCOP01.mkv"),
        _make_video_item("剧场版.mkv", "动画/刀剑神域.S1-S3+剧场版+外传/4.剧场版.2017/剧场版.mkv"),
    ]
    plan = _make_plan_from_items(items)
    recognize_import_plan_media(plan)

    # 正片
    assert items[0].group_type == "season"
    assert items[0].season_number == 1
    assert items[0].episode_number == 1

    # OP/ED
    assert items[1].group_type == "ignored"

    # 剧场版
    assert items[2].card_type == "standalone"
    assert items[2].media_type == "movie"


def test_tmdb_hint_extracted_and_title_cleaned():
    """目录里的 {tmdb-65942} 应作为结构化 hint 保留，不污染标题。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Re Zero S02E01.mkv",
        relative_path="动画/Re: 从零开始的异世界生活 (2016) {tmdb-65942}/Season 2/Re Zero S02E01.mkv",
        source="baidu",
    )
    assert guess.work_title == "Re: 从零开始的异世界生活"
    assert guess.original_title == "Re: 从零开始的异世界生活 (2016)"
    assert guess.tmdb_hint_id == 65942
    assert guess.tmdb_hint_type == "tv"


def test_baidu_direct_work_folder_is_not_category_skipped():
    """百度树允许第一层直接就是作品目录，不能把 Season 1 当作品名。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="古诺希亚.2025.S01E12.2160p.WebRip.HEVC.AAC-NoxiaAI.mkv",
        relative_path="古诺希亚 (2025) {tmdbid-278604} [4K]/Season 1/古诺希亚.2025.S01E12.2160p.WebRip.HEVC.AAC-NoxiaAI.mkv",
        source="baidu",
    )
    assert guess.work_title == "古诺希亚"
    assert guess.original_title == "古诺希亚 (2025) [4K]"
    assert guess.year == 2025
    assert guess.tmdb_hint_id == 278604
    assert guess.group_type == "season"
    assert guess.season_number == 1
    assert guess.episode_number == 12


def test_baidu_category_work_folder_still_skips_known_category():
    """百度树有明确分类层时，作品仍取第二层。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="钢之炼金术师 FA - S01E03 - 邪教之街.mkv",
        relative_path="刮削好的动画/钢之炼金术师FA/钢之炼金术师 FA - S01E03 - 邪教之街.mkv",
        source="baidu",
    )
    assert guess.work_title == "钢之炼金术师FA"
    assert guess.group_type == "season"
    assert guess.season_number == 1
    assert guess.episode_number == 3


# ============================================================
# 裸集数识别测试
# ============================================================

def test_bare_episode_dash():
    """Silent Witch - 01.mkv → season=1, episode=1"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Silent Witch - 01.mkv",
        relative_path="动画/Silent Witch/Silent Witch - 01.mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_bare_episode_dash_13():
    """Silent Witch - 13.mkv → season=1, episode=13"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Silent Witch - 13.mkv",
        relative_path="动画/Silent Witch/Silent Witch - 13.mkv",
    )
    assert guess.group_type == "season"
    assert guess.episode_number == 13


def test_bare_episode_with_dir_season():
    """One Room S2 01.mkv + 父目录 [LP-Raws] One Room S2 → season=2, episode=1"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="One Room S2 01.mkv",
        relative_path="动画/[LP-Raws] One Room S2 [Ma10p_1080p]/One Room S2 01.mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.season_number == 2, f"season: {guess.season_number}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_bare_episode_simple():
    """One Room 01.mkv → season=1, episode=1"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="One Room 01.mkv",
        relative_path="动画/[LP-Raws] One Room [Ma10p_1080p]/One Room 01.mkv",
    )
    assert guess.group_type == "season", f"group_type: {guess.group_type}"
    assert guess.episode_number == 1, f"episode: {guess.episode_number}"


def test_pure_numeric_local_filename_is_episode():
    """本地临时命名 8.mkv / 9.mkv 应作为当前作品的普通剧集处理。"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="8.mkv",
        relative_path="上伊那牡丹醉姿如百合/8.mkv",
        source="local",
    )

    assert guess.group_type == "season"
    assert guess.season_number == 1
    assert guess.episode_number == 8


def test_bare_episode_zero_is_sps():
    """00 → SPs"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="One Room S2 00.mkv",
        relative_path="动画/[LP-Raws] One Room S2 [Ma10p_1080p]/One Room S2 00.mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_bare_episode_half_is_sps():
    """11.5 → SPs"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="Title - 11.5.mkv",
        relative_path="动画/Title/Title - 11.5.mkv",
    )
    assert guess.group_type == "special", f"group_type: {guess.group_type}"


def test_ncop_still_op_ed():
    """NCOP 继续识别为 OP/ED，不被裸集数规则抢走"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="NCOP01.mkv",
        relative_path="动画/test/NCOP01.mkv",
    )
    assert guess.group_type == "ignored"


def test_pv_still_auxiliary():
    """PV 识别为附属视频，不进入 SPs/S00"""
    from app.recognition.media import recognize_media

    guess = recognize_media(
        filename="PV01.mkv",
        relative_path="动画/test/PV01.mkv",
    )
    assert guess.group_type == "auxiliary"
    assert guess.season_number == 0


# ============================================================
# 主入口
# ============================================================

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        # 作品名和年份
        test_work_title_and_year,
        test_work_title_chinese,
        test_work_title_mixed,
        test_work_title_with_status_word,
        # 正片季集
        test_standard_sxxexx,
        test_standard_s02e05,
        test_chinese_season_episode,
        test_bracket_episode,
        # 半集 / SPs
        test_half_episode_bracket,
        test_half_episode_chinese,
        test_sps_special_dir,
        test_sps_ova,
        test_zero_episode,
        # OP/ED
        test_op_ed_ncop,
        test_op_ed_directory,
        test_op_ed_priority_over_season,
        test_op_ed_priority_over_sps,
        test_no_false_positive_ed_in_redline,
        test_no_false_positive_sp_in_spy_family,
        # P4 字母边界反例
        test_no_false_positive_ova_in_casanova,
        test_no_false_positive_oad_in_roadshow,
        test_no_false_positive_special_in_specialized,
        test_no_false_positive_ova_in_casanova_movie_file,
        test_sps_ova_no_space_still_detected,
        test_sps_oad_keyword,
        test_sps_special_keyword_still_detected,
        test_sps_specials_plural_still_detected,
        test_non_credit_ed,
        test_no_subtitle_ed,
        # 独立卡片
        test_standalone_movie,
        test_standalone_recap,
        test_standalone_spin_off,
        # 非视频不识别
        test_non_video_not_recognized,
        # 不填 target
        test_no_target_fields,
        # 系列容器
        test_series_container,
        # 置信度
        test_confidence_high,
        test_confidence_low_no_group,
        # 裸集数
        test_bare_episode_dash, test_bare_episode_dash_13, test_bare_episode_with_dir_season,
        test_bare_episode_simple, test_bare_episode_zero_is_sps, test_bare_episode_half_is_sps,
        test_ncop_still_op_ed, test_pv_still_auxiliary,
        # 集成测试
        test_plan_recognizer_integration,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
