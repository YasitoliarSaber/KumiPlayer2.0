# -*- coding: utf-8 -*-
"""播放服务测试（mock mpv）"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

_PROJECT_DATA_DIR = Path(__file__).parent.parent.parent / "data"
_DATA_DIR = Path(tempfile.gettempdir()) / "kumiplayer_tests" / "test_playback_service_data"
os.environ["KUMIPLAYER_DATA_DIR"] = str(_DATA_DIR)


def _cleanup():
    from app.playback.service import reset_playback_manager
    reset_playback_manager()
    if _DATA_DIR.exists():
        if _DATA_DIR.resolve() == _PROJECT_DATA_DIR.resolve():
            raise RuntimeError("Refusing to delete project data directory from tests")
        # play 测试启动的 daemon 线程（_wait_for_exit / _monitor_progress）可能在
        # 测试结束后仍在写进度文件；Windows 下文件句柄未释放会导致 rmtree 偶发
        # PermissionError。先给线程退出时间，再带重试删除共享临时目录。
        for _attempt in range(20):
            try:
                shutil.rmtree(_DATA_DIR)
                break
            except PermissionError:
                import time
                time.sleep(0.25)


def _setup_library_index():
    """创建测试用 LibraryIndex"""
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.store import save_library_index

    work = WorkIndex(
        work_id="w1",
        title="CLANNAD",
        source="pan115",
        media_type="tv",
        poster_path="",
        episodes=[
            EpisodeIndex(
                episode_id="ep1", work_id="w1",
                season_number=1, episode_number=1,
                title="在樱花飞散的坡道", group_type="season",
                strm_path=str(_DATA_DIR / "mirror" / "115" / "CLANNAD" / "Season 1" / "test_s01e01.strm"),
            ),
            EpisodeIndex(
                episode_id="ep2", work_id="w1",
                season_number=1, episode_number=2,
                title="Episode 2", group_type="season",
                strm_path=str(_DATA_DIR / "mirror" / "115" / "CLANNAD" / "Season 1" / "test_s01e02.strm"),
            ),
        ],
        seasons=[
            SeasonIndex(season_id="s1", work_id="w1", season_number=1, group_type="season", label="第1季", episode_count=2),
        ],
    )
    index = LibraryIndex(works=[work])
    save_library_index(index)

    # 创建 .strm 文件
    for ep in work.episodes:
        strm_path = Path(ep.strm_path)
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        strm_path.write_text(
            f"https://media.example.invalid/CLANNAD-S01E{ep.episode_number:02d}.mkv\n",
            encoding="utf-8",
        )


def test_read_strm_target_accepts_utf8_bom_and_uses_first_non_empty_line(tmp_path):
    from app.playback.service import _read_strm_target

    strm_path = tmp_path / "中文剧集.strm"
    strm_path.write_text(
        "\nD:\\媒体\\动画\\中文剧集.mkv\nignored\n",
        encoding="utf-8-sig",
    )

    assert _read_strm_target(str(strm_path)) == "D:\\媒体\\动画\\中文剧集.mkv"


def test_play_rejects_empty_strm_before_opening_mpv():
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.models import PlaybackRequest
        from app.playback.service import get_playback_manager

        episode = load_library_index().works[0].episodes[0]
        Path(episode.strm_path).write_text("\ufeff\n", encoding="utf-8")

        with patch("subprocess.Popen") as mock_popen:
            with pytest.raises(ValueError, match="没有有效的媒体路径"):
                get_playback_manager().play(
                    PlaybackRequest(work_id="w1", episode_id=episode.episode_id)
                )

        mock_popen.assert_not_called()
    finally:
        _cleanup()


def test_play_by_episode_id():
    """episode_id 定位播放"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest

        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
            mock_popen.return_value = mock_process

            manager = get_playback_manager()
            session = manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))
            assert session.status == "playing"
            assert session.pid == 12345
            assert session.episode_id == "ep1"
            args, kwargs = mock_popen.call_args
            # KumiPlayer 使用内置干净 MPV，不再回退到 "mpv"、系统 PATH 或用户 mpv_path
            assert args[0][0].endswith("mpv.exe")
            assert "third_party\\mpv\\runtime\\mpv.exe" in args[0][0]
            assert session.strm_path not in args[0]
            assert "https://media.example.invalid/CLANNAD-S01E01.mkv" in args[0]
            assert any(arg.startswith("--config-dir=") for arg in args[0])
            assert not any(arg.startswith("--no-config") for arg in args[0])
            assert not any(arg.startswith("--scripts-append=") for arg in args[0])
            assert any(arg.startswith("--input-ipc-server=") for arg in args[0])
            assert "--force-window=immediate" in args[0]
            assert "--focus-on=all" in args[0]
            assert "--window-minimized=no" in args[0]
            assert "--no-terminal" in args[0]
            assert "--start=0.000" in args[0]
            assert "--no-resume-playback" in args[0]
            assert "--autocreate-playlist=no" in args[0]
            assert "--title=CLANNAD - S01E01 - 在樱花飞散的坡道" in args[0]
            assert not any(arg.startswith("--title=KumiPlayer-") for arg in args[0])
            assert kwargs.get("shell") is not True
            wait_event.set()
            import time; time.sleep(0.3)
            manager.stop()
    finally:
        _cleanup()


