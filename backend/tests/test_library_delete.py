# -*- coding: utf-8 -*-
"""手动删除测试"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_DATA_DIR = Path(__file__).parent.parent.parent / "data"


def _cleanup():
    if _DATA_DIR.exists():
        shutil.rmtree(_DATA_DIR)


def _setup_library():
    """创建测试用 LibraryIndex 和 .strm 文件"""
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    mirror_root = _DATA_DIR / "mirror" / "115" / "CLANNAD" / "Season 1"
    mirror_root.mkdir(parents=True, exist_ok=True)

    work = WorkIndex(
        work_id="w1",
        title="CLANNAD",
        source="pan115",
        media_type="tv",
        episodes=[
            EpisodeIndex(
                episode_id="ep1", work_id="w1",
                season_number=1, episode_number=1,
                title="Ep1", group_type="season",
                strm_path=str(mirror_root / "CLANNAD.S01E01.strm"),
            ),
            EpisodeIndex(
                episode_id="ep2", work_id="w1",
                season_number=1, episode_number=2,
                title="Ep2", group_type="season",
                strm_path=str(mirror_root / "CLANNAD.S01E02.strm"),
            ),
        ],
        seasons=[],
    )
    index = LibraryIndex(works=[work])
    save_library_index(index)

    # 创建 .strm 文件
    for ep in work.episodes:
        p = Path(ep.strm_path)
        p.write_text("fake_video.mkv\n", encoding="utf-8")

    # 创建 nfo 和 poster
    (mirror_root / "tvshow.nfo").write_text("<tvshow></tvshow>", encoding="utf-8")
    (mirror_root / "poster.jpg").write_bytes(b"fake_jpg")

    return work


def test_preview_by_episode_id():
    """按 episode_id 生成删除预览"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview
        preview = build_delete_preview(work_id="w1", episode_ids=["ep1"])
        assert preview.blocked is False
        strm_files = [f for f in preview.files if f.kind == "strm"]
        assert len(strm_files) == 1
        assert "S01E01" in strm_files[0].path
    finally:
        _cleanup()


def test_preview_by_work_id():
    """按 work_id 生成 work 下所有 strm 删除预览"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview
        preview = build_delete_preview(work_id="w1")
        assert preview.blocked is False
        strm_files = [f for f in preview.files if f.kind == "strm"]
        assert len(strm_files) == 2
    finally:
        _cleanup()


def test_preview_with_assets():
    """delete_assets=true 同时列出 NFO/图片"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview
        preview = build_delete_preview(work_id="w1", episode_ids=["ep1"], delete_assets=True)
        assert preview.blocked is False
        kinds = {f.kind for f in preview.files}
        assert "strm" in kinds
        # nfo 和 poster 在同目录下也会被列出
        assert "nfo" in kinds or "poster" in kinds
    finally:
        _cleanup()


def test_preview_nonexistent_work():
    """不存在的 work_id 返回 blocked"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview
        preview = build_delete_preview(work_id="nonexistent")
        assert preview.blocked is True
        assert any("不存在" in w for w in preview.warnings)
    finally:
        _cleanup()


def test_preview_invalid_episode_blocks():
    """不存在的 episode_id 返回 blocked，confirm 不应执行"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview
        preview = build_delete_preview(work_id="w1", episode_ids=["missing_ep"])
        assert preview.blocked is True
        assert any("episode_id 不存在" in w for w in preview.warnings)
    finally:
        _cleanup()


def test_preview_rejects_external_strm_path():
    """前端传 LibraryIndex 外路径时 blocked，不进入删除列表"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview
        preview = build_delete_preview(work_id="w1", strm_paths=["C:\\Windows\\notepad.exe"])
        assert preview.blocked is True
        assert any("不属于 work" in w for w in preview.warnings)
        assert preview.files == []
    finally:
        _cleanup()


def test_preview_no_library_index():
    """无 LibraryIndex 返回 blocked"""
    _cleanup()
    try:
        from app.library.delete import build_delete_preview
        preview = build_delete_preview(work_id="w1")
        assert preview.blocked is True
    finally:
        _cleanup()


def test_execute_delete():
    """confirm 删除 allowed 文件"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview, execute_delete
        preview = build_delete_preview(work_id="w1", episode_ids=["ep1"])
        assert not preview.blocked

        # 确认文件存在
        for f in preview.files:
            if f.allowed:
                assert Path(f.path).exists()

        result = execute_delete(preview)
        assert result.status == "succeeded"
        assert result.library_rescanned is True
        assert len(result.deleted) > 0

        # 确认文件已删除
        for path in result.deleted:
            assert not Path(path).exists()
    finally:
        _cleanup()


def test_execute_delete_revalidates_source_roots_at_confirmation(monkeypatch):
    """预览后来源配置变化时，确认阶段必须保护新标记的真实来源路径。"""
    from types import SimpleNamespace

    _cleanup()
    configured_source = [""]
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: SimpleNamespace(
            pan115_root=configured_source[0],
            baidu_root="",
            local_root="",
            mirror_dir=str(_DATA_DIR / "mirror"),
        ),
    )
    try:
        work = _setup_library()
        from app.library.delete import build_delete_preview, execute_delete

        preview = build_delete_preview(work_id="w1", episode_ids=["ep1"])
        assert preview.blocked is False
        protected_file = Path(work.episodes[0].strm_path)
        configured_source[0] = str(protected_file.parent)

        result = execute_delete(preview)

        assert result.status == "failed"
        assert protected_file.exists()
    finally:
        _cleanup()


