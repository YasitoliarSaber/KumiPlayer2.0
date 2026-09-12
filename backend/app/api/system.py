"""System-level helpers exposed through V4 media identities."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.media_v4 import get_database
from app.core.paths import get_mirror_root
from app.media_v4.jobs.paths import work_directory_name

router = APIRouter(prefix="/api/system", tags=["system"])


class OpenFolderRequest(BaseModel):
    work_id: str
    episode_id: str = ""
    folder_type: Literal["video", "mirror"] = "video"
    open: bool = True


@router.post("/open-folder")
def open_folder(req: OpenFolderRequest):
    """Resolve a folder from a V4 Work/Episode/Asset identity.

    The frontend sends IDs only. No arbitrary filesystem path is accepted and
    only V4 Work/Episode/Asset rows are consulted.
    """

    if req.folder_type == "mirror":
        if not _work_is_active(req.work_id):
            raise HTTPException(status_code=404, detail="作品不存在或已从媒体库移除")
        folder = Path(get_mirror_root()).expanduser() / work_directory_name(req.work_id)
        source_path = folder
    else:
        asset = _find_asset(req.work_id, req.episode_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="作品或剧集没有关联 Asset")

        locator = asset["playback_locator"] or asset["source_locator"]
        if not locator or "://" in locator:
            return {
                "ok": True,
                "opened": False,
                "exists": False,
                "folder_path": "",
                "source_path": locator or "",
            }

        source_path = Path(locator).expanduser()
        folder = source_path if source_path.is_dir() else source_path.parent
    exists = folder.is_dir()
    if req.open and not exists:
        raise HTTPException(status_code=404, detail=f"文件夹不存在: {folder}")
    if req.open:
        _open_folder(folder)
    return {
        "ok": True,
        "opened": bool(req.open and exists),
        "exists": exists,
        "folder_path": str(folder),
        "source_path": str(source_path),
    }


def _find_asset(work_id: str, episode_id: str) -> dict | None:
    with get_database().connect() as conn:
        if episode_id:
            row = conn.execute(
                """
                SELECT a.source_locator, a.playback_locator
                FROM episodes e
                JOIN episode_assets ea ON ea.episode_id = e.episode_id
                JOIN assets a ON a.asset_id = ea.asset_id
                WHERE e.work_id = ? AND e.episode_id = ?
                ORDER BY ea.preference_rank, a.asset_id
                LIMIT 1
                """,
                (work_id, episode_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT a.source_locator, a.playback_locator
                FROM episodes e
                JOIN episode_assets ea ON ea.episode_id = e.episode_id
                JOIN assets a ON a.asset_id = ea.asset_id
                WHERE e.work_id = ?
                ORDER BY e.season_id, e.local_episode_number, ea.preference_rank, a.asset_id
                LIMIT 1
                """,
                (work_id,),
            ).fetchone()
    return dict(row) if row is not None else None


def _work_is_active(work_id: str) -> bool:
    with get_database().connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM works WHERE work_id = ? AND status = 'active'",
            (work_id,),
        ).fetchone()
    return row is not None


def _open_folder(folder: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(folder))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
        return
    subprocess.Popen(["xdg-open", str(folder)])
