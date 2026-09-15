"""B2：伪父系列（作品把自己当父系列）的生成端修复与历史行只读收口。

背景：`_relation_work_key_from_row` 原先先对 movie 子作品强制父类型为 tv，
再判断"父是不是自己"，而自名抑制要求 `media_type == child_media_type`，
于是抑制必然失效，生成 `series:<作品自身标题>:tv` 这种永远查不到的父键。
结果：来源卡长期显示"11 项关联信息待补全"，而父系列本就不该存在。

修复分两层：
1. 生成端：自名判定提前到强制 tv 之前；
2. 历史行：投影层只读判定"父键标题 == 子作品标题"→ 不是待补项（不改真实库）。
"""

from __future__ import annotations

import pytest


def _database(tmp_path):
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "relation.db")
    database.initialize()
    return database


def _insert_work(conn, *, work_id: str, title: str, year: int, work_type: str, identity_key: str) -> None:
    conn.execute(
        "INSERT INTO works (work_id, identity_key, work_type, preferred_title, original_title, "
        "year, status, created_at, updated_at, show_type, card_type) "
        "VALUES (?, ?, ?, ?, '', ?, 'active', 'now', 'now', '', 'standalone')",
        (work_id, identity_key, work_type, title, year),
    )


@pytest.mark.parametrize("relation_type", ["movie", "spin_off", "recap", "related"])
def test_movie_whose_series_group_is_its_own_title_has_no_parent_key(relation_type):
    from app.media_v4.resolution.resolver import _relation_work_key_from_row

    assert _relation_work_key_from_row({
        "title": "龙猫",
        "series_group": "龙猫",
        "media_type": "movie",
        "relation_type": relation_type,
    }) == ""


def test_self_name_suppression_uses_exact_normalized_title():
    """自名判定是"归一化后精确相等"，与真实库中出现的 11 条伪父系列一致。

    刻意不做"去掉发布后缀/年份后再比"的模糊匹配：真实库里的伪父键就是
    `series:龙猫:tv` 这种精确等于作品标题的形式；而放宽比较有可能把
    "《Show》→《Show (2020)》"这类真实父子关系也误判成自关联，代价更大。
    带发布后缀的 series_group 属于另一种成因，需要先有真实样本再定规则。
    """

    from app.media_v4.resolution.resolver import _relation_work_key_from_row

    # 精确相等 → 抑制（11 条真实伪父系列即此形态）
    assert _relation_work_key_from_row({
        "title": "龙猫", "series_group": "龙猫", "media_type": "movie",
        "relation_type": "movie",
    }) == ""

    # 不相等 → 仍然按父系列处理（宁可留一条待补提示，也不误删真实父子关系）
    assert _relation_work_key_from_row({
        "title": "龙猫", "series_group": "龙猫 (1988) -1080p-Blu-ray",
        "media_type": "movie", "relation_type": "movie",
    })


def test_title_with_colon_is_not_split_into_a_fake_parent():
    from app.media_v4.resolution.resolver import _relation_work_key_from_row

    assert _relation_work_key_from_row({
        "title": "福音战士新剧场版：序",
        "series_group": "福音战士新剧场版：序",
        "media_type": "movie",
        "relation_type": "movie",
    }) == ""


def test_self_name_suppression_only_applies_to_the_forced_parent_type():
    """两类"同名"必须区别对待：

    - 被 movie 关系规则**强制**收口成 tv 的（relation_type=movie 等）→ 是伪父系列，抑制；
    - **显式**声明父类型不同的（relation_media_type=tv）→ 同名电影确实有 TV 主系列，保留。
    """

    from app.media_v4.resolution.resolver import _relation_work_key_from_row

    forced = _relation_work_key_from_row({
        "title": "同名作品", "series_group": "同名作品", "media_type": "movie",
        "relation_type": "movie",
    })
    explicit = _relation_work_key_from_row({
        "title": "同名作品", "series_group": "同名作品", "media_type": "movie",
        "relation_media_type": "tv",
    })

    assert forced == ""
    assert explicit == "series:同名作品:tv"


def test_real_spin_off_still_maps_to_tv_parent():
    """真正的外传必须仍然建立父系列（父类型收口为 tv 的原有行为不能丢）。"""

    from app.media_v4.resolution.resolver import _normalize_title, _relation_work_key_from_row

    key = _relation_work_key_from_row({
        "title": "Heya Camp",
        "series_group": "Yuru Camp",
        "media_type": "movie",
        "relation_type": "spin_off",
    })

    assert key == f"series:{_normalize_title('Yuru Camp')}:tv"


def test_pseudo_parent_issue_is_not_counted_as_pending(tmp_path):
    from app.media_v4.projection.source_libraries import _relation_is_pending

    database = _database(tmp_path)
    with database.connect() as conn:
        _insert_work(
            conn, work_id="w-totoro", title="龙猫", year=1988, work_type="movie",
            identity_key="title:龙猫:1988:movie",
        )
        issue = {
            "message": "父系列 series:龙猫:tv 尚未导入，无法建立作品关系",
            "evidence_id": "w-totoro",
        }
        assert _relation_is_pending(conn, issue) is False


def test_pseudo_parent_with_colon_title_is_not_pending(tmp_path):
    from app.media_v4.projection.source_libraries import _relation_is_pending

    database = _database(tmp_path)
    with database.connect() as conn:
        _insert_work(
            conn, work_id="w-eva", title="福音战士新剧场版：序", year=2007, work_type="movie",
            identity_key="title:福音战士新剧场版:序:2007:movie",
        )
        issue = {
            "message": "父系列 series:福音战士新剧场版:序:tv 尚未导入，无法建立作品关系",
            "evidence_id": "w-eva",
        }
        assert _relation_is_pending(conn, issue) is False


def test_genuine_missing_parent_still_counts_as_pending(tmp_path):
    from app.media_v4.projection.source_libraries import _relation_is_pending

    database = _database(tmp_path)
    with database.connect() as conn:
        _insert_work(
            conn, work_id="w-heya", title="Heya Camp", year=2020, work_type="tv",
            identity_key="title:heya camp:2020:tv",
        )
        issue = {
            "message": "父系列 series:yuru camp:tv 尚未导入，无法建立作品关系",
            "evidence_id": "w-heya",
        }
        assert _relation_is_pending(conn, issue) is True


def test_unparsable_relation_message_stays_pending(tmp_path):
    from app.media_v4.projection.source_libraries import _relation_is_pending

    database = _database(tmp_path)
    with database.connect() as conn:
        assert _relation_is_pending(conn, {"message": "其它历史提示", "evidence_id": "x"}) is True
