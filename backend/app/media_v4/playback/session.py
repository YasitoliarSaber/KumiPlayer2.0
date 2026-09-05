"""基于 V4 Asset 身份的单实例 MPV 会话管理。"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import load_config
from app.core.paths import get_data_dir
from app.media_v4.path_validation import validate_playback_locator
from app.media_v4.persistence.database import V4Database
from app.media_v4.playback.store import V4PlaybackStore
from app.playback.mpv import start_mpv
from app.playback.mpv_ipc import (
    make_ipc_server_name,
    observe_mpv_progress,
    send_mpv_quit,
    set_mpv_playback_title,
)

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_playback_locator(value: object) -> str:
    """生成仅用于受控播放队列身份比对的稳定键。"""

    locator = str(value or "").strip()
    if not locator:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", locator, flags=re.IGNORECASE):
        # URL 的 path 和 query 可能有大小写语义，不能按 Windows 路径折叠。
        return locator
    return os.path.normpath(locator).replace("/", "\\").casefold()


def _resolve_event_playlist_position(event, playlist: list[dict]) -> int | None:
    """以 MPV 实际路径确认受控队列身份，避免错误剧集被写入进度。"""

    media_path = _normalize_playback_locator(getattr(event, "media_path", ""))
    if media_path:
        positions: dict[str, int] = {}
        for position, asset in enumerate(playlist):
            for candidate in (asset.get("playback_locator"), asset.get("source_locator")):
                normalized = _normalize_playback_locator(candidate)
                if not normalized:
                    continue
                previous = positions.get(normalized)
                # 本地来源常令 source_locator 与 playback_locator 指向同一文件；
                # 这只是同一 Asset 的两个等价字段，不是跨剧集的身份歧义。
                positions[normalized] = position if previous in {None, position} else -1
        resolved = positions.get(media_path)
        return resolved if resolved is not None and resolved >= 0 else None

    playlist_position = int(getattr(event, "playlist_position", -1))
    return playlist_position if 0 <= playlist_position < len(playlist) else None


class V4PlaybackManager:
    """MPV 只消费已确认媒体图中的 Asset，不接受调用方传入任意路径。"""

    def __init__(self, database: V4Database, *, session_dir: str | Path | None = None):
        self.database = database
        self.session_dir = Path(session_dir) if session_dir else get_data_dir() / "playback" / "sessions"
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._session: dict | None = None

    def play(self, work_id: str, episode_id: str, asset_id: str = "") -> dict:
        asset = self._resolve_asset(work_id, episode_id, asset_id)
        self.stop()
        locator = asset["playback_locator"] or asset["source_locator"]
        if not locator:
            raise ValueError("Asset 没有可播放定位符")
        ok, reason = validate_playback_locator(locator)
        if not ok:
            raise ValueError(reason)
        playlist = self._build_playlist(work_id, asset)
        playlist_locators = [item["playback_locator"] or item["source_locator"] for item in playlist]
        strm_path = self.session_dir / f"{asset['asset_id']}.strm"
        self._write_strm(strm_path, locator)
        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        ipc_server = make_ipc_server_name(session_id)
        try:
            progress = V4PlaybackStore(self.database).get_progress(episode_id, asset["asset_id"])
        except KeyError:
            progress = {}
        start_position = 0.0
        if progress and not progress.get("completed"):
            start_position = float(progress.get("position") or 0)
        title = self._display_title(asset)
        process = start_mpv(
            str(strm_path),
            playlist_paths=playlist_locators,
            ipc_server=ipc_server,
            start_position=start_position,
            display_title=title,
        )
        session = {
            "session_id": session_id,
            "status": "playing",
            "pid": process.pid,
            "work_id": work_id,
            "episode_id": episode_id,
            "asset_id": asset["asset_id"],
            "playback_locator": locator,
            "ipc_server": ipc_server,
            "position": start_position,
            "duration": float(progress.get("duration") or 0) if progress else 0.0,
            "playlist": playlist,
            "playlist_position": 0,
            "started_at": _now(),
        }
        with self._lock:
            self._process = process
            self._session = session
        V4PlaybackStore(self.database).record_activation(
            work_id, episode_id, asset["asset_id"]
        )
        threading.Thread(target=self._monitor, args=(process, session), daemon=True).start()
        return dict(session)

    def stop(self) -> dict:
        with self._lock:
            process = self._process
            session = self._session
        if process is None:
            return {"status": "idle"}
        if session and session.get("ipc_server"):
            send_mpv_quit(session["ipc_server"], timeout=1.0)
        try:
            process.wait(timeout=3)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except OSError:
                    pass
        with self._lock:
            if self._process is process:
                self._process = None
                if self._session:
                    self._session["status"] = "stopped"
        return {"status": "stopped", "session_id": session["session_id"] if session else ""}

    def status(self) -> dict:
        with self._lock:
            if self._session is None:
                return {"status": "idle", "session": None}
            session = dict(self._session)
            if self._process is None and session["status"] == "playing":
                session["status"] = "exited"
            return {"status": session["status"], "session": session}

    def _resolve_asset(self, work_id: str, episode_id: str, asset_id: str) -> dict:
        with self.database.connect() as conn:
            if episode_id == f"movie:{work_id}":
                row = conn.execute(
                    """
                    SELECT a.*, w.preferred_title, 0 AS local_season_number,
                           1 AS local_episode_number, NULL AS special_number,
                           'movie' AS episode_kind
                    FROM work_assets wa
                    JOIN assets a ON a.asset_id = wa.asset_id
                    JOIN works w ON w.work_id = wa.work_id
                    JOIN source_evidence se ON se.evidence_id = a.evidence_id
                    WHERE wa.work_id = ? AND (? = '' OR a.asset_id = ?)
                      AND EXISTS (
                          SELECT 1 FROM revision_bindings rb
                          JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                          JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                          WHERE rb.work_id = wa.work_id AND rb.asset_id = a.asset_id
                            AND rb.episode_id IS NULL AND ir.status = 'confirmed'
                      )
                    ORDER BY wa.preference_rank,
                             CASE WHEN a.availability_state = 'available' THEN 0 ELSE 1 END,
                             CASE WHEN se.provider = 'local' THEN 0 ELSE 1 END,
                             CASE lower(a.resolution)
                                 WHEN '4k' THEN 4000 WHEN 'uhd' THEN 4000
                                 ELSE CAST(replace(replace(lower(a.resolution), 'p', ''), 'i', '') AS INTEGER)
                             END DESC,
                             CASE WHEN a.fingerprint = '' THEN 1 ELSE 0 END,
                             a.fingerprint, a.asset_id
                    LIMIT 1
                    """,
                    (work_id, asset_id, asset_id),
                ).fetchone()
                if row is None:
                    raise KeyError((work_id, episode_id, asset_id))
                asset = dict(row)
                asset["preferred_title"] = self._playback_work_title(
                    conn,
                    work_id,
                    str(asset.get("preferred_title") or ""),
                )
                return asset
            row = conn.execute(
                """
                SELECT a.*, w.preferred_title, e.episode_id, s.local_season_number,
                       e.local_episode_number, e.special_number, e.episode_kind,
                       e.display_title
                FROM episode_assets ea
                JOIN assets a ON a.asset_id = ea.asset_id
                JOIN episodes e ON e.episode_id = ea.episode_id
                JOIN seasons s ON s.season_id = e.season_id
                JOIN works w ON w.work_id = e.work_id
                JOIN source_evidence se ON se.evidence_id = a.evidence_id
                WHERE e.work_id = ? AND e.episode_id = ?
                  AND (? = '' OR a.asset_id = ?)
                  AND EXISTS (
                      SELECT 1 FROM revision_bindings rb
                      JOIN import_revisions ir ON ir.revision_id = rb.revision_id
                          JOIN source_roots sr ON sr.root_id = ir.root_id AND sr.retired_at = ''
                      WHERE rb.work_id = e.work_id AND rb.episode_id = e.episode_id
                        AND rb.asset_id = a.asset_id AND ir.status = 'confirmed'
                  )
                ORDER BY ea.preference_rank,
                         CASE WHEN a.availability_state = 'available' THEN 0 ELSE 1 END,
                         CASE WHEN se.provider = 'local' THEN 0 ELSE 1 END,
                         CASE lower(a.resolution)
                             WHEN '4k' THEN 4000 WHEN 'uhd' THEN 4000
                             ELSE CAST(replace(replace(lower(a.resolution), 'p', ''), 'i', '') AS INTEGER)
                         END DESC,
                         CASE WHEN a.fingerprint = '' THEN 1 ELSE 0 END,
                         a.fingerprint, a.asset_id
                LIMIT 1
                """,
                (work_id, episode_id, asset_id, asset_id),
            ).fetchone()
            if row is None:
                raise KeyError((work_id, episode_id, asset_id))
            asset = dict(row)
            asset["preferred_title"] = self._playback_work_title(
                conn,
                work_id,
                str(asset.get("preferred_title") or ""),
            )
            asset["scraped_display_title"] = self._playback_episode_title(
                conn, work_id, episode_id
            )
            return asset

    @staticmethod
    def _playback_work_title(conn, work_id: str, fallback: str) -> str:
        """播放标题跟随用户覆盖或已确认刮削标题，不回退到导入文件名。"""

        override_row = conn.execute(
            "SELECT override_json FROM work_overrides WHERE work_id = ?",
            (work_id,),
        ).fetchone()
        if override_row is not None:
            try:
                override = json.loads(str(override_row["override_json"] or "{}"))
            except (TypeError, ValueError):
                override = {}
            title = str(override.get("title") or "").strip()
            if title:
                return title

        scrape_row = conn.execute(
            """
            SELECT sb.metadata_json
            FROM scrape_bindings sb
            JOIN import_revisions ir ON ir.revision_id = sb.revision_id
            WHERE sb.work_id = ? AND sb.status = 'confirmed' AND ir.status = 'confirmed'
            ORDER BY sb.updated_at DESC, sb.binding_id DESC
            LIMIT 1
            """,
            (work_id,),
        ).fetchone()
        if scrape_row is not None:
            try:
                metadata = json.loads(str(scrape_row["metadata_json"] or "{}"))
            except (TypeError, ValueError):
                metadata = {}
            title = str(metadata.get("title") or "").strip()
            if title:
                return title
        return fallback or "KumiPlayer"

    @staticmethod
    def _playback_episode_title(conn, work_id: str, episode_id: str) -> str:
        """优先使用已确认刮削映射中的完整剧集名；本地标题只作降级。"""

        rows = conn.execute(
            """
            SELECT sb.metadata_json
            FROM scrape_bindings sb
            JOIN import_revisions ir ON ir.revision_id = sb.revision_id
            WHERE sb.work_id = ? AND sb.status = 'confirmed' AND ir.status = 'confirmed'
            ORDER BY sb.updated_at DESC, sb.binding_id DESC
            """,
            (work_id,),
        ).fetchall()
        # 同一 Work 可能在不同来源或后续 revision 中有多份已确认刮削。
        # 最新记录若只覆盖部分剧集，不能遮蔽旧记录中当前 Episode 已有的
        # 完整标题；按新到旧查找实际包含该 Episode 的非空映射即可兼顾新鲜度
        # 与播放标题的连续性。
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata_json"] or "{}"))
            except (TypeError, ValueError):
                continue
            for item in metadata.get("episode_mappings") or []:
                if str(item.get("episode_id") or "") == episode_id:
                    title = str(item.get("title") or "").strip()
                    if title:
                        return title
        return ""

    def _build_playlist(self, work_id: str, current: dict) -> list[dict]:
        """按同作品同常规季度建立受控播放队列。

        只附加当前集之后、已确认且可播放的正片剧集；特别篇、电影、MV 等永不被
        自动塞进播放列表。每集仍复用 V4 的 Asset 选择排序，因此不会绕过版本和
        可用性规则。
        """

        if (
            not load_config().auto_play_next_episode
            or current.get("episode_kind") != "regular"
            or int(current.get("local_season_number") or 0) <= 0
        ):
            return [current]
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.episode_id
                FROM episodes e
                JOIN seasons s ON s.season_id = e.season_id
                WHERE e.work_id = ?
                  AND s.season_kind = 'regular'
                  AND e.episode_kind = 'regular'
                  AND s.local_season_number = ?
                  AND e.local_episode_number > ?
                ORDER BY e.local_episode_number, e.episode_id
                """,
                (
                    work_id,
                    int(current["local_season_number"]),
                    int(current["local_episode_number"]),
                ),
            ).fetchall()
        playlist = [current]
        for row in rows:
            try:
                candidate = self._resolve_asset(work_id, str(row["episode_id"]), "")
                locator = candidate.get("playback_locator") or candidate.get("source_locator")
                ok, _reason = validate_playback_locator(str(locator or ""))
                if ok:
                    playlist.append(candidate)
            except (KeyError, ValueError):
                continue
        return playlist

    def _schedule_completion_sync(self, session: dict, asset: dict) -> None:
        """看完后非阻塞同步 Bangumi；未匹配或离线不影响本地状态。"""

        episode_id = str(asset.get("episode_id") or "")
        if not episode_id or episode_id.startswith("movie:"):
            return
        with self._lock:
            scheduled = session.setdefault("completion_sync_scheduled", set())
            if episode_id in scheduled:
                return
            scheduled.add(episode_id)
        threading.Thread(
            target=self._run_completion_sync,
            args=(
                session,
                str(session["work_id"]),
                episode_id,
                int(asset.get("local_season_number") or 0),
            ),
            daemon=True,
        ).start()

    def _run_completion_sync(
        self,
        session: dict,
        work_id: str,
        episode_id: str,
        season_number: int,
    ) -> None:
        """仅在瞬时失败耗尽重试时释放本会话的再次投递资格。"""

        try:
            terminal = self._sync_completed_episode(work_id, episode_id, season_number)
        except Exception:
            LOGGER.exception("Bangumi completion sync crashed for %s", episode_id)
            terminal = False
        if terminal:
            return
        with self._lock:
            session.setdefault("completion_sync_scheduled", set()).discard(episode_id)

    def _sync_completed_episode(self, work_id: str, episode_id: str, season_number: int) -> bool:
        """同步完成状态；返回是否已得到成功或无需重试的终态。"""

        from fastapi import HTTPException

        from app.api.bangumi import EpisodeWatchedRequest, sync_completed_episode

        for attempt in range(3):
            try:
                sync_completed_episode(
                    self.database,
                    episode_id,
                    EpisodeWatchedRequest(
                        work_id=work_id,
                        season_number=season_number if season_number > 0 else None,
                    ),
                )
                return True
            except HTTPException as exc:
                # 未匹配、剧集映射缺失和本地参数错误不是瞬时故障；继续重试
                # 只会延迟后台线程，不能让用户等同一个无效请求三次。
                if exc.status_code not in {408, 429} and exc.status_code < 500:
                    LOGGER.info(
                        "Bangumi completion sync skipped for %s: HTTP %s",
                        episode_id,
                        exc.status_code,
                    )
                    return True
                error = f"HTTP {exc.status_code}"
            except Exception as exc:
                # 同步服务已经把 BangumiError 写入失败记录；这里仍要保护
                # 播放线程，并为暂时的本地/网络异常保留有限重试机会。
                error = str(exc)
            if attempt == 2:
                LOGGER.warning(
                    "Bangumi completion sync failed after retries for %s: %s",
                    episode_id,
                    error,
                )
                return False
            delay = 0.5 * (2**attempt)
            LOGGER.warning(
                "Bangumi completion sync retry %s/3 for %s after %.1fs: %s",
                attempt + 2,
                episode_id,
                delay,
                error,
            )
            time.sleep(delay)
        return False

    @staticmethod
    def _write_strm(path: Path, locator: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(locator, encoding="utf-8", newline="\n")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _display_title(asset: dict) -> str:
        import re

        from app.media_v4.parsing.episode_titles import (
            is_generic_special_title,
            is_special_marker_only,
        )

        title = str(asset["preferred_title"] or "KumiPlayer")
        if asset["episode_kind"] == "movie":
            return title
        # 季集编号只来自 V4 Episode 本地事实；语义标题仅在有可区分内容时追加，
        # 不追加"第 N 集"/"特别篇"这类占位词。历史数据标题里残留的 SP/S00E
        # 前缀在这里剥离，避免窗口标题出现 "SP08 SP08"。
        episode_title = str(
            asset.get("scraped_display_title") or asset.get("display_title") or ""
        ).strip()
        episode_title = re.sub(
            r"^(?:S00\s*E\s*\d+|SP\s*\d+)\s*[-–—·:：]?\s*",
            "",
            episode_title,
            flags=re.IGNORECASE,
        )
        if is_generic_special_title(episode_title) or is_special_marker_only(episode_title):
            episode_title = ""
        season = int(asset["local_season_number"] or 0)
        episode = int(asset["local_episode_number"] or 0)
        if asset["episode_kind"] == "special" or season == 0:
            # SPxx 是内部结构编号，窗口标题只呈现可读的语义标题；无法刮削时
            # 使用带序号的中文兜底，既不泄漏内部码，也不会让多个特别篇同名。
            if episode_title:
                return f"{title} - {episode_title}"
            return f"{title} - 特别篇 {int(asset['special_number'] or 1)}"
        suffix = f" {episode_title}" if episode_title else ""
        return f"{title} - S{season:02d}E{episode:02d}{suffix}"

    def _checkpoint_progress(
        self,
        session: dict,
        episode_id: str,
        asset_id: str,
        position: float,
        duration: float,
        completed: bool,
        *,
        attempts: int = 1,
    ) -> bool:
        """持久化一份播放采样；暂时的数据库写冲突不能杀死 MPV 监听线程。"""

        for attempt in range(max(1, attempts)):
            try:
                V4PlaybackStore(self.database).save_progress(
                    str(session["work_id"]),
                    episode_id,
                    asset_id,
                    position,
                    duration,
                    completed,
                )
                return True
            except Exception as exc:
                # 写入端既可能遇到 SQLite 锁，也可能在用户清理/切换来源时暂时失效。
                # 旧版播放器保持会话存活并让后续事件继续落盘，V4 必须保留该语义。
                LOGGER.warning(
                    "MPV progress checkpoint failed (%s/%s): %s",
                    attempt + 1,
                    max(1, attempts),
                    exc,
                )
                if attempt + 1 < max(1, attempts):
                    time.sleep(0.1)
        return False

    def _force_checkpoint(self, session: dict) -> None:
        """在 MPV 退出时保存最后一份有效采样，并补齐完成同步。"""

        playlist = session.get("playlist") or []
        position = int(session.get("playlist_position") or 0)
        if not 0 <= position < len(playlist):
            return
        asset = playlist[position]
        elapsed = float(session.get("position") or 0.0)
        duration = float(session.get("duration") or 0.0)
        if duration <= 0:
            return
        completed = elapsed / duration >= 0.9
        saved = self._checkpoint_progress(
            session,
            str(asset.get("episode_id") or session["episode_id"]),
            str(asset["asset_id"]),
            elapsed,
            duration,
            completed,
            attempts=3,
        )
        if saved and completed:
            self._schedule_completion_sync(session, asset)

    def _monitor(self, process, session: dict) -> None:
        last_checkpoint_at = 0.0
        last_checkpoint_episode = ""
        next_checkpoint_retry_at = 0.0
        reconnect_delay = 0.25
        try:
            while process.poll() is None:
                try:
                    for event in observe_mpv_progress(session["ipc_server"]):
                        reconnect_delay = 0.25
                        if process.poll() is not None:
                            break
                        playlist = session.get("playlist") or []
                        playlist_position = _resolve_event_playlist_position(event, playlist)
                        if playlist_position is None:
                            continue
                        active_asset = playlist[playlist_position]
                        if playlist_position != int(session.get("playlist_position") or 0):
                            # MPV 已切到下一项时，上一集未必还能再发出最后一个
                            # 进度事件。先以会话中最后一份有效采样结算旧集，再替换
                            # 身份，避免丢失完成状态、Bangumi 投递和恢复位置。
                            self._force_checkpoint(session)
                            session["playlist_position"] = playlist_position
                            session["episode_id"] = active_asset.get("episode_id") or session["episode_id"]
                            session["asset_id"] = active_asset["asset_id"]
                            session["playback_locator"] = active_asset.get("playback_locator") or active_asset.get("source_locator") or ""
                            session["position"] = 0.0
                            session["duration"] = 0.0
                            set_mpv_playback_title(
                                session["ipc_server"], self._display_title(active_asset)
                            )
                            V4PlaybackStore(self.database).record_activation(
                                session["work_id"], session["episode_id"], session["asset_id"]
                            )
                            last_checkpoint_episode = ""
                        completed = event.duration > 0 and event.position / event.duration >= 0.9
                        active_episode_id = str(active_asset.get("episode_id") or session["episode_id"])
                        current_time = time.monotonic()
                        if (
                            active_episode_id != last_checkpoint_episode
                            or event.force_checkpoint
                            or completed
                            or current_time - last_checkpoint_at >= 5.0
                        ) and current_time >= next_checkpoint_retry_at:
                            saved = self._checkpoint_progress(
                                session,
                                active_episode_id,
                                active_asset["asset_id"],
                                event.position,
                                event.duration,
                                completed,
                            )
                            if saved:
                                last_checkpoint_at = current_time
                                last_checkpoint_episode = active_episode_id
                                next_checkpoint_retry_at = 0.0
                                if completed:
                                    self._schedule_completion_sync(session, active_asset)
                            else:
                                next_checkpoint_retry_at = current_time + 1.0
                        with self._lock:
                            if self._session and self._session["session_id"] == session["session_id"]:
                                self._session["position"] = event.position
                                self._session["duration"] = event.duration
                except (OSError, TimeoutError, ValueError, UnicodeDecodeError):
                    if process.poll() is not None:
                        break
                if process.poll() is None:
                    # MPV 进程的 IPC server 会在启动后稍晚出现，也可能在切文件时
                    # 短暂重建；保持旧版的有界退避，而不是首个连接失败就丢失整场会话。
                    time.sleep(reconnect_delay)
                    reconnect_delay = min(reconnect_delay * 2, 5.0)
        finally:
            try:
                self._force_checkpoint(session)
            except Exception:
                # 退出清理必须继续完成；下次播放仍可从已持久化检查点恢复。
                pass
            try:
                process.wait()
            except OSError:
                pass
            with self._lock:
                if self._process is process:
                    self._process = None
                    if self._session:
                        self._session["status"] = "exited"


_manager: V4PlaybackManager | None = None
_manager_database_path = ""


def get_v4_playback_manager(database: V4Database) -> V4PlaybackManager:
    global _manager, _manager_database_path
    path = str(database.path)
    if _manager is None or _manager_database_path != path:
        if _manager is not None:
            _manager.stop()
        _manager = V4PlaybackManager(database)
        _manager_database_path = path
    return _manager
