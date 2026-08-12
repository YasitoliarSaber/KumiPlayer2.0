# -*- coding: utf-8 -*-
"""T02 回归测试：AIR OP＆ED、NFO 转义、年份提取、scrape title 清理"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.recognition.media import recognize_media, _extract_year
from app.scrape.nfo import generate_movie_nfo, generate_tvshow_nfo
from app.scrape.target_builder import _clean_scrape_title
from app.recognition.plan_recognizer import recognize_import_plan_media
from app.import_plan.models import ImportPlan, ImportPlanItem


# ============================================================
# AIR OP＆ED 回归测试
# ============================================================

def test_air_op_ed_directory():
    """AIR.S01E05 在 OP＆ED 目录 → op_ed"""
    guess = recognize_media(
        filename="[Ygm]AIR [NCOP01][Ma10_1080p][2flac 5.1ch AC3].mkv",
        relative_path="动画/AIR.2005/OP＆ED/[Ygm]AIR [NCOP01][Ma10_1080p][2flac 5.1ch AC3].mkv",
    )
    assert guess.group_type == "ignored", f"expected op_ed, got {guess.group_type}"


def test_air_season_not_polluted_by_op_ed():
    """AIR 正片不被 OP＆ED 目录污染"""
    guess = recognize_media(
        filename="AIR.S01E05.mkv",
        relative_path="动画/AIR.2005/AIR.S01E05.mkv",
    )
    assert guess.group_type == "season", f"expected season, got {guess.group_type}"
    assert guess.episode_number == 5


def test_op_ed_no_scrape_target():
    """OP＆ED 目录条目不生成 scrape_target"""
    plan = ImportPlan(
        plan_id="test",
        source="pan115",
        status="draft",
        items=[
            ImportPlanItem(
                id="item1",
                resource_type="video",
                action="generate_strm",
                relative_path="动画/AIR.2005/OP＆ED/[Ygm]AIR [NCOP01].mkv",
                real_path="H:\\115open\\动画\\AIR.2005\\OP＆ED\\[Ygm]AIR [NCOP01].mkv",
            ),
        ],
    )
    plan = recognize_import_plan_media(plan)
    assert plan.items[0].group_type == "ignored"
    # op_ed 不应有 season/movie 的 group_type
    assert plan.items[0].group_type != "season"
    assert plan.items[0].group_type != "movie"


# ============================================================
# NFO XML 转义定性测试
# ============================================================

def test_nfo_movie_escape():
    """movie NFO 特殊字符被转义"""
    nfo = generate_movie_nfo(title="<script>alert(1)</script>", plot="A & B < C")
    assert "<script>" not in nfo
    assert "&lt;script&gt;" in nfo
    assert "&amp;" in nfo
    # XML parser 可以解析
    ET.fromstring(nfo)


def test_nfo_tvshow_escape():
    """tvshow NFO 特殊字符被转义"""
    nfo = generate_tvshow_nfo(title='He said "hello"', plot="A & B")
    assert "&quot;" in nfo or '"' in nfo  # xml.escape 处理引号
    assert "&amp;" in nfo
    # XML parser 可以解析
    ET.fromstring(nfo)


def test_nfo_normal_content():
    """正常内容不被过度转义"""
    nfo = generate_movie_nfo(title="CLANNAD", year=2007, plot="A visual novel.")
    assert "<title>CLANNAD</title>" in nfo
    assert "<year>2007</year>" in nfo
    ET.fromstring(nfo)


# ============================================================
# 年份提取前导空格测试
# ============================================================

def test_extract_year_with_leading_space():
    """_extract_year(" 2019") == 2019"""
    assert _extract_year(" 2019") == 2019


def test_extract_year_normal():
    """_extract_year("2019") == 2019"""
    assert _extract_year("2019") == 2019


def test_extract_year_dot_format():
    """".2019 格式"""
    assert _extract_year("Title.2019") == 2019


def test_extract_year_paren_format():
    """(2019) 格式"""
    assert _extract_year("Title (2019)") == 2019


def test_extract_year_invalid():
    """超出范围的数字不返回"""
    assert _extract_year("1234") is None
    assert _extract_year("") is None


# ============================================================
# Scrape title 连续点号清理测试
# ============================================================

def test_clean_scrape_title_double_dot():
    """CLANNAD After Story.[S02].2008 -> CLANNAD After Story"""
    result = _clean_scrape_title("CLANNAD After Story.[S02].2008")
    assert result == "CLANNAD After Story"


def test_clean_scrape_title_normal():
    """正常标题不被破坏"""
    result = _clean_scrape_title("Steins;Gate 0")
    assert result == "Steins;Gate 0"


def test_clean_scrape_title_bracket_tags():
    """方括号技术标签被清理"""
    result = _clean_scrape_title("One Room S2 [Ma10p_1080p]")
    assert result == "One Room"


if __name__ == "__main__":
    tests = [
        test_air_op_ed_directory,
        test_air_season_not_polluted_by_op_ed,
        test_op_ed_no_scrape_target,
        test_nfo_movie_escape,
        test_nfo_tvshow_escape,
        test_nfo_normal_content,
        test_extract_year_with_leading_space,
        test_extract_year_normal,
        test_extract_year_dot_format,
        test_extract_year_paren_format,
        test_extract_year_invalid,
        test_clean_scrape_title_double_dot,
        test_clean_scrape_title_normal,
        test_clean_scrape_title_bracket_tags,
    ]
    for t in tests:
        t()
        print(f"PASSED: {t.__name__}")
    print(f"ALL {len(tests)} PASSED")
