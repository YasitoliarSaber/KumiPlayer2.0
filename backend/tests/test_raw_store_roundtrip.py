# -*- coding: utf-8 -*-
"""RawSnapshot 持久化往返回归。

保存（asdict 全字段）→ 加载（_load_from_path 显式字段恢复）必须对称；
历史上 root_container 序列化了但加载时被丢弃，导致 OpenList 选中目录
作为系列名的回退在快照重载后失效。本文件锁定该对称性。
"""

from app.raw.models import RawFile, RawSnapshot
from app.raw import store


def _sample(snapshot_id: str) -> RawSnapshot:
    return RawSnapshot(
        snapshot_id=snapshot_id,
        source="openlist",
        provider_id="pan115",
        ingest_method="openlist_api",
        source_route_id="route-1",
        source_root="K:\\115",
        root_container="飞跃巅峰 内封中字",
        import_family="anime",
        import_scope="",
        created_at="2026-08-22T12:00:00+08:00",
        input_file="",
        file_count=1,
        video_count=1,
        files=[
            RawFile(
                id="f1",
                snapshot_id=snapshot_id,
                source="openlist",
                source_root="K:\\115",
                relative_path="S1/01.mkv",
                real_path="K:\\115\\飞跃巅峰 内封中字\\S1\\01.mkv",
                name="01.mkv",
                stem="01",
                ext=".mkv",
                is_file=True,
            ),
        ],
    )


def test_snapshot_roundtrip_preserves_root_container(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_get_snapshots_dir", lambda: tmp_path)
    snapshot = _sample("snap-roundtrip-1")

    store.save_raw_snapshot(snapshot, update_latest=False)
    loaded = store.load_raw_snapshot("snap-roundtrip-1")

    assert loaded is not None
    assert loaded.root_container == "飞跃巅峰 内封中字"
    assert loaded.provider_id == "pan115"
    assert loaded.source_route_id == "route-1"
    assert loaded.files[0].real_path.endswith("01.mkv")


def test_latest_snapshot_fallback_roundtrip_preserves_root_container(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_get_snapshots_dir", lambda: tmp_path)
    snapshot = _sample("snap-roundtrip-2")

    # update_latest=False：latest 文件不存在，走全目录 glob 回退路径
    store.save_raw_snapshot(snapshot, update_latest=False)
    loaded = store.load_latest_raw_snapshot("openlist")

    assert loaded is not None
    assert loaded.snapshot_id == "snap-roundtrip-2"
    assert loaded.root_container == "飞跃巅峰 内封中字"
