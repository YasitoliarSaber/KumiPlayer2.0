# -*- coding: utf-8 -*-
"""播放服务层

PlaybackManager 管理当前播放会话，支持 play/stop/status 和 mpv 原生播放列表续播。
"""

import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

from app.core.config import load_config
from app.library.models import EpisodeIndex, WorkIndex
from app.library.store import load_library_index
from app.playback.history import build_history_item, save_history
from app.playback.models import PlaybackRequest, PlaybackSession
from app.playback.mpv import start_mpv
from app.playback.mpv_ipc import (
    MpvProgressEvent,
    make_ipc_server_name,
    observe_mpv_progress,
    read_mpv_progress,
    set_mpv_playback_title,
)
from app.playback.progress import (
    COMPLETE_THRESHOLD,
    PlaybackProgressItem,
    list_progress,
    mark_episode_completed,
    save_progress,
    sync_episode_completion,
)

PROGRESS_CHECKPOINT_INTERVAL_SECONDS = 5.0
FORCED_CHECKPOINT_MIN_INTERVAL_SECONDS = 1.0
PROGRESS_CHECKPOINT_RETRY_SECONDS = 1.0
IPC_RECONNECT_INITIAL_SECONDS = 0.25
IPC_RECONNECT_MAX_SECONDS = 5.0
LOGGER = logging.getLogger(__name__)


