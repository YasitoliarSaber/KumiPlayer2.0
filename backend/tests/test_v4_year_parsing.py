"""O4-2：以数字结尾的片名不得被当年份（同时改错标题、年份与身份键）。

真实片名常以孤立数字结尾：``Blade Runner 2049``、``2012``、``银翼杀手 2049``。
把它们当年份会连锁出三处错误：标题被删短、年份记错、而年份进入身份键后会被刮削
评分当成"年份差 ≥ 2"直接阻断候选，作品永远停在人工确认。

同时锁定"同一模式里第一个越界匹配不该终结整个模式"：``作品.2160p.2019`` 的第一个
``.2160`` 不是年份，但同一模式的下一个匹配 ``.2019`` 才是。
"""

from __future__ import annotations

from datetime import datetime

import pytest

_THIS_YEAR = datetime.now().year


def test_title_ending_with_implausible_number_keeps_the_number():
    from app.recognition.media import _extract_year, _parse_work_title_and_year

    future_like = _THIS_YEAR + 30
    title = f"Blade Runner {future_like}"

    assert _extract_year(title) is None, "远超当前年的数字不是年份"
    assert _parse_work_title_and_year(title) == (title, None)


def test_title_ending_with_plausible_year_is_still_parsed():
    from app.recognition.media import _extract_year, _parse_work_title_and_year

    plausible = _THIS_YEAR - 3
    title = f"作品 {plausible}"

    assert _extract_year(title) == plausible
    assert _parse_work_title_and_year(title) == ("作品", plausible)


def test_dotted_year_still_parsed_after_an_out_of_range_match():
    from app.recognition.media import _extract_year, _extract_year_from_subwork

    assert _extract_year("作品.2160p.2019") == 2019
    assert _extract_year_from_subwork("Show.2160p.2019") == 2019


def test_dotted_and_parenthesised_years_are_still_stripped_from_titles():
    from app.recognition.title_cleaner import clean_work_title_container

    assert clean_work_title_container("AIR.2005").title == "AIR"
    assert clean_work_title_container("作品 (2021)").title == "作品"
    assert clean_work_title_container("作品 (2021) -1080p-Blu-ray").title == "作品"


def test_title_cleaner_keeps_implausible_trailing_number():
    from app.recognition.title_cleaner import clean_work_title_container

    future_like = _THIS_YEAR + 30
    assert clean_work_title_container(f"Blade Runner {future_like}").title == f"Blade Runner {future_like}"


@pytest.mark.parametrize("current_year, value, expected", [
    (2026, 2028, True),    # 当前年 + 2（次年新番预告）
    (2026, 2029, False),   # 超出容差
    (2026, 2026, True),
    (2026, 1899, False),   # 低于下界
    (2026, 1900, True),
    (2026, 2099, False),   # 像片名而不是年份
])
def test_is_plausible_year_boundaries(current_year, value, expected):
    from app.recognition.title_cleaner import is_plausible_year

    assert is_plausible_year(value, current_year=current_year) is expected
