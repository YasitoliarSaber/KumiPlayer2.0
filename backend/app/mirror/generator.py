# -*- coding: utf-8 -*-
"""镜像生成器

读取 confirmed ImportPlan，为 video + generate_strm 条目生成 .strm 镜像。
不重新识别媒体结构，不调用 TMDB / DeepSeek，不扫描媒体库。
"""

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from app.core.paths import get_mirror_root as _get_mirror_root_path
from app.core.paths import safe_join, sanitize_filename
from app.import_plan.models import ImportPlan, ImportPlanItem
from app.import_plan.store import save_import_plan
from app.mirror.result import MirrorGenerateResult, MirrorItemResult

# 来源 → 镜像命名空间映射
_SOURCE_NAMESPACE = {
    "pan115": "115",
    "baidu": "baidu",
    "local": "local",
    "openlist": "openlist",
}


def _get_mirror_root(mirror_root: Optional[str] = None) -> Path:
    """获取镜像根目录（委托给 core.paths 统一推导）"""
    return _get_mirror_root_path(mirror_root)


def _get_source_namespace(source: str) -> str:
    """获取来源的镜像命名空间"""
    return _SOURCE_NAMESPACE.get(source, sanitize_filename(source))


def _build_work_dir(item: ImportPlanItem) -> str:
    """构建作品目录名

    主系列（main_series）：用 series_group 聚合，如 "CLANNAD"
    独立卡片（standalone）：用 work_title (year)，如 "剧场版 (2017)"
    （card_type 已由 revision 持久化恢复，镜像阶段可安全依赖）
    """
    if item.card_type == "main_series" and item.series_group:
        return sanitize_filename(item.series_group)
    title = sanitize_filename(_movie_display_title(item) if item.group_type == "movie" else item.work_title)
    if item.year:
        return f"{title} ({item.year})"
    return title


def _movie_display_title(item: ImportPlanItem) -> str:
    """Prefer the concrete movie title for standalone movie cards."""
    filename_title = _movie_filename_title(item.relative_path)
    for raw in (item.title, filename_title, item.original_title, item.work_title):
        cleaned = _clean_standalone_movie_title(raw)
        if cleaned:
            return cleaned
    return item.work_title


