# -*- coding: utf-8 -*-
"""后端路径比较不得意外探测离线盘符。"""

import threading
import time
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("module_name", "function_name"),
    [
        ("app.library.index", "_normalize_path"),
        ("app.library.service", "_normalize_path"),
        ("app.scrape.store", "_map_path_key"),
        ("app.scrape.integrity", "_path_key"),
        ("app.media_presets.service", "_normalized_library_root"),
    ],
)
def test_path_key_helpers_are_lexical(monkeypatch, module_name, function_name):
    module = __import__(module_name, fromlist=[function_name])
    normalize_path = getattr(module, function_name)

    def fail_resolve(*args, **kwargs):
        raise AssertionError("路径键生成不得访问文件系统")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    result = normalize_path(r"Z:\offline\Anime\Season 01\..")

    assert result.casefold().replace("\\", "/") == "z:/offline/anime"


def test_tracking_path_comparison_is_lexical(monkeypatch):
    from app.tracking.registration import _same_path

    def fail_resolve(*args, **kwargs):
        raise AssertionError("路径比较不得访问文件系统")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    assert _same_path(
        Path(r"Z:\offline\Anime\Season 01\.."),
        Path(r"Z:\offline\Anime"),
    )


def test_legacy_library_visibility_does_not_probe_asset_paths(monkeypatch):
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import _visible_library_works

    def fail_is_file(*args, **kwargs):
        raise AssertionError("媒体库列表读取不得探测资源文件")

    monkeypatch.setattr(Path, "is_file", fail_is_file)
    work = WorkIndex(
        work_id="legacy-ready",
        title="旧索引作品",
        source="pan115",
        media_type="movie",
        metadata_state="ready",
        poster_path=r"Z:\offline\poster.jpg",
        fanart_path=r"Z:\offline\fanart.jpg",
        episodes=[EpisodeIndex(episode_id="movie", group_type="movie")],
    )
    index = LibraryIndex(version=1, works=[work])

    assert _visible_library_works(index, index.works) == [work]


def test_library_cache_signature_does_not_resolve_filesystem_paths(monkeypatch, tmp_path):
    from app.library.store import get_library_index_signature
    from app.tracking import store as tracking_store

    def fail_resolve(*args, **kwargs):
        raise AssertionError("媒体库缓存签名不得解析文件系统路径")

    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(Path, "resolve", fail_resolve)
    monkeypatch.setattr(tracking_store, "list_tracking_bindings", lambda: [])

    signature = get_library_index_signature()

    assert "library_index.json" in signature


def test_task_persistence_does_not_hold_registry_lock(monkeypatch):
    from app.tasks.manager import TaskManager

    manager = TaskManager(max_workers=1)
    persistence_entered = threading.Event()
    release_persistence = threading.Event()
    registry_read_finished = threading.Event()

    def slow_persist(_record):
        persistence_entered.set()
        release_persistence.wait(timeout=2)

    monkeypatch.setattr(manager, "_persist_task", slow_persist)
    creator = threading.Thread(
        target=manager._create_task,
        args=("stability_test", "local", "创建任务"),
        daemon=True,
    )
    creator.start()
    assert persistence_entered.wait(timeout=1)

    reader = threading.Thread(
        target=lambda: (manager.has_running_tasks(), registry_read_finished.set()),
        daemon=True,
    )
    reader.start()
    try:
        assert registry_read_finished.wait(timeout=0.2), "慢持久化占用了任务注册表锁"
    finally:
        release_persistence.set()
        creator.join(timeout=1)
        reader.join(timeout=1)
        manager.shutdown()


def test_task_cancellation_persistence_does_not_hold_registry_lock(monkeypatch):
    from app.tasks.manager import TaskManager

    manager = TaskManager(max_workers=1)
    monkeypatch.setattr(manager, "_persist_task", lambda _record: None)
    manager._create_task("scrape_auto", "pan115", "刮削")

    persistence_entered = threading.Event()
    release_persistence = threading.Event()
    registry_read_finished = threading.Event()

    def slow_persist(_record):
        persistence_entered.set()
        release_persistence.wait(timeout=2)

    monkeypatch.setattr(manager, "_persist_task", slow_persist)
    canceller = threading.Thread(
        target=manager.cancel_running_scrape_tasks,
        daemon=True,
    )
    canceller.start()
    assert persistence_entered.wait(timeout=1)

    reader = threading.Thread(
        target=lambda: (manager.has_running_tasks(), registry_read_finished.set()),
        daemon=True,
    )
    reader.start()
    try:
        assert registry_read_finished.wait(timeout=0.2), "取消任务持久化占用了任务注册表锁"
    finally:
        release_persistence.set()
        canceller.join(timeout=1)
        reader.join(timeout=1)
        manager.shutdown()


def test_concurrent_library_cache_miss_builds_response_once(monkeypatch):
    from app.api import library as library_api

    calls = 0

    def slow_library(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        time.sleep(0.1)
        return {
            "works": [],
            "summary": {"work_count": 0, "episode_count": 0, "source_summary": {}},
            "generated_at": "",
            "needs_rescan": False,
        }

    monkeypatch.setattr(library_api, "_prepare_response_cache", lambda: "stable")
    monkeypatch.setattr(library_api.library_service, "get_library", slow_library)
    library_api._LIBRARY_RESPONSE_CACHE.clear()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: library_api.get_library(compact=True), range(2)))

    assert calls == 1
    assert results[0] == results[1]


def test_media_path_probe_has_a_hard_timeout(monkeypatch):
    from app.api import config as config_api

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="path-probe", timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", timeout)

    states, timed_out = config_api._probe_media_path_states(
        Path(r"Z:\offline"),
        [Path(r"Z:\offline\Anime\episode.mkv")],
    )

    assert states == [False, False]
    assert timed_out is True


def test_library_response_enrichment_does_not_probe_mirror_filesystem(monkeypatch):
    from app.api import library as library_api
    from app.library import overrides
    from app.tracking import store as tracking_store

    def fail_probe(*args, **kwargs):
        raise AssertionError("媒体库读取 API 不得探测镜像文件系统")

    monkeypatch.setattr(library_api, "_read_work_title_from_nfo", fail_probe)
    monkeypatch.setattr(library_api, "_read_work_plot_from_nfo", fail_probe)
    monkeypatch.setattr(library_api, "_find_first_asset", fail_probe)
    monkeypatch.setattr(library_api, "_read_episode_nfo", fail_probe)
    monkeypatch.setattr(library_api, "load_watch_statuses", lambda: {})
    monkeypatch.setattr(tracking_store, "get_tracking_binding", lambda _work_id: None)
    monkeypatch.setattr(overrides, "get_work_override", lambda _work_id: None)

    work = {
        "work_id": "offline-work",
        "title": "离线作品",
        "media_type": "tv",
        "card_type": "main_series",
        "dir_path": r"Z:\offline\Anime",
        "poster_path": r"Z:\offline\Anime\poster.jpg",
        "fanart_path": r"Z:\offline\Anime\fanart.jpg",
        "episodes": [{
            "episode_id": "ep-1",
            "season_number": 1,
            "episode_number": 1,
            "group_type": "season",
            "title": "第一集",
            "strm_path": r"Z:\offline\Anime\Season 1\Anime - S01E01 - 第一集.strm",
        }],
        "seasons": [],
    }

    library_api._enrich_compact_payload({"works": [dict(work)]})
    library_api._enrich_payload(dict(work))
