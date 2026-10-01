"""有限、版本化的 TMDB 连续编集规则；只消费编号，不改本地媒体事实。

出处为各 ID 的 https://www.themoviedb.org/tv/{id}/season/1；官方播出边界：
https://bisquedoll-anime.com/story/?id=13
https://www.puniru-anime.com/
https://jujutsukaisen.jp/episodes/index.php
https://re-zero-anime.jp/tv/story/tv2.html
未核验的季度或集号不得根据文件数量、缺集或未来在线集数扩展。
"""
from __future__ import annotations

import re
import unicodedata

RULE_VERSION = 'tmdb-continuous-2026-10-01-v1'

# (本地季号, 在线起点前的偏移, 本地集号上限)。常量不依赖本次导入文件数。
_CONTINUOUS_RANGES = {
    123249: ((1, 0, 12), (2, 12, 12)),
    241811: ((1, 0, 12), (2, 12, 12)),
    95479: ((1, 0, 24), (2, 24, 23), (3, 47, 12)),
    65942: ((1, 0, 25), (2, 25, 25), (3, 50, 16)),
}

# 用户文件中的中文分集标题与在线第一季逐集核对过；只用于逐条校验 S02 标签，
# 不是“同作品的 S02 一律等于 S01”。未知/不同标题不使用此例外。
_MISLABELED_TITLES = {
    116727: ('奇怪的司机', '度过长夜的方法', '小心虚张声势', '田中革命',
             '别叫我偶像', '我才想问是在搞什么', '不给糖就捣蛋', '祝你保重',
             '英雄的忧郁', '我们没有明天', '如果能回到那天', '不足的两人', '请问要去哪里？'),
    274810: ('渴望赴死的她在等待大海', '斜阳之兽与祭典音乐', '希望之海', '泡沫的结点',
             '亲爱之兽', '亲爱之形', '温柔的人', '裂痕之始', '烙下的祈愿',
             '倾注祈愿', '冰冷的清晨', '深爱的孩子', '温暖的海底'),
}


def normalized_episode_title(value: object) -> str:
    """仅做字符规范化，不模糊匹配、不把占位标题当成编号证据。"""
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    text = ''.join(c for c in text if c.isalnum())
    if len(text) < 3 or text in {'未命名', 'unknown', 'untitled', 'tba'}:
        return ''
    if re.fullmatch(r'(?:episode|ep|e|第)?\d+(?:集|话|話)?', text):
        return ''
    return text


def verified_tmdb_season_offsets(provider_id: int) -> dict[int, dict]:
    """返回新对象，调用方仍须核验远端单季布局与真实 Episode 字段。"""
    ranges = _CONTINUOUS_RANGES.get(provider_id, ())
    rules = {
        season: {'offset': offset, 'max_local_episode_number': maximum,
                 'provider_season_number': 1, 'rule_version': RULE_VERSION,
                 'source_url': f'https://www.themoviedb.org/tv/{provider_id}/season/1'}
        for season, offset, maximum in ranges
    }
    titles = _MISLABELED_TITLES.get(provider_id)
    if titles:
        rules[2] = {'offset': 0, 'max_local_episode_number': len(titles),
                    'provider_season_number': 1, 'rule_version': RULE_VERSION,
                    'source_url': f'https://www.themoviedb.org/tv/{provider_id}/season/1',
                    'episode_title_aliases': dict(enumerate(titles, 1))}
    return rules
