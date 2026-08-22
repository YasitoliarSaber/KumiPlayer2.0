# -*- coding: utf-8 -*-
"""播放进度非有限值（NaN/inf）防护回归。

mpv 回退采样或异常数据路径可能产生 NaN：min/max 会原样传播并写出
非法 JSON 字面量 NaN，completed 判定也会被 NaN 比较误放行（把从未
看过的剧集标记为已看完）。本文件锁定 save_progress 的净化行为。
"""

import json
import math


def _no_nan_constant(value):
    raise AssertionError(f"progress.json 出现非法 JSON 字面量: {value}")


def test_save_progress_sanitizes_nan_position(tmp_path, monkeypatch):
    from app.playback import progress as progress_mod

    path = tmp_path / "progress.json"
    monkeypatch.setattr(progress_mod, "progress_path", lambda: path)

    item = progress_mod.save_progress(
        "w-nan", "ep-1", float("nan"), 100.0, sync_bangumi=False,
    )

    assert math.isfinite(item.ratio)
    assert item.ratio == 0.0
    assert item.completed is False
    # 落盘内容必须是严格合法 JSON（json.loads 默认接受 NaN，需禁掉）
    data = json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_nan_constant)
    assert data[0]["ratio"] == 0.0
    assert data[0]["completed"] is False


def test_save_progress_sanitizes_nan_duration(tmp_path, monkeypatch):
    from app.playback import progress as progress_mod

    path = tmp_path / "progress.json"
    monkeypatch.setattr(progress_mod, "progress_path", lambda: path)

    item = progress_mod.save_progress(
        "w-nan", "ep-2", 42.0, float("nan"), sync_bangumi=False,
    )

    assert item.duration == 0.0
    assert item.ratio == 0.0
    assert item.completed is False
    json.loads(path.read_text(encoding="utf-8"), parse_constant=_no_nan_constant)


def test_save_progress_normal_values_unchanged(tmp_path, monkeypatch):
    from app.playback import progress as progress_mod

    path = tmp_path / "progress.json"
    monkeypatch.setattr(progress_mod, "progress_path", lambda: path)

    item = progress_mod.save_progress(
        "w-ok", "ep-1", 95.0, 100.0, sync_bangumi=False,
    )

    assert item.ratio == 0.95
    assert item.completed is True