class PlaybackManager:
    """播放管理器（单个当前会话）"""

    def __init__(self):
        self._current_session: Optional[PlaybackSession] = None
        self._current_process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._suppressed_session_ids: set[str] = set()
        self._completion_sync_scheduled: set[tuple[str, str]] = set()

    def play(self, request: PlaybackRequest) -> PlaybackSession:
        """播放指定剧集

        流程：
        1. 从 LibraryIndex 定位 work/episode
        2. 停止旧进程（如果有）
        3. 启动 mpv
        4. 记录播放历史
        5. 启动后台线程等待 mpv 退出并更新状态

        异常:
            ValueError: 参数校验失败
            FileNotFoundError: .strm 文件不存在
            RuntimeError: mpv 启动失败
        """
        # 加载 LibraryIndex
        index = load_library_index()
        if index is None:
            raise ValueError("LibraryIndex 不存在，请先执行 POST /api/library/rescan")

        # 定位 work
        work = None
        for w in index.works:
            if w.work_id == request.work_id:
                work = w
                break
        if work is None:
            raise ValueError(f"work_id 不存在: {request.work_id}")

        # 定位 episode
        episode = None
        if request.episode_id:
            for ep in work.episodes:
                if ep.episode_id == request.episode_id:
                    episode = ep
                    break
            if episode is None:
                raise ValueError(f"episode_id 不存在: {request.episode_id}")
        elif request.strm_path:
            # 通过 strm_path 定位
            for ep in work.episodes:
                if ep.strm_path == request.strm_path:
                    episode = ep
                    break
            if episode is None:
                raise ValueError(f"strm_path 不属于该作品: {request.strm_path}")
        else:
            raise ValueError("必须提供 episode_id 或 strm_path")

        # 校验 strm_path 存在
        strm_path = episode.strm_path
        if not strm_path:
            raise ValueError("episode 没有关联的 strm_path")
        if not Path(strm_path).exists():
            raise FileNotFoundError(f".strm 文件不存在: {strm_path}")

        real_path = _read_strm_target(strm_path)
        if not real_path:
            raise ValueError(f".strm 没有有效的媒体路径: {strm_path}")

        # 停止旧进程
        self._stop_internal(suppress_autoplay=True)

        # 使用 KumiPlayer 内置干净 MPV；不再读取用户 mpv_path 或回退系统 PATH。
        config = load_config()
        mpv_path = None
        playback_queue = _build_playback_queue(work, episode, config.auto_play_next_episode)
        playable_queue: list[EpisodeIndex] = []
        media_targets: list[str] = []
        for queued_episode in playback_queue:
            media_target = _read_strm_target(queued_episode.strm_path)
            if not media_target:
                continue
            playable_queue.append(queued_episode)
            media_targets.append(media_target)
        playback_queue = playable_queue

        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        ipc_server = make_ipc_server_name(session_id)
        start_position = request.start_position or _resume_position(request.work_id, episode.episode_id)

        # 启动 mpv
        try:
            process = start_mpv(
                strm_path,
                mpv_path,
                playlist_paths=media_targets,
                ipc_server=ipc_server,
                start_position=start_position,
                display_title=_build_playback_display_title(work, episode),
            )
        except (FileNotFoundError, OSError, RuntimeError) as e:
            raise RuntimeError(f"mpv 启动失败: {e}")

        # 创建 session
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
        session = PlaybackSession(
            session_id=session_id,
            work_id=request.work_id,
            episode_id=episode.episode_id,
            strm_path=strm_path,
            real_path=real_path,
            ipc_server=ipc_server,
            position=start_position,
            pid=process.pid,
            status="playing",
            started_at=now,
        )

        with self._lock:
            self._current_session = session
            self._current_process = process

        _record_playback_history(work, episode)

        # 启动后立即同步 Anime4K 永久默认值（配置源静态默认是 off；用户设置通过 IPC 覆盖）
        try:
            from app.playback.mpv_ipc import send_mpv_script_message
            send_mpv_script_message(
                ipc_server,
                "kumiplayer_anime4k",
                "set-default",
                (config.mpv_anime4k_mode or "off", config.mpv_anime4k_quality or "balanced"),
            )
        except Exception:
            # IPC 同步失败不影响播放启动，静态默认仍为 off/balanced
            pass

        # MPV 进度通过标准 JSON IPC 采样，避免把逻辑做进 mpv 插件里。
        threading.Thread(
            target=self._monitor_progress,
            args=(session, process, ipc_server, work, playback_queue),
            daemon=True,
        ).start()

        # 启动后台线程等待 mpv 退出
        threading.Thread(
            target=self._wait_for_exit,
            args=(session, process),
            daemon=True,
        ).start()

        return session

    def stop(self) -> Dict:
        """停止当前播放"""
        with self._lock:
            if self._current_process is None:
                return {"status": "idle"}

            session_id = self._current_session.session_id if self._current_session else ""
        self._stop_internal(suppress_autoplay=True)
        return {"status": "stopped", "session_id": session_id}

    def status(self) -> Dict:
        """获取当前播放状态"""
        with self._lock:
            if self._current_session is None:
                return {"status": "idle", "session": None}
            return {
                "status": self._current_session.status,
                "session": {
                    "session_id": self._current_session.session_id,
                    "work_id": self._current_session.work_id,
                    "episode_id": self._current_session.episode_id,
                    "strm_path": self._current_session.strm_path,
                    "pid": self._current_session.pid,
                    "started_at": self._current_session.started_at,
                    "position": self._current_session.position,
                    "duration": self._current_session.duration,
                },
            }

    def _stop_internal(self, suppress_autoplay: bool = True) -> None:
        """内部停止方法

        优先通过 IPC 发送 quit 命令让 MPV 优雅退出（避免 Windows 硬杀
        触发崩溃对话框和 CrashSender.exe）；IPC 不可用时回退到 terminate。
        """
        with self._lock:
            if self._current_process is None:
                return
            process = self._current_process
            session = self._current_session
            if suppress_autoplay and session:
                self._suppressed_session_ids.add(session.session_id)

        # 优先尝试 IPC 优雅退出
        ipc_server = session.ipc_server if session else ""
        if ipc_server:
            from app.playback.mpv_ipc import send_mpv_quit
            send_mpv_quit(ipc_server, timeout=1.0)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
            else:
                # MPV 已优雅退出
                with self._lock:
                    if session:
                        now = datetime.now(timezone(timedelta(hours=8))).isoformat()
                        session.status = "stopped"
                        session.ended_at = now
                    if self._current_process is process:
                        self._current_process = None
                return

        # 回退：硬终止（仅当 IPC 不可用或 MPV 未在 3 秒内退出）
        try:
            process.terminate()
            process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            try:
                process.kill()
            except OSError:
                pass

        with self._lock:
            if session:
                now = datetime.now(timezone(timedelta(hours=8))).isoformat()
                session.status = "stopped"
                session.ended_at = now
            if self._current_process is process:
                self._current_process = None

    def _wait_for_exit(
        self,
        session: PlaybackSession,
        process: subprocess.Popen,
    ) -> None:
        """等待 mpv 退出并补齐当前播放项的最终状态。"""
        if process is None:
            return

        try:
            exit_code = process.wait()
        except OSError:
            exit_code = -1

        with self._lock:
            now = datetime.now(timezone(timedelta(hours=8))).isoformat()
            suppressed = session.session_id in self._suppressed_session_ids
            self._suppressed_session_ids.discard(session.session_id)
            is_current_process = self._current_process is process
            session.exit_code = exit_code
            session.ended_at = now
            if not suppressed:
                session.status = "exited" if exit_code == 0 else "failed"
            if is_current_process:
                self._current_process = None

        if suppressed or exit_code != 0 or not is_current_process:
            return

        self._finalize_natural_completion(session)

    def _monitor_progress(
        self,
        session: PlaybackSession,
        process: subprocess.Popen,
        ipc_server: str,
        work: WorkIndex,
        playback_queue: list[EpisodeIndex],
    ) -> None:
        """订阅 MPV 事件，并以低频检查点可靠持久化播放进度。"""
        active_playlist_position = 0
        last_event: Optional[MpvProgressEvent] = None
        last_checkpoint_at = 0.0
        next_checkpoint_retry_at = 0.0
        reconnect_delay = IPC_RECONNECT_INITIAL_SECONDS
        media_path_positions = _build_media_path_positions(playback_queue)

        while process.poll() is None:
            try:
                observed_event = False
                for event in observe_mpv_progress(ipc_server):
                    observed_event = True
                    reconnect_delay = IPC_RECONNECT_INITIAL_SECONDS
                    if process.poll() is not None:
                        break
                    result = self._handle_progress_event(
                        session,
                        work,
                        playback_queue,
                        media_path_positions,
                        active_playlist_position,
                        event,
                        last_checkpoint_at,
                        next_checkpoint_retry_at,
                    )
                    if result is None:
                        continue
                    active_playlist_position, last_checkpoint_at, next_checkpoint_retry_at = result
                    last_event = event

                if process.poll() is None and not observed_event:
                    time.sleep(reconnect_delay)
            except (OSError, TimeoutError, ValueError, UnicodeDecodeError) as exc:
                if process.poll() is not None:
                    break
                LOGGER.warning("MPV progress event stream disconnected; retrying: %s", exc)
                fallback = read_mpv_progress(ipc_server)
                if fallback is not None:
                    fallback_event = MpvProgressEvent(
                        position=fallback[0],
                        duration=fallback[1],
                        playlist_position=fallback[2],
                        force_checkpoint=True,
                    )
                    result = self._handle_progress_event(
                        session,
                        work,
                        playback_queue,
                        media_path_positions,
                        active_playlist_position,
                        fallback_event,
                        last_checkpoint_at,
                        next_checkpoint_retry_at,
                    )
                    if result is not None:
                        active_playlist_position, last_checkpoint_at, next_checkpoint_retry_at = result
                        last_event = fallback_event
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, IPC_RECONNECT_MAX_SECONDS)

        if last_event is not None and session.position >= 0 and session.duration > 0:
            self._checkpoint_progress(session, session.position, session.duration, attempts=3)
        with self._lock:
            self._completion_sync_scheduled = {
                key for key in self._completion_sync_scheduled if key[0] != session.session_id
            }

    def _handle_progress_event(
        self,
        session: PlaybackSession,
        work: WorkIndex,
        playback_queue: list[EpisodeIndex],
        media_path_positions: dict[str, int],
        active_playlist_position: int,
        event: MpvProgressEvent,
        last_checkpoint_at: float,
        next_checkpoint_retry_at: float,
    ) -> Optional[tuple[int, float, float]]:
        playlist_position = _resolve_playlist_position(event, playback_queue, media_path_positions)
        if playlist_position is None:
            LOGGER.warning(
                "Ignoring MPV progress with unknown playlist identity: position=%s path=%r",
                event.playlist_position,
                event.media_path,
            )
            return None

        force_checkpoint = event.force_checkpoint
        if playlist_position != active_playlist_position:
            if session.duration > 0 and session.position >= 0:
                self._checkpoint_progress(session, session.position, session.duration, attempts=3)
            self._finalize_natural_completion(session)
            active_playlist_position = playlist_position
            self._activate_episode(session, work, playback_queue[playlist_position])
            force_checkpoint = True

        self._update_session_progress(session, event.position, event.duration)
        now = time.monotonic()
        checkpoint_interval = (
            FORCED_CHECKPOINT_MIN_INTERVAL_SECONDS
            if force_checkpoint
            else PROGRESS_CHECKPOINT_INTERVAL_SECONDS
        )
        checkpoint_due = (
            last_checkpoint_at <= 0
            or now - last_checkpoint_at >= checkpoint_interval
        )
        if checkpoint_due and now >= next_checkpoint_retry_at:
            if self._checkpoint_progress(session, event.position, event.duration):
                last_checkpoint_at = now
                next_checkpoint_retry_at = 0.0
            else:
                next_checkpoint_retry_at = now + PROGRESS_CHECKPOINT_RETRY_SECONDS

        return active_playlist_position, last_checkpoint_at, next_checkpoint_retry_at

    def _update_session_progress(self, session: PlaybackSession, position: float, duration: float) -> None:
        if position < 0 or duration <= 0:
            return
        with self._lock:
            session.position = float(position)
            session.duration = float(duration)
            if self._current_session and self._current_session.session_id == session.session_id:
                self._current_session.position = float(position)
                self._current_session.duration = float(duration)

    def _checkpoint_progress(
        self,
        session: PlaybackSession,
        position: float,
        duration: float,
        *,
        attempts: int = 1,
    ) -> bool:
        for attempt in range(max(1, attempts)):
            try:
                self._save_progress_sample(session, position, duration)
                return True
            except Exception as exc:
                LOGGER.warning(
                    "MPV progress checkpoint failed (%s/%s): %s",
                    attempt + 1,
                    max(1, attempts),
                    exc,
                )
                if attempt + 1 < attempts:
                    time.sleep(0.1)
        return False

    def _activate_episode(self, session: PlaybackSession, work: WorkIndex, episode: EpisodeIndex) -> None:
        """mpv 切换播放项时同步会话身份并写入该集历史。"""
        real_path = _read_strm_target(episode.strm_path)
        with self._lock:
            session.episode_id = episode.episode_id
            session.strm_path = episode.strm_path
            session.real_path = real_path
            session.position = 0.0
            session.duration = 0.0
            if self._current_session and self._current_session.session_id == session.session_id:
                self._current_session = session
        set_mpv_playback_title(
            session.ipc_server,
            _build_playback_display_title(work, episode),
        )
        _record_playback_history(work, episode)

    def _save_progress_sample(self, session: PlaybackSession, position: float, duration: float) -> None:
        if not session.work_id or not session.episode_id:
            return
        if position < 0 or duration <= 0:
            return
        item = save_progress(
            session.work_id,
            session.episode_id,
            position,
            duration,
            sync_bangumi=False,
        )
        if item.completed and not item.bangumi_synced:
            self._schedule_completion_sync(session, item)
        with self._lock:
            if self._current_session and self._current_session.session_id == session.session_id:
                self._current_session.position = item.position
                self._current_session.duration = item.duration

    def _schedule_completion_sync(
        self,
        session: PlaybackSession,
        item: PlaybackProgressItem,
    ) -> None:
        """后台同步完成状态，避免网络请求阻塞 MPV IPC 事件读取。"""
        key = (session.session_id, item.episode_id)
        with self._lock:
            if key in self._completion_sync_scheduled:
                return
            self._completion_sync_scheduled.add(key)
        threading.Thread(
            target=self._sync_completion_in_background,
            args=(item.work_id, item.episode_id),
            daemon=True,
        ).start()

    @staticmethod
    def _sync_completion_in_background(work_id: str, episode_id: str) -> None:
        try:
            sync_episode_completion(work_id, episode_id)
        except Exception as exc:
            LOGGER.warning("Bangumi completion sync failed in background: %s", exc)

    def _finalize_natural_completion(self, session: PlaybackSession) -> bool:
        """用退出前最后一次有效采样补齐完成状态，并触发 Bangumi 同步。"""
        item = next(
            (current for current in list_progress(session.work_id) if current.episode_id == session.episode_id),
            None,
        )
        if item is None or item.duration <= 0 or item.position <= 0:
            return False
        if item.completed:
            return True
        if item.ratio < COMPLETE_THRESHOLD:
            return False
        return mark_episode_completed(session.work_id, session.episode_id, True).completed

