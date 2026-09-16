"""O15：浏览缓存热路径不能每次命中都重写整份索引，淘汰不能是 O(n²)。

`read_cache` 命中时要维护 LRU 的 `last_accessed_at`，但每次命中都读+重写整份索引
（2000 条 ≈ 数百 KB）在"每次翻页一次命中"的频率下是纯浪费；淘汰循环内每轮
`sum()` 全量重算则是 O(n²)。LRU 只需要分钟级精度，因此写回按间隔节流。
"""

from __future__ import annotations


def _entries(count: int) -> list[dict]:
    return [
        {
            "name": f"F{index}.mkv",
            "is_dir": False,
            "size": 1024,
            "modified": 1_700_000_000,
            "remote_path": f"/Anime/F{index}.mkv",
        }
        for index in range(count)
    ]


def _write(conn_key: str = "conn-1", remote_path: str = "/Anime", *, now: float = 1000.0):
    from app.integrations.openlist.cache import write_cache

    return write_cache(conn_key, remote_path, _entries(3), 10, now=now)


def test_cache_hit_within_window_does_not_rewrite_the_index(monkeypatch):
    import app.integrations.openlist.cache as cache

    _write()
    writes: list[int] = []
    real_write_index = cache._write_index
    monkeypatch.setattr(cache, "_write_index", lambda *a, **k: writes.append(1) or real_write_index(*a, **k))

    payload = None
    for offset in range(5):
        payload = cache.read_cache("conn-1", "/Anime", now=1000.0 + offset)

    assert payload is not None and payload["entry_count"] == 3, "命中必须照常返回缓存内容"
    assert writes == [], f"窗口内的命中不应重写索引，实际写了 {len(writes)} 次"


def test_index_is_written_once_the_access_time_is_stale(monkeypatch):
    import app.integrations.openlist.cache as cache

    _write()
    writes: list[int] = []
    real_write_index = cache._write_index
    monkeypatch.setattr(cache, "_write_index", lambda *a, **k: writes.append(1) or real_write_index(*a, **k))

    cache.read_cache("conn-1", "/Anime", now=1000.0 + cache.INDEX_WRITE_MIN_INTERVAL_SECONDS + 1)

    assert len(writes) == 1, "超过节流间隔后必须把访问时间落盘，LRU 才不会失效"


def test_missing_index_entry_is_registered_on_hit():
    import app.integrations.openlist.cache as cache

    _write()
    key = cache._page_key("/Anime", 1, 100)
    # 真实删掉索引项（缓存文件仍在），再命中一次。
    with cache._cache_lock:
        index = cache._read_index("conn-1")
        index.pop(key, None)
        cache._write_index("conn-1", index)
    assert key not in cache._read_index("conn-1")

    payload = cache.read_cache("conn-1", "/Anime", now=1001.0)
    assert payload is not None

    index = cache._read_index("conn-1")
    assert key in index, "索引项缺失时必须在命中时补登记"
    assert index[key]["size_bytes"] > 0
    assert index[key]["last_accessed_at"] == 1001.0


def test_eviction_respects_caps_and_removes_oldest_first(monkeypatch):
    import app.integrations.openlist.cache as cache

    monkeypatch.setattr(cache, "MAX_CACHE_PATHS", 3)
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", 10 ** 9)

    for index in range(5):
        _write(remote_path=f"/Anime/P{index}", now=1000.0 + index)

    with cache._cache_lock:
        index = cache._read_index("conn-1")
        cache._evict_locked("conn-1", index)

    assert len(index) == 3, f"淘汰后应保留 3 条，实际 {len(index)}"
    remaining = {meta["path"] for meta in index.values()}
    assert remaining == {"/Anime/P2", "/Anime/P3", "/Anime/P4"}, "应按 last_accessed_at 保留最近的"


def test_eviction_respects_byte_cap(monkeypatch):
    import app.integrations.openlist.cache as cache

    monkeypatch.setattr(cache, "MAX_CACHE_PATHS", 100)
    monkeypatch.setattr(cache, "MAX_CACHE_BYTES", 1)

    for index in range(4):
        _write(remote_path=f"/Anime/B{index}", now=1000.0 + index)

    with cache._cache_lock:
        index = cache._read_index("conn-1")
        cache._evict_locked("conn-1", index)

    assert index == {}, "字节上限极小时应把缓存全部淘汰"
