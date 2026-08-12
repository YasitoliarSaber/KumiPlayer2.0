"""增量 diff 逻辑

对比新旧 RawSnapshot，发现新增 / 缺失 / 疑似移动 / 疑似改名。
diff 层只比较文件身份，不识别媒体结构。
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.raw.models import RawFile, RawSnapshot
from app.recognition.resource_type import classify_resource_type

# ============================================================
# 数据结构
# ============================================================

@dataclass
class DiffItem:
    """单个文件的 diff 结果"""
    item_id: str = ""
    change_type: str = ""  # added / replaced / missing / moved / renamed / unchanged / uncertain
    source: str = ""
    raw_file_id: str = ""
    old_relative_path: str = ""
    new_relative_path: str = ""
    old_real_path: str = ""
    new_real_path: str = ""
    size: int = 0
    resource_type: str = ""
    confidence: str = "medium"
    reasons: list[str] = field(default_factory=list)
    needs_review: bool = False


@dataclass
class DiffSafety:
    """安全检查结果"""
    blocked: bool = False
    delete_ratio: float = 0.0
    path_change_ratio: float = 0.0
    total_change_ratio: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class DiffResult:
    """完整 diff 结果"""
    diff_id: str = ""
    source: str = ""
    old_snapshot_id: str = ""
    new_snapshot_id: str = ""
    created_at: str = ""
    old_video_count: int = 0
    new_video_count: int = 0
    added_count: int = 0
    missing_count: int = 0
    moved_count: int = 0
    renamed_count: int = 0
    unchanged_count: int = 0
    replaced_count: int = 0
    uncertain_count: int = 0
    safety: DiffSafety = field(default_factory=DiffSafety)
    items: list[DiffItem] = field(default_factory=list)


# ============================================================
# 辅助函数
# ============================================================

def _make_diff_id(source: str, old_id: str, new_id: str) -> str:
    content = f"{source}:{old_id}:{new_id}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:16]


def _make_diff_item_id(diff_id: str, change_type: str, path: str) -> str:
    content = f"{diff_id}:{change_type}:{path}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:12]


def _basename(path: str) -> str:
    """提取文件名"""
    return path.replace("\\", "/").split("/")[-1]


def _stem(name: str) -> str:
    """去掉扩展名的文件名"""
    dot = name.rfind(".")
    return name[:dot] if dot > 0 else name


def _similarity(a: str, b: str) -> float:
    """简单字符串相似度（0-1）"""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    # 使用最长公共子序列长度 / 较长字符串长度
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    lcs = dp[m][n]
    return lcs / max(m, n)


def _count_videos(snapshot: RawSnapshot) -> int:
    """统计视频文件数"""
    return sum(1 for f in snapshot.files if f.resource_hint == "video")


# ============================================================
# Diff 核心逻辑
# ============================================================

def compute_diff(
    old_snapshot: RawSnapshot,
    new_snapshot: RawSnapshot,
) -> DiffResult:
    """计算两个 RawSnapshot 的 diff

    规则：
    - 相同 relative_path 且指纹相同: unchanged
    - 相同 relative_path 但 size/mtime 改变: replaced
    - 旧不存在、新存在: added
    - 旧存在、新不存在: missing
    - 同 basename + size + 不同 path: moved
    - 相似 basename + size + 不同 path: renamed / uncertain
    """
    diff_id = _make_diff_id(
        new_snapshot.source,
        old_snapshot.snapshot_id,
        new_snapshot.snapshot_id,
    )
    now = datetime.now(timezone(timedelta(hours=8))).isoformat()

    # 建立索引
    old_by_path: dict[str, RawFile] = {}
    old_by_id: dict[str, RawFile] = {}
    for f in old_snapshot.files:
        old_by_path[f.relative_path] = f
        if f.id:
            old_by_id[f.id] = f

    new_by_path: dict[str, RawFile] = {}
    new_by_id: dict[str, RawFile] = {}
    for f in new_snapshot.files:
        new_by_path[f.relative_path] = f
        if f.id:
            new_by_id[f.id] = f

    items: list[DiffItem] = []
    matched_old_paths: set[str] = set()
    matched_new_paths: set[str] = set()

    # 1. 相同 relative_path → unchanged / replaced
    for path, new_file in new_by_path.items():
        if path in old_by_path:
            old_file = old_by_path[path]
            resource_type = classify_resource_type(name=new_file.name, ext=new_file.ext)
            changed = _file_fingerprint_changed(old_file, new_file)
            change_type = "replaced" if changed else "unchanged"
            items.append(DiffItem(
                item_id=_make_diff_item_id(diff_id, change_type, path),
                change_type=change_type,
                source=new_snapshot.source,
                raw_file_id=new_file.id,
                old_relative_path=path,
                new_relative_path=path,
                old_real_path=old_file.real_path,
                new_real_path=new_file.real_path,
                size=new_file.size or 0,
                resource_type=resource_type,
                confidence="high",
                reasons=["路径相同，但文件大小或修改时间发生变化"] if changed else ["路径和文件指纹相同"],
            ))
            matched_old_paths.add(path)
            matched_new_paths.add(path)

    # 2. 用 file_id 匹配：新存在、旧不存在，但 file_id 相同 → moved/renamed
    for path, new_file in new_by_path.items():
        if path in matched_new_paths:
            continue
        if not new_file.id:
            continue
        if new_file.id in old_by_id:
            old_file = old_by_id[new_file.id]
            if old_file.relative_path in matched_old_paths:
                continue
            resource_type = classify_resource_type(name=new_file.name, ext=new_file.ext)
            old_name = _basename(old_file.relative_path)
            new_name = _basename(path)
            old_stem = _stem(old_name)
            new_stem = _stem(new_name)

            if old_name == new_name:
                change_type = "moved"
                reasons = [f"file_id 相同，basename 相同，路径不同：{old_file.relative_path} → {path}"]
            elif _similarity(old_stem, new_stem) > 0.7:
                change_type = "renamed"
                reasons = [f"file_id 相同，basename 相似：{old_name} → {new_name}"]
            else:
                change_type = "uncertain"
                reasons = [f"file_id 相同，但 basename 差异较大：{old_name} → {new_name}"]
                needs_review = True

            items.append(DiffItem(
                item_id=_make_diff_item_id(diff_id, change_type, path),
                change_type=change_type,
                source=new_snapshot.source,
                raw_file_id=new_file.id,
                old_relative_path=old_file.relative_path,
                new_relative_path=path,
                old_real_path=old_file.real_path,
                new_real_path=new_file.real_path,
                size=new_file.size or 0,
                resource_type=resource_type,
                confidence="medium" if change_type != "uncertain" else "low",
                reasons=reasons,
                needs_review=needs_review if change_type == "uncertain" else False,
            ))
            matched_old_paths.add(old_file.relative_path)
            matched_new_paths.add(path)

    # 3. 用 basename + size 匹配剩余未匹配文件
    remaining_old = {p: f for p, f in old_by_path.items() if p not in matched_old_paths}
    remaining_new = {p: f for p, f in new_by_path.items() if p not in matched_new_paths}

    # 建立 (stem, size) → old_file 索引
    old_by_stem_size: dict[tuple[str, int], list[tuple[str, RawFile]]] = {}
    for path, f in remaining_old.items():
        name = _basename(path)
        s = _stem(name)
        size = f.size or 0
        key = (s, size)
        if key not in old_by_stem_size:
            old_by_stem_size[key] = []
        old_by_stem_size[key].append((path, f))

    for path, new_file in list(remaining_new.items()):
        if path in matched_new_paths:
            continue
        name = _basename(path)
        s = _stem(name)
        size = new_file.size or 0
        resource_type = classify_resource_type(name=new_file.name, ext=new_file.ext)

        # 尝试精确 stem + size 匹配
        key = (s, size)
        if key in old_by_stem_size:
            candidates = old_by_stem_size[key]
            if len(candidates) == 1:
                old_path, old_file = candidates[0]
                if old_path not in matched_old_paths:
                    items.append(DiffItem(
                        item_id=_make_diff_item_id(diff_id, "moved", path),
                        change_type="moved",
                        source=new_snapshot.source,
                        raw_file_id=new_file.id,
                        old_relative_path=old_path,
                        new_relative_path=path,
                        old_real_path=old_file.real_path,
                        new_real_path=new_file.real_path,
                        size=size,
                        resource_type=resource_type,
                        confidence="medium",
                        reasons=[f"basename+size 匹配，路径不同：{old_path} → {path}"],
                    ))
                    matched_old_paths.add(old_path)
                    matched_new_paths.add(path)
                    continue

        # 尝试相似 stem + size 匹配
        for (old_s, old_size), candidates in old_by_stem_size.items():
            if old_size != size:
                continue
            if _similarity(s, old_s) > 0.7:
                unmatched = [(p, f) for p, f in candidates if p not in matched_old_paths]
                if len(unmatched) == 1:
                    old_path, old_file = unmatched[0]
                    items.append(DiffItem(
                        item_id=_make_diff_item_id(diff_id, "renamed", path),
                        change_type="renamed",
                        source=new_snapshot.source,
                        raw_file_id=new_file.id,
                        old_relative_path=old_path,
                        new_relative_path=path,
                        old_real_path=old_file.real_path,
                        new_real_path=new_file.real_path,
                        size=size,
                        resource_type=resource_type,
                        confidence="medium",
                        reasons=[f"相似 basename+size 匹配：{old_path} → {path}"],
                    ))
                    matched_old_paths.add(old_path)
                    matched_new_paths.add(path)
                    break

    # 4. 剩余新文件 → added
    for path, new_file in new_by_path.items():
        if path in matched_new_paths:
            continue
        resource_type = classify_resource_type(name=new_file.name, ext=new_file.ext)
        items.append(DiffItem(
            item_id=_make_diff_item_id(diff_id, "added", path),
            change_type="added",
            source=new_snapshot.source,
            raw_file_id=new_file.id,
            new_relative_path=path,
            new_real_path=new_file.real_path,
            size=new_file.size or 0,
            resource_type=resource_type,
            confidence="high",
            reasons=["新增文件"],
        ))
        matched_new_paths.add(path)

    # 5. 剩余旧文件 → missing
    for path, old_file in old_by_path.items():
        if path in matched_old_paths:
            continue
        resource_type = classify_resource_type(name=old_file.name, ext=old_file.ext)
        items.append(DiffItem(
            item_id=_make_diff_item_id(diff_id, "missing", path),
            change_type="missing",
            source=new_snapshot.source,
            raw_file_id=old_file.id,
            old_relative_path=path,
            old_real_path=old_file.real_path,
            size=old_file.size or 0,
            resource_type=resource_type,
            confidence="high",
            reasons=["来源扫描未发现该文件"],
            needs_review=True,
        ))

    # 统计
    added = sum(1 for i in items if i.change_type == "added")
    missing = sum(1 for i in items if i.change_type == "missing")
    moved = sum(1 for i in items if i.change_type == "moved")
    renamed = sum(1 for i in items if i.change_type == "renamed")
    unchanged = sum(1 for i in items if i.change_type == "unchanged")
    replaced = sum(1 for i in items if i.change_type == "replaced")
    uncertain = sum(1 for i in items if i.change_type == "uncertain")

    old_video_count = _count_videos(old_snapshot)
    new_video_count = _count_videos(new_snapshot)

    # 安全检查
    safety = _check_safety(old_video_count, new_video_count, missing, moved, renamed, uncertain)

    return DiffResult(
        diff_id=diff_id,
        source=new_snapshot.source,
        old_snapshot_id=old_snapshot.snapshot_id,
        new_snapshot_id=new_snapshot.snapshot_id,
        created_at=now,
        old_video_count=old_video_count,
        new_video_count=new_video_count,
        added_count=added,
        missing_count=missing,
        moved_count=moved,
        renamed_count=renamed,
        unchanged_count=unchanged,
        replaced_count=replaced,
        uncertain_count=uncertain,
        safety=safety,
        items=items,
    )


def _file_fingerprint_changed(old_file: RawFile, new_file: RawFile) -> bool:
    # 目录树来源通常不提供 size/mtime；首次切换到真实挂载目录扫描时，
    # 不能把“原来未知、现在可见”的元数据误判成文件已替换。
    if (
        old_file.size is not None
        and new_file.size is not None
        and int(old_file.size) != int(new_file.size)
    ):
        return True
    old_content = getattr(old_file, "content_fingerprint", "")
    new_content = getattr(new_file, "content_fingerprint", "")
    if old_content and new_content:
        return old_content != new_content
    # 百度 / 115 / OpenList 挂载的目录树时间与挂载文件时间并非同一套时间戳，
    # 且挂载层可能在文件内容未变时刷新 mtime。路径和大小均相同时不能据此误报替换。
    if old_file.source in {"baidu", "pan115", "openlist"} and new_file.source == old_file.source:
        return False
    if (
        old_file.mtime is not None
        and new_file.mtime is not None
        and abs(float(old_file.mtime) - float(new_file.mtime)) > 0.001
    ):
        return True
    return False


def _check_safety(
    old_video_count: int,
    new_video_count: int,
    missing: int,
    moved: int,
    renamed: int,
    uncertain: int,
) -> DiffSafety:
    """安全检查"""
    reasons = []

    if old_video_count == 0:
        return DiffSafety(blocked=False, reasons=["旧快照无视频文件，跳过安全检查"])

    delete_ratio = missing / old_video_count
    path_change_ratio = (moved + renamed + uncertain) / old_video_count
    total_change_ratio = abs(new_video_count - old_video_count) / old_video_count

    blocked = False
    if delete_ratio > 0.30:
        blocked = True
        reasons.append(f"删除比例 {delete_ratio:.1%} 超过 30% 阈值")
    if path_change_ratio > 0.30:
        blocked = True
        reasons.append(f"路径变化比例 {path_change_ratio:.1%} 超过 30% 阈值")
    if total_change_ratio > 0.50:
        blocked = True
        reasons.append(f"总量变化比例 {total_change_ratio:.1%} 超过 50% 阈值")

    return DiffSafety(
        blocked=blocked,
        delete_ratio=delete_ratio,
        path_change_ratio=path_change_ratio,
        total_change_ratio=total_change_ratio,
        reasons=reasons,
    )