def _clean_standalone_movie_title(raw: str) -> str:
    """Clean release-folder noise while preserving the concrete movie title."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    cleaned = re.sub(r"^\d+[.．]\s*", "", cleaned).strip()
    cleaned = re.sub(r"[.．]\d{4}$", "", cleaned).strip()
    cleaned = re.sub(r"[(\（]\d{4}[)\）]$", "", cleaned).strip()
    try:
        from app.recognition.title_cleaner import clean_work_title_container

        cleaned = clean_work_title_container(cleaned).title or cleaned
    except Exception:
        cleaned = re.sub(r"\[[^\]]*(?:sub|raws?|vcb|studio|nekomoe|ai\-?rota)[^\]]*\]", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\[[^\]]*(?:ma\d+p|hi\d+p|\d{3,4}p|x26[45]|hevc|avc|flac|aac|hdr|sdr|dovi)[^\]]*\]", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^\d+[.．]\s*", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-_")
    return cleaned


def _movie_filename_title(relative_path: str) -> str:
    filename = Path((relative_path or "").replace("\\", "/")).name
    stem = re.sub(r"\.[^.]+$", "", filename)
    tokens = [m.group(1).strip() for m in re.finditer(r"\[([^\]]+)\]", stem)]
    movie_positions = [
        idx for idx, token in enumerate(tokens)
        if re.fullmatch(r"movies?", token, flags=re.IGNORECASE)
    ]
    if not movie_positions:
        return ""
    ordered_indexes = []
    for pos in movie_positions:
        ordered_indexes.extend([pos - 1, pos + 1])
    ordered_indexes.extend(range(len(tokens)))
    seen = set()
    for idx in ordered_indexes:
        if idx < 0 or idx >= len(tokens) or idx in seen:
            continue
        seen.add(idx)
        title = _clean_movie_filename_token(tokens[idx])
        if title:
            return title
    return ""


def _clean_movie_filename_token(token: str) -> str:
    cleaned = (token or "").replace("_", " ").strip()
    lower = cleaned.casefold()
    compact = re.sub(r"[\s._-]+", "", lower)
    tech_tokens = {
        "movie", "movies", "bdrip", "bluray", "bd", "webdl", "webrip",
        "chs", "cht", "jpn", "eng", "mp4", "mkv", "avc", "aac", "flac",
        "x264", "x265", "h264", "h265", "hevc", "hi10p", "ma10p",
    }
    if not cleaned or lower in tech_tokens or compact in tech_tokens:
        return ""
    if "&" in cleaned or "＆" in cleaned:
        return ""
    if re.fullmatch(r"\d+\s*[-~]\s*\d+(?:\s*\+\s*movies?)?", cleaned, flags=re.IGNORECASE):
        return ""
    if re.fullmatch(r"\d{3,4}p", cleaned, flags=re.IGNORECASE):
        return ""
    if re.search(r"\b(?:bdrip|bluray|web[- ]?dl|webrip|hevc|avc|aac|flac|x26[45]|h\.?26[45])\b", cleaned, flags=re.IGNORECASE):
        return ""
    if not re.search(r"[A-Za-z\u4e00-\u9fff]", cleaned):
        return ""
    return " ".join(cleaned.split()).strip(" .-_")


def _build_group_dir(item: ImportPlanItem) -> Optional[str]:
    """构建分组目录名

    season  -> Season {season_number}
    special -> Season 0
    auxiliary -> Extras
    movie  -> None（直接放作品根目录）
    """
    if item.group_type == "season":
        if item.season_number is not None:
            return f"Season {item.season_number}"
        return "Season 1"  # fallback
    if item.group_type in {"special", "sps"}:
        return "Season 0"
    if item.group_type == "auxiliary":
        return "Extras"
    if item.group_type == "op_ed":
        return None
    # movie 和其他：不创建子目录
    return None


def _clean_original_name(item: ImportPlanItem) -> str:
    """从 relative_path 提取原始文件名，做轻量清洗。

    Season 0 / 特殊级必须保留原始命名里的内容差异，例如 TV SPOT 01、
    OVA01、Preview02。这里只去掉字幕组和压制参数，不再把所有 [] 标签抹掉。
    """
    # 提取文件名
    filename = item.relative_path.replace("\\", "/").split("/")[-1]
    # 去掉扩展名
    dot_idx = filename.rfind(".")
    if dot_idx > 0:
        filename = filename[:dot_idx]

    cleaned = filename

    # 去掉常见字幕组/压制组前缀，但保留后续内容标签。
    cleaned = re.sub(r"^\[[^\]]*(?:sub|raws?|vcb|studio|dmg|tudo|mai|ye|bean|fzsd|lp)[^\]]*\]\s*", "", cleaned, flags=re.IGNORECASE)

    tech_token = (
        r"(?:BDRip|WEB-?DL|WebRip|HDRip|BluRay|BDMV|BDRemux|"
        r"1080[pP]|2160[pP]|720[pP]|4K|HEVC|AVC|H\.?264|H\.?265|"
        r"x26[45]|Ma\d+p?|FLAC|AAC|AC3|ASS|CHS|CHT|JPN|BIG5|GB|"
        r"简体|繁体|内嵌|外挂|Bilibili_[A-Za-z0-9]+)"
    )

    # 只删除纯技术类 [] / () 标签；TV SPOT、Preview、OVA、Event 等内容标签保留。
    cleaned = re.sub(rf"\[(?=[^\]]*{tech_token})[^\]]*\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(rf"\((?=[^\)]*{tech_token})[^\)]*\)", "", cleaned, flags=re.IGNORECASE)
    # 真实目录树里偶尔有未闭合技术标签，例如 [x265_flac5.1_ass.mkv。
    cleaned = re.sub(rf"\[(?=[^\]]*{tech_token})[^\]]*$", "", cleaned, flags=re.IGNORECASE)

    # 清理多余空格和标点
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+([\]\)])", r"\1", cleaned)
    cleaned = re.sub(r"([\[\(])\s+", r"\1", cleaned)
    cleaned = cleaned.strip(" .-_")

    # 如果清理后为空，用原始文件名
    if not cleaned:
        cleaned = filename

    return sanitize_filename(cleaned)


def _conflict_suffix(item: ImportPlanItem) -> str:
    """为同名目标生成可读、稳定的冲突后缀。"""
    raw = _clean_original_name(item)
    title = sanitize_filename(_movie_display_title(item) if item.group_type == "movie" else item.work_title)
    suffix = raw
    if title and suffix.lower().startswith(title.lower()):
        suffix = suffix[len(title):].strip(" .-_")
    suffix = suffix or raw or item.id
    suffix = suffix[:80].strip(" .-_")
    digest = hashlib.sha1((item.relative_path or item.id).encode("utf-8")).hexdigest()[:8]
    if not suffix:
        suffix = digest
    return sanitize_filename(f"{suffix} {digest}")


def _with_filename_suffix(path: Path, suffix: str) -> Path:
    stem = path.stem
    ext = path.suffix or ".strm"
    return path.with_name(sanitize_filename(f"{stem} - {suffix}") + ext)


def _build_strm_filename(
    item: ImportPlanItem,
    sp_sequence: Dict[str, int],
    oped_sequence: Dict[str, int],
) -> str:
    """构建 .strm 文件名

    season: {work} - S01E01 - {title}.strm
    special/auxiliary: 保留原始名称（清理技术标签后）
    movie:  {work} (year).strm
    """
    title = sanitize_filename(_movie_display_title(item) if item.group_type == "movie" else item.work_title)

    if item.group_type == "season":
        s = item.season_number or 1
        e = item.episode_number or 1
        if item.title:
            ep_title = sanitize_filename(item.title)
            return f"{title} - S{s:02d}E{e:02d} - {ep_title}.strm"
        return f"{title} - S{s:02d}E{e:02d}.strm"

    if item.group_type in {"special", "sps"}:
        # 保留原始名称，清理技术标签
        original = _clean_original_name(item)
        return f"{original}.strm"

    if item.group_type == "auxiliary":
        original = _clean_original_name(item)
        return f"{original}.strm"

    if item.group_type == "movie":
        if item.year:
            return f"{title} ({item.year}).strm"
        return f"{title}.strm"

    # fallback
    return f"{title}.strm"


def _compute_sequence(items: List[ImportPlanItem], group_type: str) -> Dict[str, int]:
    """计算分组内的稳定序号（按 relative_path 排序）"""
    filtered = [
        item for item in items
        if item.group_type == group_type
        and item.resource_type == "video"
        and item.action == "generate_strm"
    ]
    sorted_items = sorted(filtered, key=lambda i: i.relative_path)
    return {item.id: idx + 1 for idx, item in enumerate(sorted_items)}


def build_target_for_item(
    item: ImportPlanItem,
    mirror_root: Path,
    source_ns: str,
    sp_sequence: Dict[str, int],
    oped_sequence: Dict[str, int],
) -> Tuple[Path, str, str]:
    """为单个 item 生成目标路径

    返回:
        (target_strm_path, target_dir, target_filename)
    """
    work_dir = _build_work_dir(item)
    group_dir = _build_group_dir(item)
    filename = _build_strm_filename(item, sp_sequence, oped_sequence)

    # 构建路径组件
    parts = [source_ns, work_dir]
    if group_dir:
        parts.append(group_dir)
    parts.append(filename)

    # 安全拼接
    target_path = safe_join(mirror_root, *parts)
    target_dir = str(target_path.parent)
    target_filename = target_path.name

    return target_path, target_dir, target_filename


def _validate_item(item: ImportPlanItem) -> Optional[str]:
    """校验 item 字段是否完整，返回错误信息或 None

    镜像生成器只执行 confirmed import_plan，不替计划补脑。
    缺失/无效字段应让 item failed，不写 .strm。
    """
    if not item.work_title:
        return "work_title 为空"

    valid_group_types = {"season", "special", "movie", "sps", "op_ed", "auxiliary"}
    if item.group_type not in valid_group_types:
        return f"group_type 无效: '{item.group_type}'，必须是 {valid_group_types} 之一"
    if item.group_type == "op_ed":
        return "group_type=op_ed 已废弃，OP/ED 应跳过不生成镜像"

    if item.group_type == "season":
        if item.season_number is None:
            return "group_type=season 但 season_number 为空"
        if item.episode_number is None:
            return "group_type=season 但 episode_number 为空"

    return None


def _check_strm_conflict(target_path: Path, content: str) -> Tuple[bool, str]:
    """检查 .strm 文件冲突

    返回:
        (should_write, message)
        should_write=True 表示可以写入
        should_write=False 表示冲突
    """
    if not target_path.exists():
        return True, ""

    # 文件已存在，检查内容
    try:
        existing_content = target_path.read_text(encoding="utf-8").strip()
    except (IOError, UnicodeDecodeError):
        return False, f"无法读取已存在的 .strm 文件: {target_path}"

    if existing_content == content.strip():
        # 内容相同，跳过
        return False, "内容相同，跳过"

    # 旧版本可能把缺少上层目录的路径写进受控镜像。只有旧目标已经不可达、
    # 且新计划目标确实可达时才允许修复；两边都有效或两边都无效仍按冲突处理，
    # 避免静默覆盖用户仍在使用的有效镜像。
    existing_target = Path(existing_content).expanduser()
    replacement_target = Path(content.strip()).expanduser()
    if (
        existing_target.is_absolute()
        and replacement_target.is_absolute()
        and not existing_target.is_file()
        and replacement_target.is_file()
    ):
        return True, "旧镜像路径不可达，已按当前已验证路径修复"

    # 内容不同，冲突
    return False, f"目标 .strm 已存在且内容不同: {target_path}"


def _current_target_label(item: ImportPlanItem) -> str:
    """生成用户可读的当前处理对象标签（仅供进度 patch 使用）。

    season: 作品名 · 第 N 季 · 第 M 集（集数未知则省略该段）
    special/sps: 作品名 · 特别篇 N
    movie: 具体电影名
    fallback: work_title、series_group、title、文件名依次取第一个非空值
    """
    if item is None:
        return ""
    title = item.work_title or item.series_group or item.title or ""
    if item.group_type == "season":
        if item.season_number is not None and item.episode_number is not None:
            return f"{title} · 第 {item.season_number} 季 · 第 {item.episode_number} 集"
        if item.season_number is not None:
            return f"{title} · 第 {item.season_number} 季"
        return title
    if item.group_type in {"special", "sps"}:
        if item.special_number is not None:
            return f"{title} · 特别篇 {item.special_number}"
        return f"{title} · 特别篇"
    if item.group_type == "movie":
        return _movie_display_title(item) or title
    filename = (item.relative_path or "").replace("\\", "/").split("/")[-1]
    return next((value for value in (item.work_title, item.series_group, item.title, filename) if value), "")


def generate_mirror(
    plan: ImportPlan,
    mirror_root: Optional[str] = None,
    update_latest: bool = True,
    progress_callback: Optional[Callable[[int, str, Optional[dict]], None]] = None,
) -> MirrorGenerateResult:
    """生成镜像

    读取 confirmed ImportPlan，为 video + generate_strm 条目生成 .strm。

    参数:
        plan: confirmed 状态的 ImportPlan
        mirror_root: 镜像根目录，为空时从配置读取

    返回:
        MirrorGenerateResult
    """
    # 校验 plan.status
    if plan.status != "confirmed":
        return MirrorGenerateResult(
            plan_id=plan.plan_id,
            source=plan.source,
            status="failed",
            errors=[f"plan.status 必须是 confirmed，当前为 {plan.status}"],
        )

    root = _get_mirror_root(mirror_root)
    source_ns = _get_source_namespace(plan.source)

    all_video_items = [
        item for item in plan.items
        if item.resource_type == "video"
    ]

    ignored_items = [
        item for item in all_video_items
        if item.action != "generate_strm" or item.group_type in {"ignored", "op_ed"}
    ]

    # 过滤 video + generate_strm
    video_items = [
        item for item in all_video_items
        if item.action == "generate_strm"
        and item.group_type not in {"ignored", "op_ed"}
        and getattr(item, "availability", "available") == "available"
    ]

    if not video_items:
        if ignored_items:
            return MirrorGenerateResult(
                plan_id=plan.plan_id,
                source=plan.source,
                mirror_root=str(root),
                status="success",
                skipped_count=len(ignored_items),
                items=[
                    MirrorItemResult(
                        item_id=item.id,
                        raw_file_id=item.raw_file_id,
                        source=plan.source,
                        status="skipped",
                        strm_path="",
                        real_path=item.real_path,
                        message="按规则跳过，不生成镜像",
                    )
                    for item in ignored_items
                ],
            )
        return MirrorGenerateResult(
            plan_id=plan.plan_id,
            source=plan.source,
            mirror_root=str(root),
            status="failed",
            errors=["没有 video + generate_strm 的条目"],
        )

    total_items = len(ignored_items) + len(video_items)
    initial_progress = {
        "generated_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "items_count": total_items,
        "processed_count": 0,
    }

    # 正式网盘镜像写入前做轻量抽样。显式 mirror_root 是测试/离线预演入口，
    # 保持现有可测试性；生产入口不得把完全失效的挂载路径写进 .strm。
    if mirror_root is None and plan.source in {"pan115", "baidu", "openlist"}:
        from app.sources.path_validation import validate_plan_media_paths

        if progress_callback:
            progress_callback(8, "正在抽样验证最多 3 个代表视频", {**initial_progress, "log_kind": "info"})
        paths_ok, checked_count, existing_count = validate_plan_media_paths(video_items)
        if not paths_ok:
            detail = (
                "OpenList 本地挂载路径不可访问，请检查挂载后重试。"
                if plan.source == "openlist"
                else "请在设置中验证挂载路径后再生成镜像。"
            )
            return MirrorGenerateResult(
                plan_id=plan.plan_id,
                source=plan.source,
                mirror_root=str(root),
                status="failed",
                errors=[
                    "媒体路径验证失败："
                    f"抽样 {checked_count} 个视频，找到 {existing_count} 个。"
                    f"{detail}"
                ],
            )
        if progress_callback:
            progress_callback(
                14,
                f"视频样本验证完成：{existing_count}/{checked_count}",
                {**initial_progress, "validation_checked_count": checked_count, "log_kind": "done"},
            )

    if progress_callback:
        progress_callback(18, "正在检查镜像目标与文件名冲突", {**initial_progress, "log_kind": "info"})

    # 预计算 SP / OP-ED 稳定序号
    sp_sequence = _compute_sequence(plan.items, "sps")
    oped_sequence = _compute_sequence(plan.items, "op_ed")

    # 检测目标路径冲突（在写入前）
    target_paths: Dict[str, List[str]] = defaultdict(list)
    for item in video_items:
        target_path, _, _ = build_target_for_item(
            item, root, source_ns, sp_sequence, oped_sequence,
        )
        target_paths[str(target_path)].append(item.id)

    # 有冲突的 item
    conflict_items = set()
    conflict_target_paths: Dict[str, Path] = {}
    for path_str, item_ids in target_paths.items():
        if len(item_ids) > 1:
            used_paths = set(target_paths.keys())
            for item_id in item_ids:
                conflict_items.add(item_id)
            for item in video_items:
                if item.id not in item_ids:
                    continue
                base_path, _, _ = build_target_for_item(
                    item, root, source_ns, sp_sequence, oped_sequence,
                )
                candidate = _with_filename_suffix(base_path, _conflict_suffix(item))
                while str(candidate) in used_paths:
                    digest = hashlib.sha1(
                        f"{item.relative_path}:{len(used_paths)}".encode("utf-8")
                    ).hexdigest()[:8]
                    candidate = _with_filename_suffix(base_path, digest)
                used_paths.add(str(candidate))
                conflict_target_paths[item.id] = candidate

    # 生成
    result_items: List[MirrorItemResult] = []
    generated = 0
    skipped = 0
    failed = 0
    errors: List[str] = []
    last_reported_progress = -1

    def report_generation_progress(processed: int, current_item: Optional[ImportPlanItem] = None) -> None:
        nonlocal last_reported_progress
        progress = 20 + int(70 * processed / max(1, total_items))
        if progress == last_reported_progress and processed < total_items:
            return
        last_reported_progress = progress
        if progress_callback:
            patch = {
                "generated_count": generated,
                "failed_count": failed,
                "skipped_count": skipped,
                "items_count": total_items,
                "processed_count": processed,
                "log_kind": "info",
            }
            target = _current_target_label(current_item)
            if target:
                patch["current_target"] = target
            progress_callback(
                progress,
                f"正在生成镜像 {processed}/{total_items}",
                patch,
            )

    for item in ignored_items:
        result_items.append(MirrorItemResult(
            item_id=item.id,
            raw_file_id=item.raw_file_id,
            source=plan.source,
            status="skipped",
            strm_path="",
            real_path=item.real_path,
            message="按规则跳过，不生成镜像",
        ))
        skipped += 1

    report_generation_progress(len(ignored_items))

    for index, item in enumerate(video_items, start=1):
        processed = len(ignored_items) + index
        # 字段校验（不替计划补脑）
        validation_error = _validate_item(item)
        if validation_error:
            result_items.append(MirrorItemResult(
                item_id=item.id,
                raw_file_id=item.raw_file_id,
                source=plan.source,
                status="failed",
                strm_path="",
                real_path=item.real_path,
                message=f"字段校验失败: {validation_error}",
            ))
            errors.append(f"item {item.id}: {validation_error}")
            failed += 1
            report_generation_progress(processed, item)
            continue

        # 冲突检查：冲突不再失败，改用可读且稳定的唯一文件名保留每个视频。
        if item.id in conflict_items:
            target_path = conflict_target_paths[item.id]
            target_dir = str(target_path.parent)
            target_filename = target_path.name
        else:
            target_path, target_dir, target_filename = build_target_for_item(
                item, root, source_ns, sp_sequence, oped_sequence,
            )
        content = item.real_path

        # 检查已存在文件
        should_write, conflict_msg = _check_strm_conflict(target_path, content)
        if not should_write:
            if "跳过" in conflict_msg:
                # 内容相同，跳过
                result_items.append(MirrorItemResult(
                    item_id=item.id,
                    raw_file_id=item.raw_file_id,
                    source=plan.source,
                    status="skipped",
                    strm_path=str(target_path),
                    real_path=content,
                    message=conflict_msg,
                ))
                # 仍然回填 target 字段
                item.target_dir = target_dir
                item.target_filename = target_filename
                item.target_strm_path = str(target_path)
                skipped += 1
            else:
                # 内容不同，失败
                result_items.append(MirrorItemResult(
                    item_id=item.id,
                    raw_file_id=item.raw_file_id,
                    source=plan.source,
                    status="failed",
                    strm_path=str(target_path),
                    real_path=content,
                    message=conflict_msg,
                ))
                errors.append(f"item {item.id}: {conflict_msg}")
                failed += 1
            report_generation_progress(processed, item)
            continue

        # 写入 .strm
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(content + "\n", encoding="utf-8")
        except (IOError, OSError) as e:
            result_items.append(MirrorItemResult(
                item_id=item.id,
                raw_file_id=item.raw_file_id,
                source=plan.source,
                status="failed",
                strm_path=str(target_path),
                real_path=content,
                message=f"写入失败: {e}",
            ))
            errors.append(f"item {item.id}: 写入失败: {e}")
            failed += 1
            report_generation_progress(processed, item)
            continue

        # 成功，回填 target 字段
        item.target_dir = target_dir
        item.target_filename = target_filename
        item.target_strm_path = str(target_path)

        result_items.append(MirrorItemResult(
            item_id=item.id,
            raw_file_id=item.raw_file_id,
            source=plan.source,
            status="generated",
            strm_path=str(target_path),
            real_path=content,
            message=conflict_msg,
        ))
        generated += 1
        report_generation_progress(processed, item)

    # 确定整体状态
    if failed == 0:
        overall_status = "success"
    elif generated > 0:
        overall_status = "partial_failed"
    else:
        overall_status = "failed"

    # 保存 plan
    # 显式 mirror_root 主要用于隔离测试或离线预演，不能覆盖正式来源的 latest 计划。
    if progress_callback:
        progress_callback(92, "镜像文件处理完成，正在保存导入计划", {
            "generated_count": generated,
            "failed_count": failed,
            "skipped_count": skipped,
            "items_count": total_items,
            "processed_count": total_items,
            "log_kind": "done",
        })
    save_import_plan(plan, update_latest=update_latest and mirror_root is None)

    return MirrorGenerateResult(
        plan_id=plan.plan_id,
        source=plan.source,
        mirror_root=str(root),
        status=overall_status,
        generated_count=generated,
        skipped_count=skipped,
        failed_count=failed,
        items=result_items,
        errors=errors,
    )
