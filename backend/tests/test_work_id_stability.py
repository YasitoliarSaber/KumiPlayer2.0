# -*- coding: utf-8 -*-
"""P0-1 work_id 生成策略稳定性 回归测试

背景（问题 5）：TXT 目录树导入出现大量重复卡片。根因之一是
`_make_work_id` 把 source/title/year/card_type 直接拼进 md5，标题
全角/半角、空白差异、年份缺失、来源不同都会产生不同 work_id，
导致同一作品被拆成多张卡片。

本测试锁定：
1. 标题规范化后全角/半角、空白差异不产生不同 work_id。
2. 年份 None / 0 统一为空串，不产生不同 work_id。
3. 同一作品跨 source 时 canonical_work_id 相同（合并键不含 source），
   work_id 保留 source 差异（来源级追踪）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_work_id_stable_across_fullwidth_title():
    """全角/半角标点差异不产生不同 work_id。"""
    from app.recognition.media import _make_work_id

    a = _make_work_id("pan115", "刀剑神域外传：暴风之铠", 2026, "")
    b = _make_work_id("pan115", "刀剑神域外传:暴风之铠", 2026, "")
    c = _make_work_id("pan115", "刀剑神域外传: 暴风之铠", 2026, "")
    assert a == b
    assert a == c


def test_work_id_stable_across_fullwidth_parenthesis():
    """全角/半角括号差异不产生不同 work_id。"""
    from app.recognition.media import _make_work_id

    a = _make_work_id("pan115", "魔法少女小圆（剧场版）", 2013, "standalone")
    b = _make_work_id("pan115", "魔法少女小圆(剧场版)", 2013, "standalone")
    assert a == b


def test_work_id_stable_across_whitespace_collapse():
    """连续空白压缩后不产生不同 work_id。"""
    from app.recognition.media import _make_work_id

    a = _make_work_id("pan115", "  赛马娘  Pretty Derby  ", 2018, "")
    b = _make_work_id("pan115", "赛马娘 Pretty Derby", 2018, "")
    assert a == b


def test_work_id_year_none_and_zero_unified():
    """year None 与 0 统一为空串（不区分），但真实年份保留差异。"""
    from app.recognition.media import _make_work_id

    a = _make_work_id("pan115", "某作品", None, "")
    b = _make_work_id("pan115", "某作品", 0, "")
    assert a == b

    c = _make_work_id("pan115", "某作品", 2008, "")
    assert a != c  # 年份不同必须产生不同 work_id（保留既有契约）


def test_canonical_work_id_cross_source_same():
    """同一作品跨 source：canonical_work_id 相同（不含 source）。"""
    from app.recognition.media import _make_canonical_work_id

    a = _make_canonical_work_id("pan115", "刀剑神域外传：暴风之铠", 2026, "")
    b = _make_canonical_work_id("openlist", "刀剑神域外传:暴风之铠", 2026, "")
    c = _make_canonical_work_id("local", "刀剑神域外传：暴风之铠", 2026, "main_series")
    assert a == b
    assert a == c


def test_work_id_keeps_source_difference():
    """work_id 保留 source 差异（来源级追踪），不因规范化而合并。"""
    from app.recognition.media import _make_work_id

    a = _make_work_id("pan115", "刀剑神域外传：暴风之铠", 2026, "")
    b = _make_work_id("openlist", "刀剑神域外传：暴风之铠", 2026, "")
    assert a != b


def test_canonical_work_id_differs_by_title_year():
    """canonical 合并键对不同标题/年份必须不同（防止错误合并）。"""
    from app.recognition.media import _make_canonical_work_id

    a = _make_canonical_work_id("pan115", "作品A", 2020, "")
    b = _make_canonical_work_id("pan115", "作品B", 2020, "")
    assert a != b

    c = _make_canonical_work_id("pan115", "作品A", 2021, "")
    assert a != c
