import threading
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from app.media_v4.assets import thumbnails


def test_read_only_lookup_never_encodes_or_creates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnails, "get_cache_dir", lambda: tmp_path / "cache")
    source = tmp_path / "poster.jpg"
    Image.new("RGB", (780, 1170)).save(source)
    encode = Mock(side_effect=AssertionError("read path encoded an image"))
    monkeypatch.setattr(thumbnails, "_generate", encode)
    assert thumbnails.find_thumbnail(source, 384) is None
    assert not (tmp_path / "cache").exists()
    encode.assert_not_called()


def test_artwork_preparation_generates_fixed_sizes_without_touching_source(tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnails, "get_cache_dir", lambda: tmp_path / "cache")
    source = tmp_path / "poster.jpg"
    Image.new("RGB", (780, 1170)).save(source)
    before = source.read_bytes(), source.stat().st_mtime_ns
    thumbnails.prepare_artwork(source)
    for width in (256, 384, 512):
        result = thumbnails.find_thumbnail(source, width)
        assert result is not None
        with Image.open(result) as image:
            assert image.width == width
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_queue_deduplicates_promotes_and_bounds_backlog():
    from app.media_v4.assets.preparation import ThumbnailPreparer

    worker = ThumbnailPreparer(capacity=2)
    assert worker.enqueue(Path("a.jpg"), 384, priority=10)
    assert worker.enqueue(Path("b.jpg"), 384, priority=10)
    assert worker.enqueue(Path("a.jpg"), 384, priority=0)
    assert len(worker._pending) == 2
    assert worker.enqueue(Path("visible.jpg"), 384, priority=0)
    assert len(worker._pending) == 2
    assert worker._pending[(Path("visible.jpg"), 384)] == 0
    worker.stop()


def test_background_preparation_finishes_and_stops(tmp_path, monkeypatch):
    from app.media_v4.assets import preparation

    source = tmp_path / "poster.jpg"
    Image.new("RGB", (780, 1170)).save(source)
    monkeypatch.setattr(thumbnails, "get_cache_dir", lambda: tmp_path / "cache")
    finished = threading.Event()
    original = preparation.get_or_create_thumbnail

    def generate(path, width):
        assert threading.current_thread().name == "artwork-thumbnails"
        result = original(path, width)
        finished.set()
        return result

    monkeypatch.setattr(preparation, "get_or_create_thumbnail", generate)
    worker = preparation.ThumbnailPreparer()
    worker.start()
    try:
        assert worker.status(source, 384) == "pending"
        assert finished.wait(timeout=5)
        assert worker.status(source, 384) == "ready"
    finally:
        worker.stop()
    assert worker._thread is None
    assert not worker.enqueue(source, 512)


def test_transient_failure_can_retry_after_cooldown(tmp_path, monkeypatch):
    from app.media_v4.assets import preparation

    source = tmp_path / "poster.jpg"
    Image.new("RGB", (780, 1170)).save(source)
    monkeypatch.setattr(thumbnails, "get_cache_dir", lambda: tmp_path / "cache")
    clock = [100.0]
    monkeypatch.setattr(preparation.time, "monotonic", lambda: clock[0])
    worker = preparation.ThumbnailPreparer()
    key = str(thumbnails.cache_path(source, 384))
    worker._failed[key] = 100.0
    assert worker.status(source, 384) == "retry"
    assert not worker._pending
    clock[0] = 131.0
    assert worker.status(source, 384) == "pending"
    assert worker._pending == {(source, 384): 0}
    worker.stop()