def test_execute_delete_empty_dirs():
    """删除空目录"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview, execute_delete
        preview = build_delete_preview(work_id="w1", remove_empty_dirs=True)
        result = execute_delete(preview)
        # 删除所有文件后，目录应该变空并被删除
        assert result.status == "succeeded"
        assert len(result.empty_dirs_removed) > 0
    finally:
        _cleanup()


def test_execute_blocked_preview_refused():
    """blocked preview 不会执行删除"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview, execute_delete
        preview = build_delete_preview(work_id="w1", strm_paths=["C:\\Windows\\notepad.exe"])
        result = execute_delete(preview)
        assert result.status == "failed"
        assert result.deleted == []
        assert result.library_rescanned is False
    finally:
        _cleanup()


def test_library_clear_preview_only_mirror_files():
    """整库删除预览只列出 mirror root 内文件"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_library_clear_preview

        preview = build_library_clear_preview()
        assert preview.blocked is False
        assert preview.scope == "library"
        assert preview.work_id == "__library__"
        assert len(preview.files) >= 4
        assert all(f.allowed for f in preview.files)
        assert all(str(_DATA_DIR / "mirror") in f.path for f in preview.files)
    finally:
        _cleanup()


def test_execute_library_clear_deletes_generated_library():
    """整库删除会删除预览中的生成文件并重建空 LibraryIndex。"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_library_clear_preview, execute_delete
        from app.library.store import load_library_index

        preview = build_library_clear_preview()
        result = execute_delete(preview)

        assert result.status == "succeeded"
        assert result.library_rescanned is True
        assert any(Path(path).suffix == ".strm" for path in result.deleted)
        assert not any((_DATA_DIR / "mirror").rglob("*.*"))

        index = load_library_index()
        assert index is not None
        assert index.works == []
    finally:
        _cleanup()


def test_library_clear_all_removes_history_progress_and_favorites(tmp_path, monkeypatch):
    """整库删除后不能留下会生成空卡片的观看历史、进度或收藏状态。"""
    data_dir = tmp_path / "data"
    mirror_root = data_dir / "mirror"
    strm_path = mirror_root / "115" / "待删除作品" / "Season 1" / "S01E01.strm"
    strm_path.parent.mkdir(parents=True)
    strm_path.write_text("H:/source/待删除作品/S01E01.mkv", encoding="utf-8")
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror_root))

    from app.library.delete import build_library_clear_preview, execute_delete
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import save_library_index
    from app.library.watch_status import load_watch_statuses, set_watch_status
    from app.playback.history import build_history_item, load_history, save_history
    from app.playback.progress import PlaybackProgressItem, _write_progress, load_progress

    save_library_index(LibraryIndex(works=[WorkIndex(
        work_id="delete-all-work",
        title="待删除作品",
        source="pan115",
        episodes=[EpisodeIndex(
            episode_id="delete-all-episode",
            work_id="delete-all-work",
            source="pan115",
            strm_path=str(strm_path),
        )],
    )]))
    save_history(build_history_item(
        "delete-all-work", "待删除作品", "delete-all-episode", "第 1 集",
        "pan115", "tv", "season", 1, 1, str(strm_path),
    ))
    _write_progress([PlaybackProgressItem(
        work_id="delete-all-work",
        episode_id="delete-all-episode",
    )])
    set_watch_status("delete-all-work", "watching", favorite=True)

    result = execute_delete(build_library_clear_preview("all"))

    assert result.status == "succeeded"
    assert load_history() == []
    assert load_progress() == []
    assert load_watch_statuses() == {}


def test_library_source_clear_only_removes_state_for_works_that_disappear(tmp_path, monkeypatch):
    """按来源删除时保留仍有其他来源贡献的卡片状态，只清最终消失的作品。"""
    data_dir = tmp_path / "data"
    mirror_root = data_dir / "mirror"
    (mirror_root / "115").mkdir(parents=True)
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KUMIPLAYER_MIRROR_DIR", str(mirror_root))

    from app.library.delete import build_library_clear_preview, execute_delete
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import load_library_index, save_library_index
    from app.library.watch_status import load_watch_statuses, set_watch_status

    save_library_index(LibraryIndex(works=[
        WorkIndex(
            work_id="pan-only",
            title="仅 115",
            source="pan115",
            sources=["pan115"],
            episodes=[EpisodeIndex(episode_id="pan-only-ep", source="pan115")],
        ),
        WorkIndex(
            work_id="mixed-work",
            title="混合来源",
            source="pan115",
            sources=["pan115", "local"],
            import_scope="seasonal",
            episodes=[
                EpisodeIndex(episode_id="mixed-pan", source="pan115"),
                EpisodeIndex(episode_id="mixed-local", source="local"),
            ],
        ),
        WorkIndex(
            work_id="local-only",
            title="仅本地",
            source="local",
            sources=["local"],
            episodes=[EpisodeIndex(episode_id="local-only-ep", source="local")],
        ),
    ]))
    for work_id in ("pan-only", "mixed-work", "local-only"):
        set_watch_status(work_id, "watching", favorite=True)

    result = execute_delete(build_library_clear_preview("pan115"))

    assert result.status == "succeeded"
    assert {work.work_id for work in load_library_index().works} == {"mixed-work", "local-only"}
    assert set(load_watch_statuses()) == {"mixed-work", "local-only"}