def test_play_passes_scraped_chinese_title_to_mpv():
    """mpv 窗口和 media-title 应使用媒体库中文标题，而不是原始英文文件名。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index, save_library_index
        from app.playback.models import PlaybackRequest
        from app.playback.service import get_playback_manager

        index = load_library_index()
        work = index.works[0]
        work.title = "沉默魔女的秘密"
        work.episodes[0].title = "同步"
        save_library_index(index)

        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
            mock_popen.return_value = mock_process

            manager = get_playback_manager()
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))

            args, _kwargs = mock_popen.call_args
            mpv_args = args[0]
            assert "--force-media-title=沉默魔女的秘密 - S01E01 - 同步" in mpv_args
            assert "--title=沉默魔女的秘密 - S01E01 - 同步" in mpv_args
            assert not any(arg.startswith("--title=KumiPlayer-") for arg in mpv_args)

            wait_event.set()
            import time; time.sleep(0.3)
            manager.stop()
    finally:
        _cleanup()


def test_playlist_switch_updates_native_window_to_plain_media_title():
    """切换播放项后，原生标题也不应重新添加 KumiPlayer 会话前缀。"""
    from app.playback.mpv_ipc import set_mpv_playback_title

    pipe = MagicMock()
    with patch("app.playback.mpv_ipc._open_ipc") as mock_open, patch(
        "app.playback.mpv_ipc._set_property",
        side_effect=[True, True],
    ) as mock_set_property:
        mock_open.return_value.__enter__.return_value = pipe

        assert set_mpv_playback_title("ipc", "穹庐下的魔女 - S01E03 - 不灭之焰") is True

    assert mock_set_property.call_args_list == [
        call(pipe, "force-media-title", "穹庐下的魔女 - S01E03 - 不灭之焰", 11),
        call(pipe, "title", "穹庐下的魔女 - S01E03 - 不灭之焰", 12),
    ]


def test_playback_display_title_skips_raw_filename_episode_title():
    """原始文件名残留不应进入 mpv 标题和截图目录名。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.playback.service import _build_playback_display_title

    work = WorkIndex(title="无职转生：到了异世界就拿出真本事")
    episode = EpisodeIndex(
        season_number=2,
        episode_number=1,
        title="Mushoku Tensei S2 ~Isekai Ittara Honki Dasu~ [00][Hi10p_1080p][x264_flac]",
    )

    assert _build_playback_display_title(work, episode) == "无职转生：到了异世界就拿出真本事 - S02E01"


def test_playback_display_title_defaults_regular_episode_to_season_one():
    """普通剧集缺少季号时，也应使用 S01E01 格式。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.playback.service import _build_playback_display_title

    work = WorkIndex(title="擅长捉弄的高木同学")
    episode = EpisodeIndex(
        group_type="season",
        episode_number=1,
        title="",
    )

    assert _build_playback_display_title(work, episode) == "擅长捉弄的高木同学 - S01E01"


def test_playback_display_title_keeps_official_episode_title():
    """真正的官方单集标题可以追加到 SxxExx 后面。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.playback.service import _build_playback_display_title

    work = WorkIndex(title="沉默魔女的秘密")
    episode = EpisodeIndex(
        group_type="season",
        season_number=1,
        episode_number=2,
        title="守护术师菲兹",
    )

    assert _build_playback_display_title(work, episode) == "沉默魔女的秘密 - S01E02 - 守护术师菲兹"


