"""OpenList 递归目录扫描。

单任务、单请求串行遍历；不使用无界并发。每个目录分页读取，
每次请求前、每页、每个目录均检查 ``should_cancel``。

安全上限：
- 总条目上限（默认 20,000，与挂载扫描量级一致）；
- 目录深度上限（默认 12 层）；
超限时抛出 :class:`OpenListScanLimitExceeded`，调用方不得保存半成品。

进度数据合同（写入 ``result_patch``）：

- ``phase``：固定 ``remote_scan``，扫描期间整体目录树总量未知；
- ``overall_total_known``：恒为 ``False``，前端不得显示伪造的整体百分比；
- ``current_directory_total``：仅使用服务端返回的 ``total``，无总量时为 ``None``；
- ``scanned_directory_count``：一个目录全部分页读取完毕后递增；
- ``queued_directory_count``：等于当前待扫目录队列长度；
- ``found_video_candidate_count``：按现有视频扩展名映射统计的候选视频数，
  不代表最终识别出的作品数。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePosixPath

from app.integrations.openlist.client import MAX_PER_PAGE, OpenListClient
from app.integrations.openlist.models import (
    OpenListEntry,
    OpenListScanLimitExceeded,
)

DEFAULT_MAX_ENTRIES = 20_000
DEFAULT_MAX_DEPTH = 12


def scan_remote_tree(
    client: OpenListClient,
    remote_root: str,
    *,
    should_cancel: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, str, dict | None], None] | None = None,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> list[OpenListEntry]:
    """从 ``remote_root`` 开始深度优先遍历，返回全部远端条目。

    条目顺序为遍历顺序；每个条目的 ``depth`` 是相对扫描根的层级。
    进度回调约定与 TaskManager 的 ``progress_callback(progress, message, result_patch)``
    兼容（progress 为 0-99 整数，仅作内部阶段推进值，不代表整体完成度）。
    """
    from app.recognition.resource_type import VIDEO_EXTS

    cancel = should_cancel or (lambda: False)
    entries: list[OpenListEntry] = []

    def check_cancel() -> None:
        if cancel():
            raise _ScanCancelled()

    # 扫描期间累计计数（目录树总量在递归完成前未知）
    found_dir_count = 0
    found_file_count = 0
    scanned_directory_count = 0
    found_video_candidate_count = 0

    def report(
        *,
        current_path: str,
        current_page: int,
        current_directory_total: int | None,
        current_directory_collected: int,
        queued_directory_count: int,
    ) -> None:
        if progress_callback is None:
            return
        total_found = found_dir_count + found_file_count
        progress_callback(
            min(99, total_found * 100 // max_entries) if max_entries else 99,
            f"正在扫描 {current_path}（已发现 {total_found} 个条目）",
            {
                "phase": "remote_scan",
                "overall_total_known": False,
                "current_path": current_path,
                "current_page": current_page,
                "current_directory_total": current_directory_total,
                "current_directory_collected": current_directory_collected,
                "scanned_directory_count": scanned_directory_count,
                "queued_directory_count": queued_directory_count,
                "found_directory_count": found_dir_count,
                "found_file_count": found_file_count,
                "found_entry_count": total_found,
                "found_video_candidate_count": found_video_candidate_count,
            },
        )

    # 深度优先迭代栈：(远端路径, 深度)
    stack: list[tuple[str, int]] = [(remote_root, 0)]

    while stack:
        check_cancel()
        current_path, depth = stack.pop()

        if depth > max_depth:
            raise OpenListScanLimitExceeded(
                f"远端目录层级超过 {max_depth} 层上限，请选择更精确的目录"
            )

        page = 1
        collected = 0
        directory_total: int | None = None
        while True:
            check_cancel()
            dir_page = client.list_dir(current_path, page=page, per_page=MAX_PER_PAGE)
            directory_total = dir_page.total
            for entry in dir_page.entries:
                check_cancel()
                entry.depth = depth + 1
                if entry.depth > max_depth:
                    raise OpenListScanLimitExceeded(
                        f"远端目录层级超过 {max_depth} 层上限，请选择更精确的目录"
                    )
                if len(entries) >= max_entries:
                    raise OpenListScanLimitExceeded(
                        f"远端目录条目超过 {max_entries} 个上限，请选择更精确的目录"
                    )
                entries.append(entry)
                if entry.is_dir:
                    found_dir_count += 1
                    stack.append((entry.remote_path, depth + 1))
                else:
                    found_file_count += 1
                    if PurePosixPath(entry.name).suffix.lower() in VIDEO_EXTS:
                        found_video_candidate_count += 1

            collected += len(dir_page.entries)
            total = dir_page.total
            # 翻页终止条件：收齐 total、当前页不足一页、或服务端未报告总数
            if not dir_page.entries:
                break
            if total and collected >= total:
                break
            if collected % MAX_PER_PAGE == 0 and len(dir_page.entries) < MAX_PER_PAGE:
                break
            page += 1
            report(
                current_path=current_path,
                current_page=page,
                current_directory_total=directory_total,
                current_directory_collected=collected,
                queued_directory_count=len(stack),
            )

        scanned_directory_count += 1
        report(
            current_path=current_path,
            current_page=page,
            current_directory_total=directory_total,
            current_directory_collected=collected,
            queued_directory_count=len(stack),
        )

    return entries


class _ScanCancelled(Exception):
    """内部信号：任务取消。由调用方按 TaskCancelledError 语义处理。"""