def test_library_clear_only_deletes_files_captured_by_preview(tmp_path, monkeypatch):
    """确认阶段不能递归删除生成预览后才出现的文件。"""
    data_dir = tmp_path / "data"
    mirror = tmp_path / "mirror"
    generated = mirror / "115" / "Work" / "episode.strm"
    generated.parent.mkdir(parents=True)
    generated.write_text("H:\\video.mkv", encoding="utf-8")
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("all")
    late_file = mirror / "late-file.txt"
    late_file.write_text("must survive", encoding="utf-8")
    result = execute_delete(preview)

    assert result.status == "succeeded"
    assert not generated.exists()
    assert late_file.exists()


def test_library_clear_rejects_mirror_root_changed_after_preview(tmp_path, monkeypatch):
    """旧预览不能删除确认时新配置的镜像目录，更不能删除真实源目录。"""
    from types import SimpleNamespace

    data_dir = tmp_path / "data"
    mirror = tmp_path / "mirror"
    generated = mirror / "115" / "Work" / "episode.strm"
    generated.parent.mkdir(parents=True)
    generated.write_text("H:\\video.mkv", encoding="utf-8")
    source_root = tmp_path / "mounted-source"
    source_file = source_root / "real-video.mkv"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"real-video")
    selected_mirror = [mirror]
    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: selected_mirror[0])
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: SimpleNamespace(pan115_root=str(source_root), baidu_root="", local_root=""),
    )

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("all")
    assert preview.blocked is False
    selected_mirror[0] = source_root
    result = execute_delete(preview)

    assert result.status == "failed"
    assert source_file.exists()
    assert generated.exists()


def test_library_clear_preview_allows_missing_mirror_root():
    """mirror root 不存在时也允许确认，用于清理可重建缓存"""
    _cleanup()
    try:
        from app.library.delete import build_library_clear_preview
        preview = build_library_clear_preview()
        assert preview.scope == "library"
        assert preview.blocked is False
    finally:
        _cleanup()


def test_source_path_check_tolerates_offline_drive(monkeypatch):
    """源盘不可访问时，删除安全检查不能让预览接口 500"""
    from types import SimpleNamespace
    from app.library.delete import _is_source_path

    def fake_resolve(self, strict=False):
        if str(self).lower().startswith("h:\\115open"):
            raise OSError("offline drive")
        return self.absolute()

    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: SimpleNamespace(
            pan115_root="H:\\115open",
            baidu_root="",
            local_root="",
        ),
    )
    monkeypatch.setattr(Path, "resolve", fake_resolve)

    assert _is_source_path(Path("D:\\01_Software\\KumiPlayer2.0\\data\\mirror\\115\\a.strm")) is False


def test_library_clear_preview_can_scope_to_source(tmp_path, monkeypatch):
    """分来源清空预览只列出对应 namespace 下的生成文件"""
    mirror = tmp_path / "mirror"
    pan_dir = mirror / "115" / "Work"
    baidu_dir = mirror / "baidu" / "Work"
    pan_dir.mkdir(parents=True)
    baidu_dir.mkdir(parents=True)
    (pan_dir / "a.strm").write_text("H:\\a.mkv", encoding="utf-8")
    (baidu_dir / "b.strm").write_text("B:\\b.mkv", encoding="utf-8")

    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)

    from app.library.delete import build_library_clear_preview
    preview = build_library_clear_preview("pan115")

    assert preview.source == "pan115"
    assert preview.blocked is False
    assert len(preview.files) == 1
    assert "\\115\\" in preview.files[0].path or "/115/" in preview.files[0].path


def test_library_clear_preview_supports_openlist_namespace(tmp_path, monkeypatch):
    """OpenList 作为正式来源：清空预览按 openlist 命名空间枚举生成内容。"""
    mirror = tmp_path / "mirror"
    openlist_dir = mirror / "openlist" / "冰菓" / "Season 1"
    openlist_dir.mkdir(parents=True)
    (openlist_dir / "S01E01.strm").write_text("Q:\\夸克\\动画\\冰菓\\S01E01.mkv", encoding="utf-8")
    (openlist_dir / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")

    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)

    from app.library.delete import build_library_clear_preview

    preview = build_library_clear_preview("openlist")
    assert preview.source == "openlist"
    assert preview.blocked is False
    assert len(preview.files) == 2
    assert all("openlist" in f.path for f in preview.files)


def test_library_clear_openlist_never_touches_mount_root(tmp_path, monkeypatch):
    """清理 OpenList 镜像时，配置的本地挂载根（真实媒体）绝不能被删除。"""
    from types import SimpleNamespace

    mirror = tmp_path / "mirror"
    mount_root = tmp_path / "quark_mount"
    openlist_dir = mirror / "openlist" / "冰菓" / "Season 1"
    openlist_dir.mkdir(parents=True)
    (openlist_dir / "S01E01.strm").write_text("Q:\\quark_mount\\S01E01.mkv", encoding="utf-8")

    real_media = mount_root / "动画" / "冰菓"
    real_media.mkdir(parents=True)
    (real_media / "S01E01.mkv").write_bytes(b"real")

    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: SimpleNamespace(
            pan115_root="",
            baidu_root="",
            local_root="",
            openlist_mount_root=str(mount_root),
            mirror_dir=str(mirror),
        ),
    )

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    assert preview.blocked is False
    assert all("openlist" in f.path for f in preview.files)
    assert not any(str(mount_root) in f.path for f in preview.files)

    result = execute_delete(preview)
    assert result.status in {"succeeded", "partial_failed"}
    assert (real_media / "S01E01.mkv").exists()  # 真实媒体安然无恙
    assert not (openlist_dir / "S01E01.strm").exists()  # 镜像已清理