def _find_episode(work: WorkIndex, episode_id: str) -> Optional[EpisodeIndex]:
    for episode in work.episodes:
        if episode.episode_id == episode_id:
            return episode
    return None


def _build_playback_queue(
    work: WorkIndex,
    current: EpisodeIndex,
    auto_play_next_episode: bool,
) -> list[EpisodeIndex]:
    """构建同作品、同季度的受控队列，避免 mpv 扫入目录中的 SP/OVA。"""
    if not auto_play_next_episode or current.group_type != "season":
        return [current]
    candidates = sorted(
        (
            episode
            for episode in work.episodes
            if episode.group_type == current.group_type
            and int(episode.season_number or 0) == int(current.season_number or 0)
            and int(episode.episode_number or 0) > int(current.episode_number or 0)
            and episode.strm_path
            and episode.availability != "missing"
            and Path(episode.strm_path).is_file()
        ),
        key=lambda episode: (int(episode.episode_number or 0), episode.episode_id),
    )
    queue = [current]
    queued_numbers = {int(current.episode_number or 0)}
    for episode in candidates:
        episode_number = int(episode.episode_number or 0)
        if episode_number in queued_numbers:
            continue
        queue.append(episode)
        queued_numbers.add(episode_number)
    return queue


def _build_media_path_positions(playback_queue: list[EpisodeIndex]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for playlist_position, episode in enumerate(playback_queue):
        for candidate in (episode.strm_path, _read_strm_target(episode.strm_path)):
            normalized = _normalize_media_path(candidate)
            if not normalized:
                continue
            previous = positions.get(normalized)
            positions[normalized] = playlist_position if previous is None else -1
    return positions


def _resolve_playlist_position(
    event: MpvProgressEvent,
    playback_queue: list[EpisodeIndex],
    media_path_positions: dict[str, int],
) -> Optional[int]:
    """把 MPV 进度事件映射到受控队列位置。

    安全策略：
    - 事件带有真实媒体路径时，优先按路径精确匹配（strm 路径或真实路径）；
      若路径存在但无法确认属于受控队列，一律忽略（外部扩展/第三方 UI 播放
      的未知文件不得把进度写进错误剧集）；
    - 事件未携带路径（旧客户端/回退采样）时，沿用受控队列的 playlist index
      回退，保持既有事件流兼容；
    - 路径映射发生重复（-1）时也不接受，避免歧义。
    """
    normalized_path = _normalize_media_path(event.media_path)
    if normalized_path:
        path_position = media_path_positions.get(normalized_path)
        if path_position is not None and path_position >= 0:
            return path_position
        return None
    if 0 <= event.playlist_position < len(playback_queue):
        return event.playlist_position
    return None


def _normalize_media_path(value: str) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", path, flags=re.IGNORECASE):
        return path
    return os.path.normcase(os.path.normpath(path))


def _read_strm_target(strm_path: str) -> str:
    try:
        content = Path(strm_path).read_text(encoding="utf-8-sig")
    except (IOError, UnicodeDecodeError):
        return ""
    return next((line.strip() for line in content.splitlines() if line.strip()), "")


def _record_playback_history(work: WorkIndex, episode: EpisodeIndex) -> None:
    save_history(build_history_item(
        work_id=work.work_id,
        work_title=work.title,
        episode_id=episode.episode_id,
        episode_title=episode.title,
        source=episode.source or work.source,
        media_type=work.media_type,
        group_type=episode.group_type,
        season_number=episode.season_number,
        episode_number=episode.episode_number,
        strm_path=episode.strm_path,
        poster_path=work.poster_path,
    ))


def _resume_position(work_id: str, episode_id: str) -> float:
    for item in list_progress(work_id):
        if item.episode_id != episode_id:
            continue
        if item.completed or item.duration <= 0 or item.position <= 5:
            return 0.0
        if item.ratio >= 0.95:
            return 0.0
        return max(0.0, min(float(item.position), float(item.duration) - 5))
    return 0.0


def _build_playback_display_title(work: WorkIndex, episode: EpisodeIndex) -> str:
    title = (work.title or work.original_title or "KumiPlayer").strip()
    season = int(episode.season_number or 0)
    episode_number = int(episode.episode_number or 0)
    if season <= 0 and episode.group_type == "season" and episode_number > 0:
        season = 1
    if season > 0 and episode_number > 0:
        title = f"{title} - S{season:02d}E{episode_number:02d}"
    elif episode_number > 0:
        title = f"{title} - E{episode_number:02d}"
    episode_title = _clean_episode_display_title(episode.title, title, season, episode_number)
    if episode_title and episode_title not in title:
        title = f"{title} - {episode_title}"
    return title


def _clean_episode_display_title(raw_title: str, playback_title: str, season: int, episode_number: int) -> str:
    """只保留真正的剧集标题，避免把原始文件名拼进 mpv/截图名称。"""
    title = " ".join(str(raw_title or "").split()).strip(" -_")
    if not title:
        return ""

    title = _strip_episode_code_prefix(title, season, episode_number).strip(" -_")
    if not title:
        return ""
    if _looks_like_raw_filename_title(title):
        return ""
    if _looks_like_generic_episode_title(title, season, episode_number):
        return ""
    if title in playback_title:
        return ""
    return title


def _strip_episode_code_prefix(title: str, season: int, episode_number: int) -> str:
    patterns: list[str] = []
    if season > 0 and episode_number > 0:
        patterns.append(rf"(?i)^S0?{season}E0?{episode_number}\s*[-_:：]\s*")
    if episode_number > 0:
        patterns.extend([
            rf"(?i)^E0?{episode_number}\s*[-_:：]\s*",
            rf"^第\s*0?{episode_number}\s*[集话話]\s*[-_:：]\s*",
        ])
    for pattern in patterns:
        title = re.sub(pattern, "", title).strip()
    return title


def _looks_like_raw_filename_title(title: str) -> bool:
    lowered = title.casefold()
    if re.search(r"\.(?:mkv|mp4|avi|mov|wmv|flv|webm|m2ts|ts)$", lowered):
        return True
    if re.search(r"\[[^\]]*(?:\d{3,4}p|x26[45]|h\.?26[45]|hevc|avc|flac|aac|bdrip|webrip|web-dl|ma10p|hi10p|10bit)[^\]]*\]", lowered):
        return True
    if len(re.findall(r"\[[^\]]+\]", title)) >= 2:
        return True
    if re.search(r"(?i)(?:^|[\s._-])S\d{1,2}(?:[\s~._-]|$)", title) and ("[" in title or "~" in title):
        return True
    if re.search(r"(?i)(?:vcb-studio|loli(?:house)?|nekomoe|airota|bdrip|webrip|x264|x265|flac)", title):
        return True
    return False


def _looks_like_generic_episode_title(title: str, season: int, episode_number: int) -> bool:
    normalized = re.sub(r"\s+", "", title).casefold()
    if not normalized:
        return True
    generic_values = {
        f"episode{episode_number}",
        f"ep{episode_number}",
        f"e{episode_number}",
        f"episode{episode_number:02d}",
        f"ep{episode_number:02d}",
        f"e{episode_number:02d}",
        f"第{episode_number}集",
        f"第{episode_number:02d}集",
        f"第{episode_number}话",
        f"第{episode_number:02d}话",
        f"第{episode_number}話",
        f"第{episode_number:02d}話",
    }
    if season > 0:
        generic_values.add(f"s{season}e{episode_number}")
        generic_values.add(f"s{season:02d}e{episode_number:02d}")
    return normalized in generic_values


# 全局 PlaybackManager 实例
_playback_manager: Optional[PlaybackManager] = None


def get_playback_manager() -> PlaybackManager:
    """获取全局 PlaybackManager 单例"""
    global _playback_manager
    if _playback_manager is None:
        _playback_manager = PlaybackManager()
    return _playback_manager


def reset_playback_manager() -> None:
    """重置 PlaybackManager（测试用）"""
    global _playback_manager
    if _playback_manager is not None:
        _playback_manager.stop()
        _playback_manager = None
