"""Source Catalog 扫描服务。

- one-shot（TXT）来源：一次性完整快照按 500 条分批写入；
- 分页来源：逐目录分页写 stage → 完整性校验（重复 path / total 漂移 / 页集合不一致，
  最多重试两次，仍不一致保留旧事实并标记 stale）→ 原子提交；
- 深度保护线 128 层（防环），达到保护线进入 review；
- 每批最多 500 条写入，持续检查取消与背压；网络失败表示 stale，不表示目录为空。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from app.catalog import store
from app.catalog.models import SourceNodeInput, SourceRootRecord
from app.integrations.openlist.models import OpenListError

#: 分页完整性校验最大重试次数
MAX_PAGE_RETRIES = 2
#: 网络错误类型（视为 stale/unknown，不判空）
_STALE_ERROR_TYPES = ("network", "timeout", "rate_limit", "transient")
#: 来源级安全失败：不得重试、不得转 PageConsistencyError 吞掉，必须向上传播
#: （DiscoveryEngine → handler → JobDeferredError 等待冷却）
_ABORT_SCAN_KINDS = frozenset({"risk_control", "rate_limit", "source_cooling_down"})


class ScanCancelled(Exception):
    """扫描被取消。"""


class PageConsistencyError(Exception):
    """分页漂移/页集合不一致（可重试）。"""


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise ScanCancelled()


def ingest_snapshot(
    root_id: str,
    generation: int,
    entries: Iterable[SourceNodeInput],
    *,
    should_cancel: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, str, dict | None], None] | None = None,
) -> dict:
    """一次性完整快照导入（TXT 等 one-shot 来源），500 条一批写入 source_nodes。

    TXT 视为一次完整目录读取：未出现的旧条目按 missing tombstone。
    """
    from app.db.database import get_connection
    from app.db.transactions import transaction

    conn = get_connection()
    items = list(entries)
    stats = {"added": 0, "updated": 0, "missing": 0, "unchanged": 0}
    current = {
        row["remote_path"]: row
        for row in conn.execute(
            "SELECT * FROM source_nodes WHERE root_id = ?", (root_id,)
        ).fetchall()
    }
    seen: set[str] = set()

    for offset in range(0, len(items), store.BATCH_WRITE_LIMIT):
        _check_cancel(should_cancel)
        chunk = items[offset:offset + store.BATCH_WRITE_LIMIT]
        with transaction(conn) as tx:
            for item in chunk:
                seen.add(item.remote_path)
                existing = current.get(item.remote_path)
                if existing is None:
                    stats["added"] += 1
                    tx.execute(
                        """
                        INSERT INTO source_nodes (
                            root_id, remote_path, parent_path, name, kind, size, mtime,
                            first_seen_generation, last_seen_generation, tombstone
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                        """,
                        (root_id, item.remote_path, item.parent_path, item.name, item.kind,
                         item.size, item.mtime, generation, generation),
                    )
                elif (existing["kind"] == item.kind and existing["size"] == item.size
                      and existing["mtime"] == item.mtime):
                    stats["unchanged"] += 1
                    tx.execute(
                        "UPDATE source_nodes SET last_seen_generation = ?, tombstone = '' WHERE root_id = ? AND remote_path = ?",
                        (generation, root_id, item.remote_path),
                    )
                else:
                    stats["updated"] += 1
                    tx.execute(
                        """
                        UPDATE source_nodes
                        SET kind = ?, size = ?, mtime = ?, last_seen_generation = ?, tombstone = ''
                        WHERE root_id = ? AND remote_path = ?
                        """,
                        (item.kind, item.size, item.mtime, generation, root_id, item.remote_path),
                    )
        if progress_callback is not None:
            progress_callback(
                int(min(99, (offset + len(chunk)) * 100 // max(1, len(items)))),
                f"正在写入目录事实 {offset + len(chunk)}/{len(items)}",
                {"phase": "catalog_write", "written": offset + len(chunk), "total": len(items)},
            )

    # 完整快照：旧条目未出现 → missing tombstone
    with transaction(conn) as tx:
        for remote, row in current.items():
            if row["tombstone"]:
                continue
            if remote in seen:
                continue
            stats["missing"] += 1
            tx.execute(
                "UPDATE source_nodes SET tombstone = ? WHERE root_id = ? AND remote_path = ?",
                (store.now_iso(), root_id, remote),
            )
    return stats


def _validate_page_consistency(entries: list[SourceNodeInput], page: int, total: int | None) -> None:
    """检测重复 path / total 漂移 / 页集合不一致。"""
    seen: set[str] = set()
    for item in entries:
        if item.remote_path in seen:
            raise PageConsistencyError(f"分页返回重复路径: {item.remote_path}")
        seen.add(item.remote_path)
    if total is not None and len(entries) > total:
        raise PageConsistencyError("分页返回条目数超过服务端总数")


def scan_directory_paginated(
    client,
    root_id: str,
    remote_path: str,
    generation: int,
    *,
    parent_path: str = "",
    depth: int = 0,
    per_page: int = 100,
    should_cancel: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, str, dict | None], None] | None = None,
    rate_limiter: Callable[[], None] | None = None,
) -> dict:
    """分页扫描一个目录并原子提交；失败重试最多两次，仍失败保留旧事实标记 stale。"""
    if depth > store.MAX_DIRECTORY_DEPTH:
        store.update_directory(
            root_id, remote_path,
            state="failed", last_error_kind="depth_limit",
        )
        raise PageConsistencyError(
            f"目录层级超过 {store.MAX_DIRECTORY_DEPTH} 层保护线，进入人工范围选择"
        )

    store.upsert_directory(root_id, remote_path, parent_path=parent_path, depth=depth)
    store.update_directory(root_id, remote_path, state="scanning")

    last_error: Exception | None = None
    for _attempt in range(MAX_PAGE_RETRIES + 1):
        _check_cancel(should_cancel)
        run_id = store.new_stage_run()
        # 先清旧的暂存残留（对新 run_id 为 no-op），再登记归属：保证从第一页
        # 写入到提交/取消/失败清理之前，source_stage_runs 中该 run 的归属始终存在，
        # 供删除来源根/父根归并时精确清理。
        store.clear_stage(run_id)
        store.register_stage_run(run_id, root_id)
        page = 1
        collected = 0
        total: int | None = None
        first_total: int | None = None
        seen_paths: set[str] = set()
        try:
            while True:
                _check_cancel(should_cancel)
                if rate_limiter is not None:
                    rate_limiter()
                dir_page = client.enumerate_directory(
                    remote_path, page=page, per_page=per_page,
                )
                total = dir_page.total
                # 跨页 total 漂移：服务端总数在分页过程中变化 → 页集合不可信
                if first_total is None:
                    first_total = total
                elif total != first_total:
                    raise PageConsistencyError(
                        f"分页 total 漂移: 首页 total={first_total}, 第 {page} 页 total={total}"
                    )
                _validate_page_consistency(dir_page.entries, page, total)
                # 跨页重复检测：写 stage 前拦截，避免 INSERT OR REPLACE 静默覆盖
                for item in dir_page.entries:
                    if item.remote_path in seen_paths:
                        raise PageConsistencyError(
                            f"跨页重复路径: {item.remote_path}"
                        )
                    seen_paths.add(item.remote_path)
                store.add_stage_page(run_id, remote_path, page, dir_page.entries)
                collected += len(dir_page.entries)
                if not dir_page.entries:
                    break
                if total is not None and collected >= total:
                    break
                if len(dir_page.entries) < per_page:
                    break
                page += 1
            # 服务端提供可信 total 时，完整页集合必须精确等于 total：
            # 提前短页（最后一页 < per_page）不代表分页完成，可能是
            # snapshot drift / 页集合不完整，整轮按不一致处理。
            if first_total is not None and collected != first_total:
                raise PageConsistencyError(
                    f"提前短页: 收集 {collected} 条 != 服务端总数 {first_total}"
                )
            # 完整分页：原子提交（missing 只发生在完整读取后）
            stats = store.commit_directory(root_id, remote_path, run_id, generation)
            if progress_callback is not None:
                progress_callback(
                    0, f"已提交 {remote_path}",
                    {"phase": "catalog_directory", "path": remote_path, "diff": stats},
                )
            return stats
        except ScanCancelled:
            store.update_directory(
                root_id, remote_path, state="queued", last_error_kind="cancelled",
            )
            store.clear_stage(run_id)
            raise
        except PageConsistencyError as exc:
            last_error = exc
        except (OpenListError, OSError, ValueError) as exc:
            # 来源级风控/限流/冷却：立即中止并向上传播（不重试、不转
            # PageConsistencyError）。429 已在 client 层第一次响应即失败，
            # 这里的 rate_limit 只可能是冷启动竞态，同样直接传播。
            if getattr(exc, "kind", "") in _ABORT_SCAN_KINDS:
                store.update_directory(
                    root_id, remote_path,
                    state="failed",
                    last_error_kind=getattr(exc, "kind", "risk_control"),
                )
                raise
            last_error = exc
            if getattr(exc, "kind", type(exc).__name__.lower()) not in _STALE_ERROR_TYPES:
                break  # 非网络错误不重试
        # 重试前清理暂存
        store.clear_stage(run_id)

    # 重试耗尽：保留旧目录事实并标记 stale（不误删）
    existing_dir = store.get_directory(root_id, remote_path)
    store.update_directory(
        root_id, remote_path,
        state="failed",
        last_error_kind=(
            getattr(last_error, "kind", None) or type(last_error).__name__.lower()
        ) if last_error else "unknown",
        retry_count=(existing_dir.get("retry_count") or 0) + 1 if existing_dir else 1,
    )
    raise PageConsistencyError(f"目录分页一致性校验失败: {last_error}") from last_error


def list_source_roots_for_scan(
    source_id: str = "",
) -> list[SourceRootRecord]:
    """列出待扫描的 source roots（供任务调度）。"""
    return store.list_source_roots(source_id)
