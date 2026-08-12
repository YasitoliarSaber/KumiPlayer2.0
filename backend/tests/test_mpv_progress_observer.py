import json
from itertools import islice
from unittest.mock import patch


class FakePipe:
    def __init__(self, messages: list[dict]):
        self._lines = [json.dumps(message).encode("utf-8") + b"\n" for message in messages]
        self.writes: list[bytes] = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.closed = True

    def write(self, payload: bytes):
        self.writes.append(payload)

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


def test_observer_uses_one_connection_and_forces_checkpoint_after_seek():
    from app.playback.mpv_ipc import MpvProgressEvent, observe_mpv_progress

    pipe = FakePipe([
        {"request_id": 101, "error": "success"},
        {"request_id": 102, "error": "success"},
        {"request_id": 103, "error": "success"},
        {"request_id": 104, "error": "success"},
        {"request_id": 105, "error": "success"},
        {"event": "property-change", "id": 1, "name": "playlist-pos", "data": 0},
        {"event": "property-change", "id": 5, "name": "path", "data": "video.mkv"},
        {"event": "property-change", "id": 2, "name": "duration", "data": 1000.0},
        {"event": "property-change", "id": 3, "name": "time-pos", "data": 120.0},
        {"event": "seek"},
        {"event": "property-change", "id": 3, "name": "time-pos", "data": 450.0},
    ])

    with patch("app.playback.mpv_ipc._open_ipc", return_value=pipe) as mock_open:
        events = list(islice(observe_mpv_progress("ipc", timeout=0.25), 2))

    assert events == [
        MpvProgressEvent(position=120.0, duration=1000.0, playlist_position=0, media_path="video.mkv"),
        MpvProgressEvent(
            position=450.0,
            duration=1000.0,
            playlist_position=0,
            force_checkpoint=True,
            media_path="video.mkv",
        ),
    ]
    mock_open.assert_called_once_with("ipc", 0.25)
    commands = [json.loads(payload.decode("utf-8"))["command"] for payload in pipe.writes]
    assert commands == [
        ["observe_property", 1, "playlist-pos"],
        ["observe_property", 5, "path"],
        ["observe_property", 2, "duration"],
        ["observe_property", 3, "time-pos"],
        ["observe_property", 4, "pause"],
    ]
    assert pipe.closed is True


def test_observer_forces_checkpoint_when_playback_pauses():
    from app.playback.mpv_ipc import MpvProgressEvent, observe_mpv_progress

    pipe = FakePipe([
        {"request_id": 101, "error": "success"},
        {"request_id": 102, "error": "success"},
        {"request_id": 103, "error": "success"},
        {"request_id": 104, "error": "success"},
        {"request_id": 105, "error": "success"},
        {"event": "property-change", "id": 1, "name": "playlist-pos", "data": 0},
        {"event": "property-change", "id": 5, "name": "path", "data": "video.mkv"},
        {"event": "property-change", "id": 2, "name": "duration", "data": 1000.0},
        {"event": "property-change", "id": 3, "name": "time-pos", "data": 300.0},
        {"event": "property-change", "id": 4, "name": "pause", "data": True},
    ])

    with patch("app.playback.mpv_ipc._open_ipc", return_value=pipe):
        events = list(islice(observe_mpv_progress("ipc"), 2))

    assert events[-1] == MpvProgressEvent(
        position=300.0,
        duration=1000.0,
        playlist_position=0,
        force_checkpoint=True,
        media_path="video.mkv",
    )
