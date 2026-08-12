# -*- coding: utf-8 -*-
"""T01R 刮削检查表候选名回归测试"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def test_extract_subwork_dir_from_reasons():
    from tools.export_scrape_review_excel import _extract_subwork_dir

    item = {
        "reasons": ["子作品目录: 3.CLANNAD After Story.[S02].2008"],
        "warnings": [],
    }

    assert _extract_subwork_dir(item) == "3.CLANNAD After Story.[S02].2008"


def test_scrape_candidate_keeps_official_subtitle_and_removes_season_token():
    from tools.export_scrape_review_excel import _extract_scrape_candidate

    item = {
        "group_type": "season",
        "work_title": "CLANNAD",
        "year": 2008,
        "reasons": ["子作品目录: 3.CLANNAD After Story.[S02].2008"],
        "warnings": [],
    }

    assert _extract_scrape_candidate(item) == ("CLANNAD After Story", 2008)


def test_scrape_candidate_cleans_fansub_and_tech_but_keeps_season_field_separate():
    from tools.export_scrape_review_excel import _extract_scrape_candidate

    item = {
        "group_type": "season",
        "work_title": "One Room",
        "year": None,
        "season_number": 2,
        "reasons": ["子作品目录: [LP-Raws] One Room S2 [Ma10p_1080p]"],
        "warnings": [],
    }

    assert _extract_scrape_candidate(item) == ("One Room", None)
    assert item["season_number"] == 2


def test_scrape_candidate_extracts_year_from_subwork_dir():
    from tools.export_scrape_review_excel import _extract_scrape_candidate

    item = {
        "group_type": "season",
        "work_title": "冰海战记",
        "year": None,
        "season_number": 2,
        "reasons": ["子作品目录: 冰海战记.[S2].2023"],
        "warnings": [],
    }

    assert _extract_scrape_candidate(item) == ("冰海战记", 2023)


def test_movie_candidate_keeps_special_title():
    from tools.export_scrape_review_excel import _extract_scrape_candidate

    item = {
        "group_type": "movie",
        "work_title": "CLANNAD",
        "year": 2009,
        "reasons": ["子作品目录: 5.CLANNAD总集篇：在那苍绿的树下.2009"],
        "warnings": [],
    }

    assert _extract_scrape_candidate(item) == ("CLANNAD总集篇：在那苍绿的树下", 2009)
