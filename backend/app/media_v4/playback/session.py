"""基于 V4 Asset 身份的单实例 MPV 会话管理。"""

from __future__ import annotations

import os
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.core.paths import get_data_dir
from app.media_v4.persistence.database import V4Database
from app.media_v4.playback.store import V4PlaybackStore
from app.playback.mpv import start_mpv
from app.playback.mpv_ipc import make_ipc_server_name, observe_mpv_progress, send_mpv_quit


def _now() -> str:
    return datetime.now(UTC).isoformat()


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
            playlist_paths=[locator],
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
            "started_at": _now(),
        }
        with self._lock:
            self._process = process
            self._session = session
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
                return dict(row)
            row = conn.execute(
                """
                SELECT a.*, w.preferred_title, s.local_season_number,
                       e.local_episode_number, e.special_number, e.episode_kind
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
        return dict(row)

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
        title = str(asset["preferred_title"] or "KumiPlayer")
        if asset["episode_kind"] == "movie":
            return title
        season = int(asset["local_season_number"] or 0)
        episode = int(asset["local_episode_number"] or 0)
        if asset["episode_kind"] == "special" or season == 0:
            return f"{title} - SP{int(asset['special_number'] or 1):02d}"
        return f"{title} - S{season:02d}E{episode:02d}"

    def _monitor(self, process, session: dict) -> None:
        try:
            for event in observe_mpv_progress(session["ipc_server"]):
                if process.poll() is not None:
                    break
                completed = event.duration > 0 and event.position / event.duration >= 0.9
                V4PlaybackStore(self.database).save_progress(
                    session["work_id"],
                    session["episode_id"],
                    session["asset_id"],
                    event.position,
                    event.duration,
                    completed,
                )
                with self._lock:
                    if self._session and self._session["session_id"] == session["session_id"]:
                        self._session["position"] = event.position
                        self._session["duration"] = event.duration
        except (OSError, TimeoutError, ValueError, UnicodeDecodeError):
            pass
        finally:
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
