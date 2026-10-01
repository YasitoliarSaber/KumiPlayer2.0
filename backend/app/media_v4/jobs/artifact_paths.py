"""C-006 产物目标名的唯一实现。

镜像与元数据消费者只能通过本模块计算目标路径；禁止在别处拼接
``Season xx`` / ``Specials`` / ``S00E00``，也禁止用 fingerprint 尾串、
截短 ID 或标题当路径证明。

两类目标名：

- ``mirror_relative_path``：``<work_dir>[/Season xx]/<stem>-<完整标签>.strm``，
  标签是完整 ``SHA-256(canonical_json(asset_id, confirmed_playback_locator))``，
  因此同资产同定位符路径稳定，真实改名/定位符变动自然换名而不会互相覆盖。
- ``metadata_relative_path``：资料代次目录 ``<work_dir>/.metadata/<snapshot_id>/``
  下的文件；没有候选 snapshot 时退回旧的就地布局（显式 legacy 分支），
  但未知季集绝不再写 ``Specials`` / ``S00E00``。
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from urllib.parse import urlparse

from app.media_v4.domain.identity import (
    CONTENT_CLASS_MOVIE,
    CONTENT_CLASS_PLAYABLE_SPECIAL,
    NON_IMPORTABLE_CONTENT_CLASSES,
    SEASON_KIND_REGULAR,
    canonical_json,
)
from app.media_v4.jobs.paths import work_directory_name

SEASON_KIND_SPECIAL = "special"

UNASSIGNED_DIRECTORY = "Unassigned"
SPECIALS_DIRECTORY = "Specials"
METADATA_DIRECTORY = ".metadata"

UNKNOWN_STEM = "U"
MOVIE_STEM = "movie"

# 相对受管根的相对名长度上限。镜像根由用户配置、不属于本模块能改写的命名，
# 因此只约束我们真正控制的相对名（Work UUID 目录 + 固定标签）。
MAX_TARGET_PATH_LENGTH = 200

_SAFE_FILENAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class ArtifactPathInvalid(RuntimeError):
    """目标相对名在平台限制下仍不合法；调用方应记录 ``artifact_path_invalid``。"""

    code = "artifact_path_invalid"


class NonMaterializableArtifact(RuntimeError):
    """附属/特别篇等没有对应 binding 的类别，不生成产物。"""

    code = "legacy_excluded"


def asset_label(asset_id: str, confirmed_playback_locator: str) -> str:
    """完整稳定标签：``SHA-256(canonical_json(asset_id, locator))``。

    永不使用 fingerprint 尾串或截短 ID：stat/size/mtime 指纹会在同一时间窗
    碰撞，截短 ID 不足以作为唯一证明。
    """

    payload = canonical_json([str(asset_id or ""), str(confirmed_playback_locator or "")])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def locator_digest(playback_locator: str) -> str:
    """产物 digest：直接由写入的 locator 字节推出，便于引用关系做完整性复查。"""

    return hashlib.sha256(str(playback_locator or "").encode("utf-8")).hexdigest()


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = "".join(
        char if char in _SAFE_FILENAME_CHARS else "_" for char in str(value or "")
    ).strip(" ._")
    return cleaned or fallback


#: 相对名（可含目录层）允许的安全字符：Windows 允许路径段内空格，而 ``Season 02``
#: 是 C-006 规定的布局，不能被下划线替换掉。
_SAFE_RELATIVE_CHARS = _SAFE_FILENAME_CHARS | frozenset(" ")


def _safe_relative_name(value: str) -> str:
    """逐段安全化相对名，保留 ``Season 02`` 这类带空格的目录层。"""

    parts = [
        part.strip()
        for part in str(value or "").replace("\\", "/").split("/")
        if part.strip() not in {"", ".", ".."}
    ]
    cleaned: list[str] = []
    for part in parts:
        safe = "".join(
            char if char in _SAFE_RELATIVE_CHARS else "_" for char in part
        ).strip(" ._")
        if safe:
            cleaned.append(safe)
    return "/".join(cleaned) or "artifact"


def season_directory(*, season_kind: str | None, local_season_number: int | None) -> str:
    """已知普通季 → ``Season xx``；明确特别篇 → ``Specials``；其余 → ``Unassigned``。"""

    if season_kind == SEASON_KIND_SPECIAL:
        return SPECIALS_DIRECTORY
    if season_kind == SEASON_KIND_REGULAR and local_season_number is not None:
        try:
            number = int(local_season_number)
        except (TypeError, ValueError):
            number = 0
        # C-002/C-008：旧 ``regular/0`` 不是已证明特别篇，不得按 0 重新分类，
        # 也不得写成 S00E00；按未知季处理。
        if number > 0:
            return f"Season {number:02d}"
    return UNASSIGNED_DIRECTORY


def episode_stem(
    *,
    season_kind: str | None = None,
    local_season_number: int | None = None,
    local_episode_number: int | None = None,
    absolute_episode_number: int | None = None,
    special_number: int | None = None,
) -> str:
    """季集文件名主干：``S02E13`` / ``E13`` / ``ABS0013`` / ``U``。"""

    if season_kind == SEASON_KIND_SPECIAL:
        return f"SP{int(special_number or local_episode_number or 1):02d}"
    if local_episode_number is not None:
        try:
            episode = int(local_episode_number)
        except (TypeError, ValueError):
            episode = 0
        if season_kind == SEASON_KIND_REGULAR and episode > 0:
            season = int(local_season_number) if local_season_number is not None else 0
            if season > 0:
                return f"S{season:02d}E{episode:02d}"
        if episode > 0:
            return f"E{episode:02d}"
    if absolute_episode_number is not None:
        try:
            absolute = int(absolute_episode_number)
        except (TypeError, ValueError):
            absolute = 0
        if absolute > 0:
            return f"ABS{absolute:04d}"
    return UNKNOWN_STEM


def _coordinate_directory(
    *,
    season_kind: str | None,
    local_season_number: int | None,
    local_episode_number: int | None,
) -> str:
    """只有“明确普通季 + 真实局部集号”才进 ``Season xx``。

    只有绝对编号、未知季或未知集都不进季目录：C-006 要求它们落在
    ``Unassigned``，由完整 Asset 标签区分，不能复用 ``S00E00``。
    """

    if season_kind == SEASON_KIND_SPECIAL:
        return SPECIALS_DIRECTORY
    if local_episode_number is None:
        return UNASSIGNED_DIRECTORY
    try:
        has_episode = int(local_episode_number) > 0
    except (TypeError, ValueError):
        has_episode = False
    if not has_episode:
        return UNASSIGNED_DIRECTORY
    return season_directory(season_kind=season_kind, local_season_number=local_season_number)


def mirror_relative_path(
    *,
    work_id: str,
    asset_id: str,
    playback_locator: str,
    is_movie: bool = False,
    content_class: str | None = None,
    season_kind: str | None = None,
    local_season_number: int | None = None,
    local_episode_number: int | None = None,
    absolute_episode_number: int | None = None,
    special_number: int | None = None,
) -> str:
    """镜像相对路径（POSIX 分隔）；调用方负责再拼受管镜像根。"""

    work_dir = work_directory_name(work_id)
    label = asset_label(asset_id, playback_locator)
    if is_movie or content_class == CONTENT_CLASS_MOVIE:
        return f"{work_dir}/{MOVIE_STEM}-{label}.strm"
    if content_class in NON_IMPORTABLE_CONTENT_CLASSES or (
        season_kind == SEASON_KIND_SPECIAL and content_class != CONTENT_CLASS_PLAYABLE_SPECIAL
    ):
        raise NonMaterializableArtifact("附属/特别篇内容没有对应 binding，不生成镜像")
    directory = _coordinate_directory(
        season_kind=season_kind,
        local_season_number=local_season_number,
        local_episode_number=local_episode_number,
    )
    stem = episode_stem(
        season_kind=season_kind,
        local_season_number=local_season_number,
        local_episode_number=local_episode_number,
        absolute_episode_number=absolute_episode_number,
        special_number=special_number,
    )
    return f"{work_dir}/{directory}/{stem}-{label}.strm"


def metadata_relative_path(*, work_id: str, filename: str, snapshot_id: str | None = None) -> str:
    """元数据相对路径：有代次时落在 ``.metadata/<snapshot_id>/``，否则退回就地布局。"""

    work_dir = work_directory_name(work_id)
    safe_name = _safe_relative_name(filename)
    if snapshot_id:
        generation = _safe_segment(snapshot_id, "generation")
        return f"{work_dir}/{METADATA_DIRECTORY}/{generation}/{safe_name}"
    return f"{work_dir}/{safe_name}"


def work_nfo_relative_path(*, work_id: str, is_movie: bool, snapshot_id: str | None = None) -> str:
    if snapshot_id:
        return metadata_relative_path(work_id=work_id, filename="work.nfo", snapshot_id=snapshot_id)
    return metadata_relative_path(
        work_id=work_id, filename="movie.nfo" if is_movie else "tvshow.nfo"
    )


def episode_nfo_relative_path(
    *,
    work_id: str,
    episode_id: str | None = None,
    snapshot_id: str | None = None,
    season_kind: str | None = None,
    local_season_number: int | None = None,
    local_episode_number: int | None = None,
    absolute_episode_number: int | None = None,
    special_number: int | None = None,
) -> str:
    if snapshot_id:
        safe_episode = _safe_segment(str(episode_id or ""), "episode")
        return metadata_relative_path(
            work_id=work_id, filename=f"episode-{safe_episode}.nfo", snapshot_id=snapshot_id
        )
    stem = episode_stem(
        season_kind=season_kind,
        local_season_number=local_season_number,
        local_episode_number=local_episode_number,
        absolute_episode_number=absolute_episode_number,
        special_number=special_number,
    )
    directory = _coordinate_directory(
        season_kind=season_kind,
        local_season_number=local_season_number,
        local_episode_number=local_episode_number,
    )
    return metadata_relative_path(work_id=work_id, filename=f"{directory}/{stem}.nfo")


def artwork_relative_path(*, work_id: str, filename: str, snapshot_id: str | None = None) -> str:
    return metadata_relative_path(work_id=work_id, filename=filename, snapshot_id=snapshot_id)


def episode_thumb_relative_path(
    *,
    work_id: str,
    source_url: str,
    snapshot_id: str | None = None,
    season_kind: str | None = None,
    local_season_number: int | None = None,
    local_episode_number: int | None = None,
    absolute_episode_number: int | None = None,
    special_number: int | None = None,
) -> str:
    stem = episode_stem(
        season_kind=season_kind,
        local_season_number=local_season_number,
        local_episode_number=local_episode_number,
        absolute_episode_number=absolute_episode_number,
        special_number=special_number,
    )
    suffix = PurePosixPath(urlparse(str(source_url or "")).path).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    if snapshot_id:
        return metadata_relative_path(
            work_id=work_id, filename=f"episode-{stem}-thumb{suffix}", snapshot_id=snapshot_id
        )
    directory = _coordinate_directory(
        season_kind=season_kind,
        local_season_number=local_season_number,
        local_episode_number=local_episode_number,
    )
    return metadata_relative_path(work_id=work_id, filename=f"{directory}/{stem}-thumb{suffix}")


def guard_target_path(relative_path: str) -> str:
    """过长的相对目标名不改写真实源路径，而是明确失败 ``artifact_path_invalid``。"""

    if len(str(relative_path)) > MAX_TARGET_PATH_LENGTH:
        raise ArtifactPathInvalid(
            f"artifact_path_invalid: 产物目标名超过平台允许长度 ({len(str(relative_path))})"
        )
    return str(relative_path)