def test_play_strm_path_must_belong_to_library():
    """strm_path 必须属于 LibraryIndex"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest

        manager = get_playback_manager()
        try:
            manager.play(PlaybackRequest(work_id="w1", strm_path="C:\\Windows\\notepad.exe"))
            assert False, "应该抛出 ValueError"
        except ValueError as e:
            assert "不属于" in str(e)
    finally:
        _cleanup()


def test_play_nonexistent_work():
    """不存在的 work_id"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest

        manager = get_playback_manager()
        try:
            manager.play(PlaybackRequest(work_id="nonexistent", episode_id="ep1"))
            assert False, "应该抛出 ValueError"
        except ValueError as e:
            assert "不存在" in str(e)
    finally:
        _cleanup()


def test_stop_terminates_process():
    """stop 优先 IPC quit，不可用时回退 terminate"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest

        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen, \
             patch("app.playback.mpv_ipc.send_mpv_quit", return_value=False) as mock_ipc_quit:
            mock_process = MagicMock()
            mock_process.pid = 12345

            def _wait_behavior(*_args, **_kwargs):
                # 带 timeout 的调用（_stop_internal）：IPC quit 失败后模拟 MPV 未退出 -> 超时
                if _kwargs.get("timeout") is not None:
                    raise subprocess.TimeoutExpired("mpv", _kwargs["timeout"])
                # 无参数 wait（_wait_for_exit 后台线程）阻塞
                return wait_event.wait(timeout=10) or 0

            mock_process.wait.side_effect = _wait_behavior
            mock_popen.return_value = mock_process

            manager = get_playback_manager()
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))
            result = manager.stop()
            assert result["status"] == "stopped"
            # IPC quit 被调用过（但返回 False = 无管道）
            mock_ipc_quit.assert_called_once()
            # 回退到 terminate
            mock_process.terminate.assert_called_once()
    finally:
        _cleanup()


def test_stop_ipc_quit_graceful_exit():
    """IPC quit 成功时不需要 terminate"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest

        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen, \
             patch("app.playback.mpv_ipc.send_mpv_quit", return_value=True) as mock_ipc_quit:
            mock_process = MagicMock()
            mock_process.pid = 12345
            # 无参数 wait（_wait_for_exit 后台线程）阻塞；带 timeout（_stop_internal 优雅退出）返回 0
            mock_process.wait.side_effect = lambda *a, timeout=None, **kw: \
                0 if timeout is not None else (wait_event.wait(timeout=10) or 0)
            mock_popen.return_value = mock_process

            manager = get_playback_manager()
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))
            result = manager.stop()
            assert result["status"] == "stopped"
            mock_ipc_quit.assert_called_once()
            # 优雅退出时不调用 terminate
            mock_process.terminate.assert_not_called()
    finally:
        _cleanup()


def test_status_idle():
    """无播放时 status 返回 idle"""
    _cleanup()
    try:
        from app.playback.service import get_playback_manager
        manager = get_playback_manager()
        result = manager.status()
        assert result["status"] == "idle"
        assert result["session"] is None
    finally:
        _cleanup()


def test_play_writes_history():
    """启动成功写播放历史"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest
        from app.playback.history import get_history

        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
            mock_popen.return_value = mock_process

            manager = get_playback_manager()
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))

            history = get_history(work_id="w1")
            assert len(history) == 1
            assert history[0].work_id == "w1"
            assert history[0].episode_id == "ep1"
            wait_event.set()
            import time; time.sleep(0.3)
            manager.stop()
    finally:
        _cleanup()


def test_mpv_exit_does_not_autoplay_next_episode():
    """mpv 退出后不再由后端自动拉起下一集"""
    _cleanup()
    try:
        _setup_library_index()
        from app.core import config as config_module
        from app.core.config import AppConfig
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest

        old_config = config_module._cached_config
        config_module._cached_config = AppConfig(auto_play_next_episode=False)
        try:
            with patch("subprocess.Popen") as mock_popen:
                mock_process = MagicMock()
                mock_process.pid = 12345
                mock_process.wait.return_value = 0
                mock_popen.return_value = mock_process

                manager = get_playback_manager()
                manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))

                import time; time.sleep(0.3)
                assert mock_popen.call_count == 1
        finally:
            config_module._cached_config = old_config
    finally:
        _cleanup()


def test_mpv_natural_exit_does_not_mark_episode_completed():
    """mpv 退出不等于看完，避免秒开秒关污染已看状态。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest
        from app.playback.progress import list_progress

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.wait.return_value = 0
            mock_popen.return_value = mock_process

            manager = get_playback_manager()
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))

            import time; time.sleep(0.3)
            progress = list_progress("w1")
            completed = {item.episode_id for item in progress if item.completed}
            assert "ep1" not in completed
    finally:
        _cleanup()


