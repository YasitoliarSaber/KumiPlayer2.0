"""借鉴目录树路径的容器规则：结构目录不再被当成作品容器（审计驱动）。

用户反馈：OpenList 路径导出的作品里出现名为「OVA」「特别篇」「始动篇」的独立作品，
而目录树/本地路径没有这个问题。A/B 对比确认根因在 `_extract_work_container` 的
OpenList 分支：遇到季目录直接放弃容器（调用方回退到未清洗的文件名），且不认
特典类目录（``摇曳露营/特别篇`` 的 ``特别篇`` 被当成作品容器）。
"""

from __future__ import annotations

from app.recognition.media import _extract_work_container, _looks_like_specials_dir
from app.recognition.title_cleaner import strip_leading_quality_prefix


def test_structure_dirs_are_skipped_and_work_container_is_kept():
    # 季目录：向作品层回退，而不是放弃容器
    assert _extract_work_container("摇曳露营/第2季/摇曳露营 S02E01.mkv", "openlist") == "摇曳露营"
    assert _extract_work_container("1.命运石之门/Season 1/E01.mkv", "openlist") == "1.命运石之门"
    # 特典目录同理（修复前返回 "特别篇"，直接变成一部作品）
    assert _extract_work_container("摇曳露营/特别篇/摇曳露营 SP01.mkv", "openlist") == "摇曳露营"
    # 分类层 + 作品目录保持不变
    assert _extract_work_container("动画/CLANNAD/Season 1/E01.mkv", "openlist") == "CLANNAD"


def test_specials_dir_matching_is_exact_segment():
    for name in ("特别篇", "OVA", "OAD", "映像特典", "SP", "NCOP", "Menu"):
        assert _looks_like_specials_dir(name), name
    # 反证：真标题不能被当成结构目录
    for name in ("SPY×FAMILY", "OVAlord", "特别篇外传", "Edition"):
        assert not _looks_like_specials_dir(name), name


def test_leading_specials_prefix_is_stripped_from_titles():
    assert strip_leading_quality_prefix("OVA 小魔女学园") == "小魔女学园"
    assert strip_leading_quality_prefix("特别篇 摇曳露营") == "摇曳露营"
    assert strip_leading_quality_prefix("4k 彻夜之歌") == "彻夜之歌"
    # 反证：不带分隔符的同前缀真标题不受影响
    assert strip_leading_quality_prefix("OVAlord") == "OVAlord"
