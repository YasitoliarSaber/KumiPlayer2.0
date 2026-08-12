# -*- coding: utf-8 -*-
"""网盘目录树路径推断与轻量验证。

只读取目录元数据并抽样检查文件是否存在，不打开视频、不计算哈希，避免对
挂载网盘产生高频读取。115 的路径规范保持原样；百度也只验证调用方明确给定的目录。
"""

import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from app.raw.models import RawSnapshot

_MAX_MEDIA_SAMPLES = 3


@dataclass
class PathValidationResult:
    source: str
    ok: bool
    status: str
    configured_root: str
    resolved_root: str
    scope_name: str = ""
    checked_count: int = 0
    existing_count: int = 0
    example_path: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def tree_scope_name(input_path: str) -> str:
    """从常见导出文件名中取得目录作用域，例如 ``01动画`` 或 ``新番``。"""
    stem = Path(input_path).stem.strip()
    stem = re.sub(
        r"[_\s-]*(?:文件目录|目录树)(?:[_\s-]*\d{6,})?$",
        "",
        stem,
        flags=re.IGNORECASE,
    ).strip()
    if not stem or re.fullmatch(r"根目录\d*", stem):
        return ""
    return stem


def _video_samples(snapshot: RawSnapshot) -> list:
    videos = [item for item in snapshot.files if item.resource_hint == "video"]
    if len(videos) <= _MAX_MEDIA_SAMPLES:
        return videos
    # 均匀取样，避免只检查同一作品或同一季的开头文件。
    indexes = {
        round(index * (len(videos) - 1) / (_MAX_MEDIA_SAMPLES - 1))
        for index in range(_MAX_MEDIA_SAMPLES)
    }
    return [videos[index] for index in sorted(indexes)]


def _candidate_score(root: Path, samples: Sequence) -> tuple[int, str]:
    candidates = []
    for item in samples:
        parts = item.source_path_parts or list(Path(item.relative_path).parts)
        candidates.append(root.joinpath(*parts))
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(candidates)))) as executor:
        states = list(executor.map(Path.is_file, candidates))
    existing_paths = [path for path, exists in zip(candidates, states, strict=True) if exists]
    existing = len(existing_paths)
    example = str(existing_paths[0]) if existing_paths else ""
    return existing, example


def _rebase_snapshot(snapshot: RawSnapshot, root: Path) -> None:
    snapshot.source_root = str(root)
    for item in snapshot.files:
        parts = item.source_path_parts or list(Path(item.relative_path).parts)
        item.source_root = str(root)
        item.real_path = str(root.joinpath(*parts))


def resolve_baidu_snapshot_root(
    snapshot: RawSnapshot,
    input_path: str,
    configured_root: str,
) -> PathValidationResult:
    """只验证调用方给定的百度内容根目录，不枚举或搜索挂载盘。"""
    configured = Path(configured_root).expanduser()
    scope = tree_scope_name(input_path)
    samples = _video_samples(snapshot)
    best_count, example = _candidate_score(configured, samples)
    _rebase_snapshot(snapshot, configured)

    if samples and best_count == len(samples):
        return PathValidationResult(
            source="baidu",
            ok=True,
            status="verified",
            configured_root=str(configured),
            resolved_root=str(configured),
            scope_name=scope,
            checked_count=len(samples),
            existing_count=best_count,
            example_path=example,
            message=f"已验证用户指定目录中的 {best_count}/{len(samples)} 个视频样本",
        )

    if best_count > 0:
        return PathValidationResult(
            source="baidu",
            ok=False,
            status="mismatch",
            configured_root=str(configured),
            resolved_root=str(configured),
            scope_name=scope,
            checked_count=len(samples),
            existing_count=best_count,
            example_path=example,
            message=(
                f"仅验证到 {best_count}/{len(samples)} 个视频样本，路径可能缺少上层目录或目录树已失效；"
                "已阻止生成镜像"
            ),
        )

    status = "unavailable" if not configured.is_dir() else "mismatch"
    return PathValidationResult(
        source="baidu",
        ok=False,
        status=status,
        configured_root=str(configured),
        resolved_root=str(configured),
        scope_name=scope,
        checked_count=len(samples),
        existing_count=0,
        example_path=str(configured.joinpath(*(samples[0].source_path_parts or []))) if samples else "",
        message=(
            "挂载目录不可用，已保留目录树预览，生成镜像前请先恢复挂载"
            if status == "unavailable"
            else "目录可访问，但抽样视频均不存在；已停止把该路径视为已验证"
        ),
    )


def validate_snapshot_paths(snapshot: RawSnapshot) -> PathValidationResult:
    """验证既有快照中的真实路径，供设置页与镜像生成共用。"""
    root = Path(snapshot.source_root).expanduser()
    samples = _video_samples(snapshot)
    sample_paths = [Path(item.real_path) for item in samples]
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(sample_paths)))) as executor:
        states = list(executor.map(Path.is_file, sample_paths))
    existing = [item for item, exists in zip(samples, states, strict=True) if exists]
    root_ok = root.is_dir()
    ok = root_ok and (not samples or len(existing) == len(samples))
    return PathValidationResult(
        source=snapshot.source,
        ok=ok,
        status="verified" if ok else ("unavailable" if not root_ok else "mismatch"),
        configured_root=str(root),
        resolved_root=str(root),
        checked_count=len(samples),
        existing_count=len(existing),
        example_path=(existing[0].real_path if existing else (samples[0].real_path if samples else "")),
        message=(
            f"挂载正常，已验证 {len(existing)}/{len(samples)} 个视频样本"
            if ok and samples
            else "挂载目录可访问，暂无可抽样的历史视频" if ok
            else "挂载目录不可访问"
            if not root_ok
            else f"挂载目录可访问，但仅找到 {len(existing)}/{len(samples)} 个视频样本"
        ),
    )


def validate_plan_media_paths(items: Sequence) -> tuple[bool, int, int]:
    """对正式镜像计划做有界抽样；只检查文件元数据，不读取媒体内容。"""
    videos = [
        item for item in items
        if item.resource_type == "video"
        and item.action == "generate_strm"
        and getattr(item, "availability", "available") == "available"
    ]
    if len(videos) > _MAX_MEDIA_SAMPLES:
        indexes = {
            round(index * (len(videos) - 1) / (_MAX_MEDIA_SAMPLES - 1))
            for index in range(_MAX_MEDIA_SAMPLES)
        }
        videos = [videos[index] for index in sorted(indexes)]
    paths = [Path(item.real_path) for item in videos]
    with ThreadPoolExecutor(max_workers=max(1, min(3, len(paths)))) as executor:
        states = list(executor.map(Path.is_file, paths))
    existing = sum(states)
    return bool(videos) and existing == len(videos), len(videos), existing
