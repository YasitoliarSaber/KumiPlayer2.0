"""V4 reset manifest safety contract。"""

from __future__ import annotations


def test_preview_is_non_destructive_and_apply_keeps_credentials_and_runtime_state(tmp_path, monkeypatch):
    from app.maintenance import reset_backend_state_v4 as reset

    data_dir = tmp_path / "data"
    mirror_dir = tmp_path / "managed-mirror"
    data_dir.mkdir(exist_ok=True)
    mirror_dir.mkdir(exist_ok=True)
    (data_dir / "config.json").write_text("{}", encoding="utf-8")
    (data_dir / "bangumi_account.json").write_text("{}", encoding="utf-8")
    (data_dir / "logs").mkdir()
    (data_dir / "kumiplayer.db").write_bytes(b"db")
    (data_dir / "kumiplayer.db-wal").write_bytes(b"wal")
    (data_dir / "library_snapshots").mkdir()
    (data_dir / "library_snapshots" / "old.json").write_text("{}", encoding="utf-8")
    (mirror_dir / "Show").mkdir()
    (mirror_dir / "Show" / "S01E01.strm").write_text("local://show", encoding="utf-8")

    monkeypatch.setattr(reset, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(reset, "get_mirror_root", lambda: mirror_dir)
    monkeypatch.setattr(reset, "_configured_source_roots", lambda: [])

    preview = reset.preview_reset()
    assert str(data_dir / "kumiplayer.db-wal") in preview["targets"]
    assert (data_dir / "config.json").exists()
    assert (mirror_dir / "Show" / "S01E01.strm").exists()

    result = reset.apply_reset()

    assert str(data_dir / "kumiplayer.db") in result["removed"]
    assert str(mirror_dir) in result["removed"]
    assert (data_dir / "config.json").exists()
    assert (data_dir / "bangumi_account.json").exists()
    assert (data_dir / "logs").exists()
    assert not (data_dir / "library_snapshots").exists()
    assert not mirror_dir.exists()


def test_source_root_and_symlink_targets_are_rejected(tmp_path, monkeypatch):
    import pytest

    from app.maintenance import reset_backend_state_v4 as reset

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    source_root = tmp_path / "source"
    source_root.mkdir(exist_ok=True)
    (source_root / "video.mkv").write_bytes(b"source")
    symlink_target = data_dir / "mirror"
    try:
        symlink_target.symlink_to(source_root, target_is_directory=True)
        mirror_root = symlink_target
    except OSError:
        # Windows CI without SeCreateSymbolicLinkPrivilege still exercises the
        # same source-root guard through an explicitly configured mirror root.
        mirror_root = source_root

    monkeypatch.setattr(reset, "get_data_dir", lambda: data_dir)
    monkeypatch.setattr(reset, "get_mirror_root", lambda: mirror_root)
    monkeypatch.setattr(reset, "_configured_source_roots", lambda: [source_root.resolve()])

    with pytest.raises(reset.ResetProtectionError):
        reset.apply_reset()
    assert (source_root / "video.mkv").exists()