def test_library_clear_openlist_removes_scan_manifests(tmp_path, monkeypatch):
    """清理 OpenList 范围时，KumiPlayer 自有扫描清单一并移除。"""
    from types import SimpleNamespace

    data_dir = tmp_path / "data"
    mirror = data_dir / "mirror"
    manifest_dir = data_dir / "openlist_manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "scan_1.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("KUMIPLAYER_DATA_DIR", str(data_dir))
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: SimpleNamespace(
            pan115_root="",
            baidu_root="",
            local_root="",
            openlist_mount_root=str(tmp_path / "quark_mount"),
            mirror_dir=str(mirror),
        ),
    )

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("openlist")
    result = execute_delete(preview)
    assert result.status in {"succeeded", "partial_failed"}
    assert not (manifest_dir / "scan_1.json").exists()


def test_library_source_clear_preserves_other_sources_in_seasonal_card(tmp_path, monkeypatch):
    """清理单一来源时只剥离混合卡中的该来源贡献。"""
    monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: tmp_path / "cache")

    from app.library.delete import _count_library_works_for_clear, _remove_source_from_library_index
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import load_library_index, save_library_index

    mixed = WorkIndex(
        work_id="seasonal-mixed",
        source="local",
        sources=["pan115", "local"],
        import_scope="seasonal",
        episodes=[
            EpisodeIndex(episode_id="pan-1", source="pan115", season_number=1, episode_number=1),
            EpisodeIndex(episode_id="local-2", source="local", season_number=1, episode_number=2),
        ],
        source_locations={
            "pan115": {"episode_id": "pan-1", "strm_path": "D:/mirror/115/show/S01E01.strm"},
            "local": {"episode_id": "local-2", "strm_path": "D:/mirror/local/show/S01E02.strm"},
        },
        source_episode_counts={"pan115": 1, "local": 1},
    )
    pan_only = WorkIndex(
        work_id="pan-only",
        source="pan115",
        sources=["pan115"],
        episodes=[EpisodeIndex(episode_id="pan-only-1", source="pan115")],
    )
    save_library_index(LibraryIndex(
        works=[mixed, pan_only],
        source_summary={
            "pan115": {"work_count": 2, "episode_count": 2},
            "local": {"work_count": 1, "episode_count": 1},
        },
    ))

    assert _count_library_works_for_clear("pan115") == 2

    _remove_source_from_library_index("pan115")

    index = load_library_index()
    assert index is not None
    assert [work.work_id for work in index.works] == ["seasonal-mixed"]
    retained = index.works[0]
    assert retained.source == "local"
    assert retained.sources == ["local"]
    assert [episode.episode_id for episode in retained.episodes] == ["local-2"]
    assert set(retained.source_locations) == {"local"}
    assert retained.source_episode_counts == {"local": 1}
    assert "pan115" not in index.source_summary


def test_library_clear_removes_source_import_plans(tmp_path, monkeypatch):
    """清空某来源时必须清掉该来源旧 ImportPlan，避免刮削继续跑旧来源"""
    data_dir = tmp_path / "data"
    plans_dir = data_dir / "import_plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "pan115_latest.json").write_text('{"source":"pan115"}', encoding="utf-8")
    (plans_dir / "old-pan.json").write_text('{"source":"pan115"}', encoding="utf-8")
    (plans_dir / "local_latest.json").write_text('{"source":"local"}', encoding="utf-8")

    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)

    from app.library.delete import _clear_import_plans
    failures = _clear_import_plans("pan115")

    assert failures == []
    assert not (plans_dir / "pan115_latest.json").exists()
    assert not (plans_dir / "old-pan.json").exists()
    assert (plans_dir / "local_latest.json").exists()


def test_library_clear_removes_only_matching_source_media_presets(tmp_path, monkeypatch):
    """清空单一来源时同步删除该来源目录树卡片，保留其他来源。"""
    monkeypatch.setattr("app.media_presets.store.get_data_dir", lambda: tmp_path)

    from app.library.delete import _clear_media_presets
    from app.media_presets.models import MediaLibraryPreset
    from app.media_presets.store import list_presets, save_preset, version_archive_dir

    save_preset(MediaLibraryPreset(preset_id="pan", name="115 动画库", source="pan115"))
    save_preset(MediaLibraryPreset(preset_id="baidu", name="百度动画库", source="baidu"))
    (version_archive_dir("pan") / "tree.txt").write_text("tree", encoding="utf-8")

    deleted_ids, failures = _clear_media_presets("pan115")

    assert failures == []
    assert deleted_ids == ["pan"]
    assert [preset.preset_id for preset in list_presets()] == ["baidu"]
    assert not (tmp_path / "media_presets" / "pan").exists()


def test_library_clear_all_removes_every_media_preset(tmp_path, monkeypatch):
    """清空全部来源时同步删除所有目录树卡片和受控归档。"""
    monkeypatch.setattr("app.media_presets.store.get_data_dir", lambda: tmp_path)

    from app.library.delete import _clear_media_presets
    from app.media_presets.models import MediaLibraryPreset
    from app.media_presets.store import list_presets, save_preset

    save_preset(MediaLibraryPreset(preset_id="pan", source="pan115"))
    save_preset(MediaLibraryPreset(preset_id="baidu", source="baidu", import_scope="seasonal"))

    deleted_ids, failures = _clear_media_presets("all")

    assert failures == []
    assert deleted_ids == ["pan", "baidu"]
    assert list_presets() == []


