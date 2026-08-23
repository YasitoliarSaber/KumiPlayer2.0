# -*- coding: utf-8 -*-
"""验证旧数据（canonical_work_id 为空）的 standalone 同标题合并。

模拟用户报告的 bug：TXT 导入产生多个不同 work_id 但同标题的卡片
（如刀剑神域外传 7 张、中二病 3 张、咒术回战 3 张）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.library.models import WorkIndex, EpisodeIndex
from app.library.service import _deduplicate_library_works


def _make_work(title, source="pan115", card_type="standalone", work_id_suffix=""):
    """构造一个有 1 集的 WorkIndex，canonical_work_id 为空（模拟旧数据）。"""
    return WorkIndex(
        work_id=f"work_{work_id_suffix}",
        canonical_work_id="",  # 模拟旧数据：canonical 为空
        title=title,
        source=source,
        card_type=card_type,
        episodes=[EpisodeIndex(
            strm_path=f"test/{work_id_suffix}/01.strm",
            season_number=1,
            episode_number=1,
        )],
    )


def test_canonical_fallback_merges_same_title():
    """同标题同年份、不同 work_id 的 standalone 应合并成一张卡。

    模拟用户报告：刀剑神域外传 7 张、中二病 3 张、咒术回战 2 张。
    根因：WorkIndex 构造时 canonical_work_id 为空（旧数据），
    _deduplicate_library_works 走 legacy 分支按 source+work_id 不合并。
    """
    works = [
        _make_work("刀剑神域外传：暴风之铳", work_id_suffix="sao1"),
        _make_work("刀剑神域外传：暴风之铳", work_id_suffix="sao2"),
        _make_work("刀剑神域外传：暴风之铳", work_id_suffix="sao3"),
        _make_work("中二病也要谈恋爱！", work_id_suffix="chu1"),
        _make_work("中二病也要谈恋爱！", work_id_suffix="chu2"),
        _make_work("咒术回战", work_id_suffix="jju1"),
        _make_work("咒术回战", work_id_suffix="jju2"),
    ]
    normalized = _deduplicate_library_works(works)

    sao = [w for w in normalized if "刀剑神域外传" in w.title]
    chuunibyou = [w for w in normalized if "中二病" in w.title]
    jujutsu = [w for w in normalized if "咒术回战" in w.title]

    assert len(sao) == 1, f"刀剑神域外传应合并为1张，实际{len(sao)}张"
    assert len(chuunibyou) == 1, f"中二病应合并为1张，实际{len(chuunibyou)}张"
    assert len(jujutsu) == 1, f"咒术回战应合并为1张，实际{len(jujutsu)}张"


def test_different_titles_not_merged():
    """不同标题的 standalone 不应合并。"""
    works = [
        _make_work("作品A", work_id_suffix="a"),
        _make_work("作品B", work_id_suffix="b"),
    ]
    normalized = _deduplicate_library_works(works)
    assert len(normalized) == 2, f"不同标题应保持2张，实际{len(normalized)}张"


if __name__ == "__main__":
    test_canonical_fallback_merges_same_title()
    test_different_titles_not_merged()
    print("PASS: 旧数据 canonical 兜底合并验证通过")