def test_mpv_progress_sample_marks_episode_completed():
    """MPV IPC 采样达到阈值时，才自动标记单集看完。"""
    _cleanup()
    try:
        from app.playback.models import PlaybackSession
        from app.playback.progress import list_progress
        from app.playback.service import PlaybackManager

        manager = PlaybackManager()
        session = PlaybackSession(work_id="w1", episode_id="ep1")

        manager._save_progress_sample(session, 960, 1000)

        progress = list_progress("w1")
        assert len(progress) == 1
        assert progress[0].episode_id == "ep1"
        assert progress[0].position == 960
        assert progress[0].duration == 1000
        assert progress[0].completed is True
    finally:
        _cleanup()


def test_mpv_progress_sample_below_threshold_is_not_completed():
    """只打开/只看一小段会记录进度，但不会污染已看状态。"""
    _cleanup()
    try:
        from app.playback.models import PlaybackSession
        from app.playback.progress import list_progress
        from app.playback.service import PlaybackManager

        manager = PlaybackManager()
        session = PlaybackSession(work_id="w1", episode_id="ep1")

        manager._save_progress_sample(session, 30, 1000)

        progress = list_progress("w1")
        assert len(progress) == 1
        assert progress[0].episode_id == "ep1"
        assert progress[0].ratio == 0.03
        assert progress[0].completed is False
    finally:
        _cleanup()


def test_mpv_natural_completion_respects_95_percent_threshold():
    """自然结束兜底与进度存储统一：94% 仍不能标记已看完。"""
    _cleanup()
    try:
        from app.playback.models import PlaybackSession
        from app.playback.progress import list_progress, save_progress
        from app.playback.service import PlaybackManager

        save_progress("w1", "ep1", 940, 1000)
        manager = PlaybackManager()
        session = PlaybackSession(work_id="w1", episode_id="ep1")

        assert manager._finalize_natural_completion(session) is False
        progress = list_progress("w1")
        assert len(progress) == 1
        assert progress[0].ratio == 0.94
        assert progress[0].completed is False
    finally:
        _cleanup()


def test_playing_next_episode_does_not_mark_previous_completed():
    """切到下一集不自动把上一集标为看完，避免误点污染同步状态。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest
        from app.playback.progress import list_progress

        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen:
            first_process = MagicMock()
            first_process.pid = 111
            first_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
            second_process = MagicMock()
            second_process.pid = 222
            second_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
            mock_popen.side_effect = [first_process, second_process]

            manager = get_playback_manager()
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep2"))

            progress = list_progress("w1")
            completed = {item.episode_id for item in progress if item.completed}
            assert "ep1" not in completed
            wait_event.set()
            import time; time.sleep(0.3)
            manager.stop()
    finally:
        _cleanup()


def test_manual_stop_does_not_mark_episode_completed():
    """用户手动停止当前播放时，不应把这一集误记为看完。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest
        from app.playback.progress import list_progress

        wait_event = threading.Event()

        with patch("subprocess.Popen") as mock_popen:
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
            mock_popen.return_value = mock_process

            manager = get_playback_manager()
            manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))
            manager.stop()
            wait_event.set()

            import time; time.sleep(0.3)
            progress = list_progress("w1")
            completed = {item.episode_id for item in progress if item.completed}
            assert "ep1" not in completed
    finally:
        _cleanup()