def test_source_library_clear_preview_and_result_include_removed_media_presets(tmp_path, monkeypatch):
    """来源清理的预览与执行结果都应报告目录树导入档案数量。"""
    data_dir = tmp_path / "data"
    mirror = data_dir / "mirror"
    (mirror / "115").mkdir(parents=True)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.media_presets.store.get_data_dir", lambda: data_dir)

    from app.library.delete import build_library_clear_preview, execute_delete
    from app.media_presets.models import MediaLibraryPreset
    from app.media_presets.store import get_preset, save_preset

    save_preset(MediaLibraryPreset(preset_id="pan", source="pan115"))
    save_preset(MediaLibraryPreset(preset_id="baidu", source="baidu"))

    preview = build_library_clear_preview("pan115")
    assert preview.media_preset_count == 1

    result = execute_delete(preview)
    assert result.status == "succeeded"
    assert result.deleted_preset_ids == ["pan"]
    assert get_preset("pan") is None
    assert get_preset("baidu") is not None


def test_library_clear_preview_and_result_include_tracking_state(tmp_path, monkeypatch):
    """正式清空必须预告并报告追更绑定与扫描历史，避免清空后重新出现。"""
    data_dir = tmp_path / "data"
    mirror = data_dir / "mirror"
    (mirror / "baidu").mkdir(parents=True)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.library.delete._count_tracking_state_for_clear",
        lambda source: {"binding_count": 7, "scan_run_count": 42},
        raising=False,
    )
    monkeypatch.setattr(
        "app.library.delete._clear_tracking_state_for_clear",
        lambda source: {
            "binding_count": 7,
            "scan_run_count": 42,
            "cancelled_task_count": 1,
        },
        raising=False,
    )

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("baidu")
    assert preview.library_work_count == 0
    assert preview.tracking_binding_count == 7
    assert preview.tracking_scan_run_count == 42

    result = execute_delete(preview)
    assert result.status == "succeeded"
    assert result.deleted_library_work_count == 0
    assert result.deleted_tracking_binding_count == 7
    assert result.deleted_tracking_scan_run_count == 42
    assert result.cancelled_tracking_task_count == 1


def test_library_clear_reports_removed_work_count(tmp_path, monkeypatch):
    """清理确认必须使用后端索引数量，明确显示会删除多少部作品。"""
    data_dir = tmp_path / "data"
    mirror = data_dir / "mirror"
    (mirror / "115").mkdir(parents=True)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror)
    monkeypatch.setattr(
        "app.library.delete._clear_tracking_state_for_clear",
        lambda source: {"binding_count": 0, "scan_run_count": 0, "cancelled_task_count": 0},
    )

    from app.library.delete import build_library_clear_preview, execute_delete
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    save_library_index(LibraryIndex(works=[
        WorkIndex(work_id="pan-a", source="pan115"),
        WorkIndex(work_id="pan-b", source="pan115"),
        WorkIndex(work_id="baidu-a", source="baidu"),
    ]))

    preview = build_library_clear_preview("pan115")
    assert preview.library_work_count == 2

    result = execute_delete(preview)
    assert result.status == "succeeded"
    assert result.deleted_library_work_count == 2


def test_library_clear_aborts_when_tracking_cleanup_fails(tmp_path, monkeypatch):
    """追更状态无法清理时必须失效关闭，不能先清空媒体库制造分裂状态。"""
    data_dir = tmp_path / "data"
    mirror = data_dir / "mirror"
    source_dir = mirror / "115"
    source_dir.mkdir(parents=True)
    generated = source_dir / "keep-on-failure.strm"
    generated.write_text("H:\\source.mkv", encoding="utf-8")
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)
    monkeypatch.setattr("app.library.delete._count_tracking_state_for_clear", lambda source: {
        "binding_count": 1,
        "scan_run_count": 3,
    })
    monkeypatch.setattr(
        "app.library.delete._clear_tracking_state_for_clear",
        lambda source: (_ for _ in ()).throw(RuntimeError("tracking database locked")),
    )

    from app.library.delete import build_library_clear_preview, execute_delete

    preview = build_library_clear_preview("pan115")
    result = execute_delete(preview)

    assert result.status == "failed"
    assert result.library_rescanned is False
    assert generated.exists()
    assert [failure.path for failure in result.failed] == ["tracking_state"]


def test_persisted_library_clear_preview_keeps_tracking_counts(tmp_path, monkeypatch):
    """确认请求从磁盘恢复预览时不能丢失追更清理范围。"""
    monkeypatch.setattr("app.library.delete_store._get_delete_preview_dir", lambda: tmp_path)

    from app.library.delete import DeletePreview
    from app.library.delete_store import load_delete_preview, save_delete_preview

    preview = DeletePreview(
        preview_id="tracking-preview",
        source="baidu",
        scope="library",
        work_id="__library__",
        retained_work_ids=["kept-seasonal"],
        media_preset_count=2,
        library_work_count=3,
        tracking_binding_count=7,
        tracking_scan_run_count=42,
    )
    save_delete_preview(preview)

    loaded = load_delete_preview(preview.preview_id)
    assert loaded is not None
    assert loaded.retained_work_ids == ["kept-seasonal"]
    assert loaded.media_preset_count == 2
    assert loaded.library_work_count == 3
    assert loaded.tracking_binding_count == 7
    assert loaded.tracking_scan_run_count == 42


