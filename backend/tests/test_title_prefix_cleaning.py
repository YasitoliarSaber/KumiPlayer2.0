"""标题前缀清洗：画质标记不在首 token 时也要去掉，序号前缀要带反例保护。

真因（用户库里实测的 parsed_facts）：
- ``work_title='4k Clannad'`` 来自路径 ``4k 京阿尼合集/C 4k Clannad/第一季/…``
  —— 画质标记落在**第二个** token，只剥一次第一个 token 会留下中间的 ``4k``；
- ``series_group='4k 偶像大师 灰姑娘女孩 U149'`` 来自选中根目录名
  ``O 4k 偶像大师 灰姑娘女孩 U149``（同样两段装饰）。

序号前缀（``1.命运石之门``）按用户特别叮嘱收紧：真标题里带数字的
``2.5次元的诱惑`` / ``86-不存在的战区-`` / ``3月的狮子`` / ``22／7`` / ``91Days``
/ ``7SEEDS`` 必须一律保持原样。
"""

from __future__ import annotations

import pytest
from app.recognition.title_cleaner import (
    clean_work_title_container,
    strip_leading_decoration_run,
    strip_ordering_prefix,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C 4k Clannad", "Clannad"),
        ("O 4k 偶像大师 灰姑娘女孩 U149", "偶像大师 灰姑娘女孩 U149"),
        ("B 4k 彻夜之歌", "彻夜之歌"),
        ("4k 彻夜之歌", "彻夜之歌"),
        ("4k彻夜之歌", "彻夜之歌"),
        ("2160p 冰海战记", "冰海战记"),
    ],
)
def test_quality_marker_is_stripped_even_when_not_the_first_token(raw, expected):
    assert clean_work_title_container(raw).title == expected
    assert strip_leading_decoration_run(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.命运石之门", "命运石之门"),
        ("16.物语系列", "物语系列"),
        ("3.命运石之门 0", "命运石之门 0"),
        ("5.新·福音战士剧场版 序", "新·福音战士剧场版 序"),
        ("7、某科学的超电磁炮", "某科学的超电磁炮"),
    ],
)
def test_ordering_prefix_is_stripped(raw, expected):
    assert clean_work_title_container(raw).title == expected
    assert strip_ordering_prefix(raw) == expected


@pytest.mark.parametrize(
    "real_title",
    [
        "2.5次元的诱惑",      # 点号后是数字：剥了会变成 "5次元的诱惑"
        "86-不存在的战区-",   # 连字符不是序号分隔符
        "3月的狮子",          # 数字后直接接中文，没有分隔符
        "22／7",              # 全角斜杠
        "91Days",
        "7SEEDS",
        "009-1",
        "5.5 秒的彩虹",
    ],
)
def test_real_titles_that_start_with_digits_are_never_touched(real_title):
    assert clean_work_title_container(real_title).title == real_title
    assert strip_ordering_prefix(real_title) == real_title


def test_ordering_label_is_reported():
    result = clean_work_title_container("1.命运石之门")

    assert any("序号" in rule for rule in result.applied_rules)