def test_stop_does_not_autoplay_next_episode():
    """手动 stop 后，即使 wait 返回 0 也不自动播放下一集"""
    _cleanup()
    try:
        _setup_library_index()
        from app.core import config as config_module
        from app.core.config import AppConfig
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest

        old_config = config_module._cached_config
        config_module._cached_config = AppConfig(auto_play_next_episode=True)
        try:
            wait_event = threading.Event()
            with patch("subprocess.Popen") as mock_popen:
                mock_process = MagicMock()
                mock_process.pid = 12345
                mock_process.wait.side_effect = lambda *a, **kw: wait_event.wait(timeout=10) or 0
                mock_popen.return_value = mock_process

                manager = get_playback_manager()
                manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))
                result = manager.stop()
                wait_event.set()

                import time; time.sleep(0.3)
                assert result["status"] == "stopped"
                assert mock_popen.call_count == 1
        finally:
            config_module._cached_config = old_config
    finally:
        _cleanup()


def test_autoplay_enabled_passes_same_season_queue_to_one_mpv_process():
    """自动续播由同一个标准 mpv 进程的原生播放列表完成。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.core import config as config_module
        from app.core.config import AppConfig
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackRequest
        old_config = config_module._cached_config
        config_module._cached_config = AppConfig(auto_play_next_episode=True)
        try:
            wait_event = threading.Event()
            process = MagicMock()
            process.pid = 111
            process.wait.side_effect = lambda timeout=None: (wait_event.wait(timeout=timeout or 5), 0)[1]
            process.poll.side_effect = lambda: 0 if wait_event.is_set() else None
            process.terminate.side_effect = wait_event.set

            with patch("subprocess.Popen") as mock_popen:
                mock_popen.return_value = process

                manager = get_playback_manager()
                manager.play(PlaybackRequest(work_id="w1", episode_id="ep1"))

                mpv_args = mock_popen.call_args.args[0]
                assert mock_popen.call_count == 1
                assert "https://media.example.invalid/CLANNAD-S01E01.mkv" in mpv_args
                assert "https://media.example.invalid/CLANNAD-S01E02.mkv" in mpv_args
                assert not any(arg.casefold().endswith(".strm") for arg in mpv_args)
                assert "--reset-on-next-file=start" in mpv_args

                manager.stop()
        finally:
            config_module._cached_config = old_config
    finally:
        _cleanup()


def test_playlist_switch_records_progress_for_the_new_episode():
    """mpv 原生切到下一项后，进度和历史必须归属新剧集。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.history import get_history
        from app.playback.models import PlaybackSession
        from app.playback.mpv_ipc import MpvProgressEvent
        from app.playback.progress import list_progress
        from app.playback.service import PlaybackManager

        work = load_library_index().works[0]
        session = PlaybackSession(
            session_id="sess_test",
            work_id="w1",
            episode_id="ep1",
            strm_path=work.episodes[0].strm_path,
        )
        monitor_finished = threading.Event()
        process = MagicMock()
        process.poll.side_effect = lambda: 0 if monitor_finished.is_set() else None
        manager = PlaybackManager()
        manager._current_session = session
        manager._current_process = process

        def progress_events():
            yield MpvProgressEvent(position=960, duration=1000, playlist_position=0)
            yield MpvProgressEvent(position=25, duration=1000, playlist_position=1, force_checkpoint=True)
            monitor_finished.set()

        with patch(
            "app.playback.service.observe_mpv_progress",
            return_value=progress_events(),
        ), patch("app.playback.service.time.sleep"), patch(
            "app.playback.service.set_mpv_playback_title",
        ) as mock_set_title:
            manager._monitor_progress(session, process, "ipc", work, work.episodes)

        progress = {item.episode_id: item for item in list_progress("w1")}
        assert progress["ep1"].completed is True
        assert progress["ep2"].position == 25
        assert progress["ep2"].completed is False
        assert session.episode_id == "ep2"
        assert get_history(work_id="w1")[0].episode_id == "ep2"
        mock_set_title.assert_called_once_with("", "CLANNAD - S01E02")
    finally:
        _cleanup()


