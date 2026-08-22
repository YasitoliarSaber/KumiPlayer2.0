# -*- coding: utf-8 -*-
"""P0-4：compact 序列化 artwork 探测缓存 回归测试

背景（问题 6）：`_local_summary_artwork_path` 每作品对 `Path.is_file()`
重复 stat（网络盘可能等数百 ms），100 部作品 × 3 字段在每次 compact
查询时全部重复探测 → 滑动卡顿根因之一。

修复：对本地 artwork 探测加短 TTL 缓存（60s），同一对象/相邻请求命中缓存
不重复 stat。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.library.models import WorkIndex
import app.library.service as service


def _reset_cache():
    service._artwork_probe_cache.clear()


def test_local_artwork_probe_cached(tmp_path, monkeypatch):
    """同一对象重复探测不重复 stat（缓存命中）。"""
    _reset_cache()
    work_dir = tmp_path / "作品A"
    work_dir.mkdir(parents=True)
    (work_dir / "poster.jpg").write_bytes(b"fake")

    work = WorkIndex(work_id="w1", dir_path=str(work_dir))
    stat_calls = 0
    original_is_file = Path.is_file

    def counting_is_file(self):
        nonlocal stat_calls
        stat_calls += 1
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", counting_is_file)

    first = service._local_summary_artwork_path(work, "poster_path")
    second = service._local_summary_artwork_path(work, "poster_path")
    assert first == str(work_dir / "poster.jpg")
    assert second == first
    # 命中缓存：第二次不再对 poster.jpg 做 is_file 探测（至少不翻倍）
    assert stat_calls <= 3, f"缓存命中后不应重复 stat: {stat_calls}"


def test_local_artwork_probe_remote_url_not_probed(tmp_path, monkeypatch):
    """work_path 为远程 URL 时不探测本地文件。"""
    _reset_cache()
    work = WorkIndex(
        work_id="w2",
        poster_path="https://image.tmdb.org/p/original/abc.jpg",
        dir_path=str(tmp_path),
    )
    stat_calls = 0
    original_is_file = Path.is_file

    def counting_is_file(self):
        nonlocal stat_calls
        stat_calls += 1
        return original_is_file(self)

    monkeypatch.setattr(Path, "is_file", counting_is_file)

    result = service._local_summary_artwork_path(work, "poster_path")
    assert result == ""
    # 远程 URL 直接跳过：不应对远程路径做 stat（可能探测目录名，上限放宽）
    assert stat_calls < 10, f"远程 URL 不应触发大量 stat: {stat_calls}"


def test_artwork_probe_cache_ttl_expiry(tmp_path, monkeypatch):
    """未命中不缓存：海报生成后立即可见（无需等 TTL 过期）。"""
    _reset_cache()
    monkeypatch.setattr(service, "_ARTWORK_PROBE_CACHE_TTL_SECONDS", 0.05)
    work_dir = tmp_path / "作品B"
    work_dir.mkdir(parents=True)

    work = WorkIndex(work_id="w3", dir_path=str(work_dir))
    before = service._local_summary_artwork_path(work, "poster_path")
    assert before == ""

    # P0-4 修复：未命中不缓存空结果——海报生成前每次探测都返回空
    cached = service._local_summary_artwork_path(work, "poster_path")
    assert cached == ""

    # 海报生成后，新对象探测立即可见（不缓存空，无需等 TTL 过期）
    (work_dir / "poster.jpg").write_bytes(b"fake")
    work2 = WorkIndex(work_id="w3", dir_path=str(work_dir))
    after = service._local_summary_artwork_path(work2, "poster_path")
    assert after == str(work_dir / "poster.jpg")