def test_library_clear_purges_source_error_logs(tmp_path, monkeypatch):
    """清空某来源生成库时，同步删除该来源错误日志，避免旧任务残留在新任务里"""
    data_dir = tmp_path / "data"
    mirror = data_dir / "mirror"
    pan_dir = mirror / "115" / "Broken Work"
    pan_dir.mkdir(parents=True)
    (pan_dir / "Broken.S01E01.strm").write_text("H:\\video.mkv", encoding="utf-8")

    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror)

    from app.core import error_log
    from app.library.delete import build_library_clear_preview, execute_delete

    log_dir = tmp_path / "error_log"
    log_dir.mkdir()
    monkeypatch.setattr(error_log, "_get_error_log_dir", lambda: log_dir)
    error_log.log_error("scrape", "scrape_failed", "115 失败", source="pan115")
    error_log.log_error("scrape", "scrape_failed", "百度失败", source="baidu")

    preview = build_library_clear_preview("pan115")
    result = execute_delete(preview)

    assert result.status == "succeeded"
    remaining = error_log.load_recent_errors()
    assert [entry["source"] for entry in remaining] == ["baidu"]


def test_deleted_work_clears_scrape_state(tmp_path, monkeypatch):
    """删除作品时应清理旧刮削映射，避免后续手动刮削出现重复卡片"""
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path)

    from app.library.delete import _clear_work_scrape_state
    from app.scrape.models import ScrapeMap, ScrapeMapItem
    from app.scrape.store import load_scrape_map, save_scrape_map
    from app.scrape.review_queue import ReviewQueue, ReviewQueueItem, load_review_queue, save_review_queue

    save_scrape_map(ScrapeMap(items=[
        ScrapeMapItem(scrape_target_id="target-old", work_id="work-old", source="local"),
        ScrapeMapItem(scrape_target_id="target-keep", work_id="work-keep", source="local"),
    ]))
    save_review_queue(ReviewQueue(items=[
        ReviewQueueItem(scrape_target_id="target-old", source="local", status="pending"),
        ReviewQueueItem(scrape_target_id="target-keep", source="local", status="pending"),
    ]))

    failures = _clear_work_scrape_state("local", "work-old")

    assert failures == []
    assert [item.scrape_target_id for item in load_scrape_map().items] == ["target-keep"]
    assert [item.scrape_target_id for item in load_review_queue().items] == ["target-keep"]


def test_delete_log():
    """删除后写 delete_log.json"""
    _cleanup()
    try:
        _setup_library()
        from app.library.delete import build_delete_preview, execute_delete
        from app.library.delete_store import save_delete_log, load_delete_log

        preview = build_delete_preview(work_id="w1", episode_ids=["ep1"])
        result = execute_delete(preview)
        save_delete_log(result, source="pan115", scope="episode")

        logs = load_delete_log()
        assert len(logs) == 1
        assert logs[0]["preview_id"] == preview.preview_id
        assert logs[0]["source"] == "pan115"
    finally:
        _cleanup()