def test_progress_monitor_survives_a_transient_checkpoint_write_failure():
    """一次临时落盘失败不能终止后续进度同步。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.models import PlaybackSession
        from app.playback.mpv_ipc import MpvProgressEvent
        from app.playback.service import PlaybackManager

        work = load_library_index().works[0]
        session = PlaybackSession(session_id="sess_test", work_id="w1", episode_id="ep1")
        monitor_finished = threading.Event()
        process = MagicMock()
        process.poll.side_effect = lambda: 0 if monitor_finished.is_set() else None
        manager = PlaybackManager()
        manager._current_session = session
        manager._current_process = process

        def progress_events():
            yield MpvProgressEvent(30, 1000, 0, force_checkpoint=True)
            yield MpvProgressEvent(40, 1000, 0, force_checkpoint=True)
            monitor_finished.set()

        with patch(
            "app.playback.service.observe_mpv_progress",
            return_value=progress_events(),
        ), patch.object(
            manager,
            "_save_progress_sample",
            side_effect=[OSError("temporary file lock"), None, None],
        ) as mock_save, patch("app.playback.service.time.sleep"):
            manager._monitor_progress(session, process, "ipc", work, work.episodes)

        assert mock_save.call_count >= 2
        assert session.position == 40
        assert session.duration == 1000
    finally:
        _cleanup()


def test_progress_monitor_reconnects_after_ipc_failure_and_uses_fallback_sample():
    """IPC 短暂断开时先补采样，再重连事件流。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.models import PlaybackSession
        from app.playback.mpv_ipc import MpvProgressEvent
        from app.playback.progress import list_progress
        from app.playback.service import PlaybackManager

        work = load_library_index().works[0]
        session = PlaybackSession(session_id="sess_test", work_id="w1", episode_id="ep1")
        monitor_finished = threading.Event()
        process = MagicMock()
        process.poll.side_effect = lambda: 0 if monitor_finished.is_set() else None
        manager = PlaybackManager()
        manager._current_session = session
        manager._current_process = process

        def recovered_events():
            yield MpvProgressEvent(30, 1000, 0, force_checkpoint=True)
            monitor_finished.set()

        with patch(
            "app.playback.service.observe_mpv_progress",
            side_effect=[OSError("pipe unavailable"), recovered_events()],
        ) as mock_observe, patch(
            "app.playback.service.read_mpv_progress",
            return_value=(15, 1000, 0),
        ) as mock_fallback, patch("app.playback.service.time.sleep"):
            manager._monitor_progress(session, process, "ipc", work, work.episodes)

        progress = {item.episode_id: item for item in list_progress("w1")}
        assert progress["ep1"].position == 30
        assert mock_observe.call_count == 2
        mock_fallback.assert_called_once_with("ipc")
    finally:
        _cleanup()


def test_progress_monitor_maps_episode_by_real_media_path_when_playlist_position_is_unexpected():
    """播放列表被外部扩展时，仍可按真实视频路径保存当前剧集进度。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.models import PlaybackSession
        from app.playback.mpv_ipc import MpvProgressEvent
        from app.playback.progress import list_progress
        from app.playback.service import PlaybackManager

        work = load_library_index().works[0]
        current_episode = work.episodes[0]
        real_path = Path(current_episode.strm_path).read_text(encoding="utf-8").strip()
        session = PlaybackSession(
            session_id="sess_test",
            work_id="w1",
            episode_id=current_episode.episode_id,
            strm_path=current_episode.strm_path,
            real_path=real_path,
        )
        monitor_finished = threading.Event()
        process = MagicMock()
        process.poll.side_effect = lambda: 0 if monitor_finished.is_set() else None
        manager = PlaybackManager()
        manager._current_session = session
        manager._current_process = process

        def progress_events():
            yield MpvProgressEvent(500, 1000, 9, force_checkpoint=True, media_path=real_path)
            monitor_finished.set()

        with patch(
            "app.playback.service.observe_mpv_progress",
            return_value=progress_events(),
        ), patch("app.playback.service.time.sleep"):
            manager._monitor_progress(session, process, "ipc", work, [current_episode])

        progress = {item.episode_id: item for item in list_progress("w1")}
        assert progress["ep1"].position == 500
        assert session.episode_id == "ep1"
    finally:
        _cleanup()


def test_progress_monitor_ignores_unknown_path_even_with_valid_playlist_position():
    """外部扩展/第三方 UI 播放未知路径时，即使 playlist index 有效也不得猜测写入进度。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.models import PlaybackSession
        from app.playback.mpv_ipc import MpvProgressEvent
        from app.playback.progress import list_progress
        from app.playback.service import PlaybackManager

        work = load_library_index().works[0]
        session = PlaybackSession(session_id="sess_test", work_id="w1", episode_id="ep1")
        monitor_finished = threading.Event()
        process = MagicMock()
        process.poll.side_effect = lambda: 0 if monitor_finished.is_set() else None
        manager = PlaybackManager()
        manager._current_session = session
        manager._current_process = process

        def progress_events():
            # media_path 是队列中不存在的外部文件；playlist_position=0 看似有效
            yield MpvProgressEvent(600, 1000, 0, force_checkpoint=True, media_path="D:/External/unrelated.mkv")
            monitor_finished.set()

        with patch(
            "app.playback.service.observe_mpv_progress",
            return_value=progress_events(),
        ), patch("app.playback.service.time.sleep"), patch.object(
            manager, "_save_progress_sample"
        ) as mock_save:
            manager._monitor_progress(session, process, "ipc", work, work.episodes)

        assert mock_save.call_count == 0
        progress = {item.episode_id: item for item in list_progress("w1")}
        assert "ep1" not in progress
        assert session.position == 0.0
        assert session.duration == 0.0
    finally:
        _cleanup()


