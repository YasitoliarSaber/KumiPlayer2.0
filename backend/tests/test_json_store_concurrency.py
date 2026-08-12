# -*- coding: utf-8 -*-
"""JSON 读改写事务不能在并发请求间丢失更新。"""

import threading
import time


def test_watch_status_concurrent_updates_preserve_both_works(tmp_path, monkeypatch):
    from app.library import watch_status

    path = tmp_path / "watch_status.json"
    monkeypatch.setattr(watch_status, "_status_path", lambda: path)
    original_load = watch_status.load_watch_statuses
    first_has_loaded = threading.Event()
    release_first = threading.Event()

    def controlled_load():
        items = original_load()
        if threading.current_thread().name == "first-writer":
            first_has_loaded.set()
            assert release_first.wait(timeout=2)
        return items

    monkeypatch.setattr(watch_status, "load_watch_statuses", controlled_load)
    first = threading.Thread(
        name="first-writer",
        target=lambda: watch_status.set_watch_status("work-a", "watching"),
    )
    second = threading.Thread(
        name="second-writer",
        target=lambda: watch_status.set_watch_status("work-b", "watched"),
    )

    first.start()
    assert first_has_loaded.wait(timeout=2)
    second.start()
    time.sleep(0.05)
    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert set(original_load()) == {"work-a", "work-b"}