def test_delete_work_removes_all_references_and_cannot_be_restored(tmp_path, monkeypatch):
    """整作删除要清理关联、观看状态和追更索引，并阻止旧计划重建同一卡片。"""
    data_dir = tmp_path / "data"
    mirror_root = data_dir / "mirror"
    source_root = tmp_path / "source"
    source_video = source_root / "作品" / "S01E01.mkv"
    source_video.parent.mkdir(parents=True)
    source_video.write_bytes(b"real-media-must-survive")
    strm_path = mirror_root / "local" / "作品" / "Season 1" / "S01E01.strm"
    strm_path.parent.mkdir(parents=True)
    strm_path.write_text(str(source_video), encoding="utf-8")

    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: data_dir / "cache")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror_root)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_cache_dir", lambda: data_dir / "cache")
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror_root)
    monkeypatch.setattr(
        "app.core.config.load_config",
        lambda: type("Config", (), {
            "pan115_root": "", "baidu_root": "", "local_root": str(source_root),
        })(),
    )

    from app.db import database
    database.close_connection()
    monkeypatch.setattr(database, "_db_path", data_dir / "state.db")
    database.init_db()

    from app.integrations.bangumi import (
        BangumiEpisodeSync,
        BangumiMatch,
        BangumiState,
        load_state,
        save_state,
    )
    from app.library.delete import build_delete_preview, execute_delete
    from app.library.models import EpisodeIndex, LibraryIndex, RelatedWork, WorkIndex
    from app.library.store import load_library_index, save_library_index
    from app.library.watch_status import load_watch_statuses, set_watch_status
    from app.playback.history import build_history_item, load_history, save_history
    from app.playback.progress import PlaybackProgressItem, _write_progress, load_progress
    from app.scrape.models import ScrapeMap, ScrapeMapItem
    from app.scrape.store import load_scrape_map, save_scrape_map
    from app.tracking.models import TrackingBinding
    from app.tracking.store import list_tracking_bindings, record_tracking_scan_run, upsert_tracking_binding

    target = WorkIndex(
        work_id="delete-me",
        title="待删除作品",
        source="local",
        media_type="movie",
        episodes=[EpisodeIndex(
            episode_id="delete-episode",
            work_id="delete-me",
            source="local",
            season_number=1,
            episode_number=1,
            group_type="season",
            strm_path=str(strm_path),
            nfo_path=str(strm_path.parent / "movie.nfo"),
        )],
    )
    keep = WorkIndex(
        work_id="keep-me",
        title="保留作品",
        source="local",
        related_works=[RelatedWork(work_id="delete-me", title="待删除作品")],
    )
    save_library_index(LibraryIndex(
        works=[target, keep],
        source_summary={"local": {"work_count": 2, "episode_count": 1, "warnings": []}},
    ))
    save_scrape_map(ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="raw-movie-target",
            work_id="raw-movie-id",
            source="local",
            media_type="movie",
            nfo_path=str(strm_path.parent / "movie.nfo"),
        ),
        ScrapeMapItem(
            scrape_target_id="keep-target",
            work_id="keep-me",
            source="local",
            nfo_path=str(mirror_root / "local" / "保留作品" / "tvshow.nfo"),
        ),
    ]))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO scrape_candidate_cache "
        "(cache_id, scrape_target_id, tmdb_id, cached_at) VALUES (?, ?, ?, ?)",
        ("delete-cache", "raw-movie-target", 123, "2026-07-23T00:00:00+08:00"),
    )
    conn.execute(
        "INSERT INTO scrape_review_queue (scrape_target_id, added_at) VALUES (?, ?)",
        ("raw-movie-target", "2026-07-23T00:00:00+08:00"),
    )
    conn.commit()
    save_history(build_history_item(
        "delete-me", "待删除作品", "delete-episode", "第一集",
        "local", "tv", "season", 1, 1, str(strm_path),
    ))
    _write_progress([PlaybackProgressItem(work_id="delete-me", episode_id="delete-episode")])
    set_watch_status("delete-me", "watched", favorite=True)
    save_state(BangumiState(
        matches=[BangumiMatch(work_id="delete-me", subject_id=123, season_number=1)],
        episode_sync=[BangumiEpisodeSync(
            local_episode_id="delete-episode", bangumi_episode_id=456,
            work_id="delete-me", season_number=1, subject_id=123,
        )],
    ))
    binding = upsert_tracking_binding(TrackingBinding(
        work_id="delete-me",
        display_title="待删除作品",
        logical_source="local",
        root_path=str(source_video.parent),
    ))
    record_tracking_scan_run(binding, {"status": "succeeded"})

    preview = build_delete_preview("delete-me")
    result = execute_delete(preview)

    assert result.status == "succeeded"
    assert result.deleted_library_work_count == 1
    assert source_video.read_bytes() == b"real-media-must-survive"
    index = load_library_index()
    assert [work.work_id for work in index.works] == ["keep-me"]
    assert index.works[0].related_works == []
    assert index.source_summary["local"]["work_count"] == 1
    assert index.source_summary["local"]["episode_count"] == 0
    assert [item.scrape_target_id for item in load_scrape_map().items] == ["keep-target"]
    assert load_history() == []
    assert load_progress() == []
    assert "delete-me" not in load_watch_statuses()
    assert load_state().matches == []
    assert load_state().episode_sync == []
    assert list_tracking_bindings() == []
    conn = database.get_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM scrape_candidate_cache WHERE scrape_target_id = ?",
        ("raw-movie-target",),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM scrape_review_queue WHERE scrape_target_id = ?",
        ("raw-movie-target",),
    ).fetchone()[0] == 0

    save_library_index(LibraryIndex(
        works=[target, keep],
        source_summary={"local": {"work_count": 2, "episode_count": 1, "warnings": []}},
    ))
    restored = load_library_index()
    assert [work.work_id for work in restored.works] == ["keep-me"]
    assert restored.source_summary["local"]["work_count"] == 1
    assert restored.source_summary["local"]["episode_count"] == 0
    database.close_connection()


def test_delete_preview_includes_hidden_duplicate_source_files(tmp_path, monkeypatch):
    """跨来源合卡隐藏的重复集也必须从来源计划补进整作删除预览。"""
    data_dir = tmp_path / "data"
    mirror_root = data_dir / "mirror"
    pan_strm = mirror_root / "115" / "作品" / "Season 1" / "S01E01.strm"
    local_strm = mirror_root / "local" / "作品" / "Season 1" / "S01E01.strm"
    reused_strm = mirror_root / "local" / "其他作品" / "S01E01.strm"
    for path in (pan_strm, local_strm, reused_strm):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("D:/source/video.mkv", encoding="utf-8")

    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror_root)
    monkeypatch.setattr("app.library.delete.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.library.delete.get_mirror_root", lambda: mirror_root)

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.delete import build_delete_preview
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import save_library_index

    def plan(source: str, plan_id: str, path: Path) -> ImportPlan:
        return ImportPlan(
            plan_id=plan_id,
            source=source,
            status="confirmed",
            import_scope="seasonal",
            items=[ImportPlanItem(
                id=f"{source}-episode",
                plan_id=plan_id,
                source=source,
                resource_type="video",
                action="generate_strm",
                work_id=f"{source}-raw",
                canonical_work_id="mixed-card",
                work_title="跨来源作品",
                media_type="tv",
                group_type="season",
                season_number=1,
                episode_number=1,
                target_strm_path=str(path),
                target_dir=str(path.parent),
            )],
        )

    save_import_plan(plan("pan115", "pan-plan", pan_strm))
    local_plan = plan("local", "local-plan", local_strm)
    local_plan.items.append(ImportPlanItem(
        id="historical-reused-path",
        plan_id="local-plan",
        source="local",
        resource_type="video",
        action="generate_strm",
        work_id="local-raw",
        canonical_work_id="mixed-card",
        media_type="tv",
        group_type="season",
        season_number=1,
        episode_number=2,
        target_strm_path=str(reused_strm),
        target_dir=str(reused_strm.parent),
    ))
    save_import_plan(local_plan)
    save_library_index(LibraryIndex(works=[WorkIndex(
        work_id="mixed-card",
        source="pan115",
        sources=["pan115", "local"],
        import_scope="seasonal",
        episodes=[EpisodeIndex(
            episode_id="pan115-episode",
            work_id="mixed-card",
            source="pan115",
            season_number=1,
            episode_number=1,
            strm_path=str(pan_strm),
        )],
        source_locations={
            "pan115": {"episode_id": "pan115-episode", "strm_path": str(pan_strm)},
            "local": {"episode_id": "local-episode", "strm_path": str(local_strm)},
        },
    ), WorkIndex(
        work_id="other-card",
        source="local",
        episodes=[EpisodeIndex(
            episode_id="other-episode",
            work_id="other-card",
            source="local",
            season_number=1,
            episode_number=1,
            strm_path=str(reused_strm),
        )],
    )]))

    preview = build_delete_preview("mixed-card")

    assert {
        Path(item.path) for item in preview.files if item.kind == "strm"
    } == {pan_strm, local_strm}


