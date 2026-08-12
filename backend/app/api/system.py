# -*- coding: utf-8 -*-
"""System-level helpers exposed through safe IDs."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.integrations.bangumi import resolve_episode, resolve_work

router = APIRouter(prefix="/api/system", tags=["system"])


class OpenFolderRequest(BaseModel):
    work_id: str
    episode_id: str = ""
    folder_type: Literal["video", "mirror"] = "video"
    open: bool = True


@router.post("/open-folder")
def open_folder(req: OpenFolderRequest):
    """Open or resolve a real media folder for a known work/episode.

    The request accepts library IDs only.  It never opens an arbitrary path from
    the frontend.
    """
    try:
        if req.folder_type == "mirror":
            folder, source_path = _resolve_mirror_folder(req.work_id, req.episode_id)
        else:
            folder, source_path = _resolve_folder(req.work_id, req.episode_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    exists = folder.exists()
    if req.open:
        if not exists:
            raise HTTPException(status_code=404, detail=f"文件夹不存在: {folder}")
        _open_folder(folder)

    return {
        "ok": True,
        "opened": bool(req.open and exists),
        "exists": exists,
        "folder_path": str(folder),
        "source_path": str(source_path) if source_path else "",
    }


def _resolve_folder(work_id: str, episode_id: str) -> tuple[Path, Path | None]:
    if episode_id:
        return _folder_from_strm(_resolve_episode_strm(work_id, episode_id))

    work = resolve_work(work_id)
    if work.episodes:
        return _folder_from_strm(work.episodes[0].strm_path)
    if work.dir_path:
        return Path(work.dir_path), Path(work.dir_path)
    raise LookupError(f"作品没有可打开的视频文件夹: {work_id}")


def _resolve_mirror_folder(work_id: str, episode_id: str) -> tuple[Path, Path | None]:
    if episode_id:
        strm_path = _resolve_episode_strm(work_id, episode_id)
        if not strm_path:
            raise LookupError("剧集没有关联 .strm 文件")
        strm = Path(strm_path)
        return strm.parent, strm

    work = resolve_work(work_id)
    if work.dir_path:
        mirror_dir = Path(work.dir_path)
        source = next(
            (Path(episode.strm_path) for episode in work.episodes if episode.strm_path),
            mirror_dir,
        )
        return mirror_dir, source

    for episode in work.episodes:
        if episode.strm_path:
            strm = Path(episode.strm_path)
            return strm.parent, strm
    raise LookupError(f"作品没有可打开的镜像文件夹: {work_id}")


def _resolve_episode_strm(work_id: str, episode_id: str) -> str:
    try:
        _, episode = resolve_episode(episode_id, work_id)
        return episode.strm_path
    except LookupError:
        work = resolve_work(work_id)
        for location in (work.source_locations or {}).values():
            if not isinstance(location, dict):
                continue
            if location.get("episode_id") == episode_id:
                return str(location.get("strm_path") or "")
        raise


def _folder_from_strm(strm_path: str) -> tuple[Path, Path | None]:
    if not strm_path:
        raise LookupError("剧集没有关联 .strm 文件")

    strm = Path(strm_path)
    source: Path | None = strm
    try:
        real_path = strm.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        real_path = ""

    if real_path:
        source = Path(real_path)
        return source.parent, source
    return strm.parent, source


def _open_folder(folder: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(str(folder))  # type: ignore[attr-defined]
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
        return
    subprocess.Popen(["xdg-open", str(folder)])
