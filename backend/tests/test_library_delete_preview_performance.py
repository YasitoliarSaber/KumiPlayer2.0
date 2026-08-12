# -*- coding: utf-8 -*-
"""整库清理预览不应按文件重复解析配置路径。"""

from types import SimpleNamespace


def test_library_clear_preview_resolves_source_roots_once(tmp_path, monkeypatch):
    from app.library import delete

    mirror_root = tmp_path / "mirror"
    work_dir = mirror_root / "baidu" / "demo"
    work_dir.mkdir(parents=True)
    for index in range(3):
        (work_dir / f"S01E{index + 1:02}.strm").write_text("demo", encoding="utf-8")

    config_calls = 0

    def fake_load_config():
        nonlocal config_calls
        config_calls += 1
        return SimpleNamespace(
            pan115_root=str(tmp_path / "pan115"),
            baidu_root=str(tmp_path / "baidu"),
            local_root=str(tmp_path / "local"),
        )

    monkeypatch.setattr(delete, "get_mirror_root", lambda: mirror_root)
    monkeypatch.setattr("app.core.config.load_config", fake_load_config)

    preview = delete.build_library_clear_preview("all")

    assert len(preview.files) == 3
    assert preview.blocked is False
    assert config_calls == 1