def test_progress_monitor_throttles_normal_disk_checkpoints_and_flushes_latest_on_exit():
    """高频播放事件只更新内存，正常落盘按检查点节流并在退出时补齐。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.models import PlaybackSession
        from app.playback.mpv_ipc import MpvProgressEvent
        from app.playback.service import PlaybackManager

        work = load_library_index().works[0]
        session = PlaybackSession(session_id="sess_test", work_id="w1", episode_id="ep1")
        monitor_finished = threading.Event()
        process = MagicMock()
        process.poll.side_effect = lambda: 0 if monitor_finished.is_set() else None
        manager = PlaybackManager()
        manager._current_session = session
        manager._current_process = process

        def progress_events():
            for position in (10, 11, 12, 13, 14):
                yield MpvProgressEvent(position, 1000, 0)
            monitor_finished.set()

        with patch(
            "app.playback.service.observe_mpv_progress",
            return_value=progress_events(),
        ), patch.object(manager, "_save_progress_sample") as mock_save, patch(
            "app.playback.service.time.monotonic",
            side_effect=[100.0, 101.0, 102.0, 103.0, 104.0],
        ):
            manager._monitor_progress(session, process, "ipc", work, work.episodes)

        assert mock_save.call_count == 2
        assert mock_save.call_args_list[0].args[1:] == (10, 1000)
        assert mock_save.call_args_list[-1].args[1:] == (14.0, 1000.0)
    finally:
        _cleanup()


def test_progress_monitor_throttles_dense_seek_checkpoints_and_flushes_latest_on_exit():
    """连续拖动时间轴不能让每个 seek 事件都同步写盘。"""
    _cleanup()
    try:
        _setup_library_index()
        from app.library.store import load_library_index
        from app.playback.models import PlaybackSession
        from app.playback.mpv_ipc import MpvProgressEvent
        from app.playback.service import PlaybackManager

        work = load_library_index().works[0]
        session = PlaybackSession(session_id="sess_test", work_id="w1", episode_id="ep1")
        monitor_finished = threading.Event()
        process = MagicMock()
        process.poll.side_effect = lambda: 0 if monitor_finished.is_set() else None
        manager = PlaybackManager()
        manager._current_session = session
        manager._current_process = process

        def progress_events():
            for position in range(100, 110):
                yield MpvProgressEvent(position, 1000, 0, force_checkpoint=True)
            monitor_finished.set()

        with patch(
            "app.playback.service.observe_mpv_progress",
            return_value=progress_events(),
        ), patch.object(manager, "_save_progress_sample") as mock_save, patch(
            "app.playback.service.time.monotonic",
            side_effect=[100.0 + index * 0.1 for index in range(10)],
        ):
            manager._monitor_progress(session, process, "ipc", work, work.episodes)

        assert mock_save.call_count == 2
        assert mock_save.call_args_list[0].args[1:] == (100, 1000)
        assert mock_save.call_args_list[-1].args[1:] == (109.0, 1000.0)
    finally:
        _cleanup()


def test_playback_checkpoint_defers_bangumi_sync_outside_mpv_event_thread():
    """进度监听线程只保存本地进度，完成同步交给后台任务。"""
    from app.playback.models import PlaybackSession
    from app.playback.progress import PlaybackProgressItem
    from app.playback.service import PlaybackManager

    manager = PlaybackManager()
    session = PlaybackSession(session_id="sess_test", work_id="w1", episode_id="ep1")
    completed = PlaybackProgressItem(
        work_id="w1",
        episode_id="ep1",
        position=960,
        duration=1000,
        ratio=0.96,
        completed=True,
        bangumi_synced=False,
    )

    with patch("app.playback.service.save_progress", return_value=completed) as mock_save, patch.object(
        manager,
        "_schedule_completion_sync",
    ) as mock_schedule:
        manager._save_progress_sample(session, 960, 1000)

    mock_save.assert_called_once_with("w1", "ep1", 960, 1000, sync_bangumi=False)
    mock_schedule.assert_called_once_with(session, completed)


def test_playback_completion_sync_is_scheduled_once_per_session_episode():
    """同一会话的连续检查点不能重复创建 Bangumi 同步线程。"""
    from app.playback.models import PlaybackSession
    from app.playback.progress import PlaybackProgressItem
    from app.playback.service import PlaybackManager

    manager = PlaybackManager()
    session = PlaybackSession(session_id="sess_test", work_id="w1", episode_id="ep1")
    completed = PlaybackProgressItem(
        work_id="w1",
        episode_id="ep1",
        completed=True,
        bangumi_synced=False,
    )

    with patch("app.playback.service.threading.Thread") as mock_thread:
        manager._schedule_completion_sync(session, completed)
        manager._schedule_completion_sync(session, completed)

    assert mock_thread.call_count == 1
    mock_thread.return_value.start.assert_called_once_with()


def test_force_window_to_front_uses_foreground_activation():
    """Windows 置前逻辑会先允许前台激活，再短暂 topmost 并恢复非置顶。"""
    from app.playback import mpv

    user32 = MagicMock()
    hwnd = 2468
    user32.GetForegroundWindow.side_effect = [0, hwnd]
    user32.GetWindowThreadProcessId.return_value = 10

    fake_windll = MagicMock()
    fake_windll.kernel32.GetCurrentThreadId.return_value = 10

    with patch.object(mpv.ctypes, "windll", fake_windll):
        mpv._force_window_to_front(user32, hwnd, pid=1357)

    user32.AllowSetForegroundWindow.assert_called_with(1357)
    assert user32.keybd_event.call_count == 2
    user32.ShowWindow.assert_any_call(hwnd, mpv.SW_SHOW)
    user32.ShowWindow.assert_any_call(hwnd, mpv.SW_RESTORE)
    user32.SetForegroundWindow.assert_called_with(hwnd)
    user32.SetWindowPos.assert_any_call(
        hwnd,
        mpv.HWND_TOPMOST,
        0,
        0,
        0,
        0,
        mpv.SWP_NOMOVE | mpv.SWP_NOSIZE | mpv.SWP_SHOWWINDOW,
    )
    user32.SetWindowPos.assert_any_call(
        hwnd,
        mpv.HWND_NOTOPMOST,
        0,
        0,
        0,
        0,
        mpv.SWP_NOMOVE | mpv.SWP_NOSIZE | mpv.SWP_SHOWWINDOW,
    )


if __name__ == "__main__":
    tests = [
        test_play_by_episode_id,
        test_play_strm_path_must_belong_to_library,
        test_play_nonexistent_work,
        test_stop_terminates_process,
        test_status_idle,
        test_play_writes_history,
        test_mpv_exit_does_not_autoplay_next_episode,
        test_stop_does_not_autoplay_next_episode,
        test_autoplay_enabled_passes_same_season_queue_to_one_mpv_process,
        test_force_window_to_front_uses_foreground_activation,
    ]
    for t in tests:
        t()
        print(f"  OK {t.__name__}")
    print(f"\nResult: {len(tests)} passed, 0 failed, {len(tests)} total")