def test_scrape_state_failure_keeps_map_for_retry(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path)

    from app.library.delete import _clear_work_scrape_state
    from app.scrape.models import ScrapeMap, ScrapeMapItem
    from app.scrape.review_queue import ReviewQueue, ReviewQueueItem, save_review_queue
    from app.scrape.store import load_scrape_map, save_scrape_map

    save_scrape_map(ScrapeMap(items=[
        ScrapeMapItem(scrape_target_id="retry-target", work_id="raw-work", source="local"),
    ]))
    save_review_queue(ReviewQueue(items=[
        ReviewQueueItem(scrape_target_id="retry-target", source="local", status="pending"),
    ]))
    monkeypatch.setattr(
        "app.scrape.review_queue.save_review_queue",
        lambda *_: (_ for _ in ()).throw(OSError("queue locked")),
    )

    failures = _clear_work_scrape_state("local", "raw-work")

    assert [failure.path for failure in failures] == ["review_queue.json"]
    assert [item.scrape_target_id for item in load_scrape_map().items] == ["retry-target"]


def test_deleted_work_filter_preserves_hidden_source_episode_counts(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: data_dir)

    from app.library.deleted_works import mark_work_deleted
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import load_library_index, save_library_index

    hidden_mixed = WorkIndex(
        work_id="mixed-card",
        source="pan115",
        sources=["pan115", "local"],
        episodes=[EpisodeIndex(
            episode_id="pan-1",
            work_id="mixed-card",
            source="pan115",
            season_number=1,
            episode_number=1,
            strm_path="D:/mirror/115/show/S01E01.strm",
        )],
        source_locations={
            "pan115": {"episode_id": "pan-1", "strm_path": "D:/mirror/115/show/S01E01.strm"},
            "local": {"episode_id": "local-1", "strm_path": "D:/mirror/local/show/S01E01.strm"},
        },
        source_episode_counts={"pan115": 1, "local": 1},
    )
    deleted = WorkIndex(
        work_id="delete-card",
        source="pan115",
        sources=["pan115", "local"],
        episodes=[
            EpisodeIndex(episode_id="delete-pan", source="pan115", strm_path="D:/mirror/115/delete/1.strm"),
            EpisodeIndex(episode_id="delete-local", source="local", strm_path="D:/mirror/local/delete/2.strm"),
        ],
        source_locations={
            "pan115": {"episode_id": "delete-pan", "strm_path": "D:/mirror/115/delete/1.strm"},
            "local": {"episode_id": "delete-local", "strm_path": "D:/mirror/local/delete/2.strm"},
        },
        source_episode_counts={"pan115": 2, "local": 2},
    )
    mark_work_deleted("delete-card")
    save_library_index(LibraryIndex(
        works=[hidden_mixed, deleted],
        source_summary={
            "pan115": {"work_count": 1, "episode_count": 1, "strm_count": 1},
            "local": {"work_count": 2, "episode_count": 3, "strm_count": 3},
        },
    ))

    index = load_library_index()
    assert [work.work_id for work in index.works] == ["mixed-card"]
    assert index.source_summary["local"]["work_count"] == 1
    assert index.source_summary["local"]["episode_count"] == 1
    assert index.source_summary["local"]["strm_count"] == 1


def test_scrape_path_cleanup_does_not_remove_unrelated_same_directory_map(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: tmp_path)

    from app.library.delete import _clear_work_scrape_state
    from app.scrape.models import ScrapeMap, ScrapeMapItem
    from app.scrape.store import load_scrape_map, save_scrape_map

    shared_dir = tmp_path / "mirror" / "local" / "shared"
    target_nfo = shared_dir / "movie-a.nfo"
    save_scrape_map(ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="target-a",
            work_id="raw-a",
            source="local",
            nfo_path=str(target_nfo),
        ),
        ScrapeMapItem(
            scrape_target_id="target-b",
            work_id="raw-b",
            source="local",
            nfo_path=str(shared_dir / "movie-b.nfo"),
        ),
    ]))

    failures = _clear_work_scrape_state(
        "local",
        "library-a",
        target_paths={str(target_nfo), str(shared_dir)},
    )

    assert failures == []
    assert [item.scrape_target_id for item in load_scrape_map().items] == ["target-b"]


if __name__ == "__main__":
    tests = [
        test_preview_by_episode_id,
        test_preview_by_work_id,
        test_preview_with_assets,
        test_preview_nonexistent_work,
        test_preview_invalid_episode_blocks,
        test_preview_rejects_external_strm_path,
        test_preview_no_library_index,
        test_execute_delete,
        test_execute_delete_empty_dirs,
        test_execute_blocked_preview_refused,
        test_library_clear_preview_only_mirror_files,
        test_execute_library_clear_deletes_generated_library,
        test_delete_log,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
