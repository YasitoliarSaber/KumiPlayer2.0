"""证据驱动渐进发现器（任务 4）。

- 逐层处理分类目录、结构目录、候选作品目录与未知目录；
- 目录名只产生证据（recognition 层规则），视频完整相对路径提供最终识别证据；
- unit boundary = 去除分类/季度/OVA/SP 等结构段后的稳定作品容器；
- 同一 scope 下相同 series group 的季度与特殊内容合并到同一 unit；
- 发现作品后立即进入现有识别→revision 链路，不等待其他分类分支；
- 证据不足或冲突 → media_units.status=needs_review，不自动建作品。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from app.catalog import service as catalog_service
from app.catalog import store as catalog_store
from app.catalog.closure import is_boundary_complete
from app.catalog.service import PageConsistencyError, ScanCancelled
from app.db.database import get_connection
from app.integrations.openlist.models import OpenListError
from app.recognition.evidence import is_structure_dirname, recognize_path_evidence


class DiscoveryCancelled(Exception):
    pass


#: 扫描必须立即中止（并向 handler 传播为 JobDeferredError）的来源级错误类型。
#: 普通 OpenListError（404/403/网络等）仍按目录失败隔离收集。
_ABORT_SCAN_KINDS = frozenset({"risk_control", "rate_limit", "source_cooling_down"})


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise DiscoveryCancelled()


def _stable_work_key(evidence: dict) -> str:
    return evidence.get("series_group") or evidence.get("work_id") or ""


def _boundary_for_path(remote_path: str, root_path: str, is_structure: Callable[[str], bool]) -> str:
    """从视频完整远端路径向上取第一个非结构段目录作为作品外层（boundary）。

    返回完整路径（含 root 前缀），与 source_nodes 的 remote_path 匹配。
    """
    parts = [part for part in remote_path.split("/") if part]
    # 去掉文件名
    parts = parts[:-1]
    while parts and is_structure(parts[-1]):
        parts.pop()
    if not parts:
        return root_path.rstrip("/") or "/"
    return "/" + "/".join(parts)


class DiscoveryEngine:
    def __init__(
        self,
        scanner,
        *,
        source_id: str,
        root_id: str,
        generation: int,
        source: str = "",
        recognize: Callable = recognize_path_evidence,
        is_structure: Callable = is_structure_dirname,
    ):
        self.scanner = scanner
        self.source_id = source_id
        self.root_id = root_id
        self.generation = generation
        self.source = source or ""
        self.recognize = recognize
        self.is_structure = is_structure

    def _source_for_root(self) -> str:
        """按 source_id 推导来源语义（openlist-xxx → openlist；其余按前缀）。"""
        if self.source:
            return self.source
        sid = self.source_id or ""
        if sid.startswith("pan115"):
            return "pan115"
        if sid.startswith("baidu"):
            return "baidu"
        if sid.startswith("local"):
            return "local"
        return "openlist"

    # ---- 探测与聚合 --------------------------------------------------

    def run(
        self,
        should_cancel: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, str, dict | None], None] | None = None,
        on_unit: Callable[[dict], None] | None = None,
        rate_limiter: Callable[[], None] | None = None,
    ) -> list[dict]:
        """探测根下目录，边探测边聚合作品单元并逐单元生成 revision。

        - 候选作品目录直接含视频 → 立即处理（不等待其他分支）；
        - 有结构段子目录（Season/OVA）的候选 → 等其全部子目录扫描完再结算（完整聚合）；
        - 结构段后续变化走增量 revision（新 generation）。
        返回单元结果列表（含 status / boundary / revision_id）。
        """
        root = catalog_store.get_source_root(self.root_id)
        if root is None:
            raise ValueError("source root 不存在")
        root_path = root.remote_locator

        results: list[dict] = []
        processed: set[str] = set()
        candidates: set[str] = set()
        failed_paths: list[str] = []

        # frontier 是数据库事实而不是一次性 BFS 队列。进程退出后完整目录保持
        # complete，未完成目录保持 queued/scanning/failed，下一 worker 直接续扫。
        catalog_store.recover_interrupted_directories(self.root_id)
        if not catalog_store.list_all_directories(self.root_id):
            catalog_store.upsert_directory(self.root_id, root_path, parent_path="", depth=0)

        def is_candidate(path: str) -> bool:
            # 用户选中的 root 本身可以是作品容器（如“飞跃巅峰 内封中字”下直接
            # 是 S1/S2 季目录）：root 有直属视频或结构子目录时也作为候选单元。
            dirname = path.rstrip("/").rsplit("/", 1)[-1]
            if self.is_structure(dirname):
                return False
            children = [
                item for item in catalog_store.list_nodes(self.root_id)
                if item["parent_path"] == path and item["tombstone"] == ""
            ]
            return any(
                item["kind"] == "file" and _is_video_name(item["name"]) for item in children
            ) or any(
                item["kind"] == "dir" and self.is_structure(item["name"])
                for item in children
            )

        def ancestors(path: str) -> list[str]:
            value = path.rstrip("/") or "/"
            values: list[str] = []
            while value and value != root_path:
                values.append(value)
                parent = value.rsplit("/", 1)[0] or "/"
                if parent == value:
                    break
                value = parent
            return values

        def boundary_is_closed(boundary: str) -> bool:
            # closure 唯一实现（catalog.closure）：boundary 下所有当前有效目录
            # 必须全部 complete 才收口；任一 queued/scanning/failed 都阻塞。
            return is_boundary_complete(self.root_id, boundary)

        def settle_candidates() -> None:
            for candidate in sorted(candidates):
                if candidate in processed or not boundary_is_closed(candidate):
                    continue
                processed.add(candidate)
                result = self._process_boundary(candidate, root_path, should_cancel=should_cancel)
                results.append(result)
                if on_unit is not None:
                    on_unit(result)

        while True:
            _check_cancel(should_cancel)
            pending = catalog_store.list_pending_directories(self.root_id, limit=1)
            if not pending:
                break
            directory = pending[0]
            try:
                catalog_service.scan_directory_paginated(
                    self.scanner, self.root_id, directory["remote_path"],
                    self.generation,
                    parent_path=directory["parent_path"],
                    depth=directory["depth"],
                    per_page=100,
                    should_cancel=should_cancel,
                    progress_callback=progress_callback,
                    rate_limiter=rate_limiter,
                )
            except ScanCancelled:
                raise
            except (PageConsistencyError, OpenListError, OSError, ValueError) as exc:
                # 来源级风控/限流/冷却：整棵扫描必须立即中止并向上传播
                # （handler 转 JobDeferredError 等待冷却），绝不能当作普通
                # 目录失败收集后继续扫描下一个目录（否则冷却期间仍会向
                # 同一账号发请求）。普通错误保持 failed_paths 隔离。
                if getattr(exc, "kind", "") in _ABORT_SCAN_KINDS:
                    raise
                # 单目录失败不中断整个 root：目录已由 service 标记 failed，
                # 记录后继续扫描其余目录；下次任务触发时 prepare_scan 会
                # 把 failed 恢复为 queued 统一重试（渐进收敛）。
                failed_paths.append(directory["remote_path"])
                continue
            for path in ancestors(directory["remote_path"]):
                if is_candidate(path):
                    candidates.add(path)
            if progress_callback is not None:
                progress_callback(
                    0, f"已探测 {directory['remote_path']}",
                    {"phase": "discovery_scan", "path": directory["remote_path"]},
                )
            settle_candidates()
        # 重启后可能只剩已经 complete 的边界；再次基于持久目录结算一次，
        # 但不会重扫 root 或重新建立整棵内存树。
        for directory in catalog_store.list_all_directories(self.root_id):
            if is_candidate(directory["remote_path"]):
                candidates.add(directory["remote_path"])
        settle_candidates()

        # 选中 root 直属视频没有稳定的作品目录边界，不能因为 root 本身不是
        # candidate 而静默遗漏。它们作为独立的低证据单元保留给人工复核，且不
        # 阻塞同一 root 下其他已闭合作品的自动识别。
        # （root 已作为作品容器被 settle 处理时不再重复建单元）
        root_files = [
            node for node in catalog_store.list_nodes(self.root_id)
            if node["parent_path"] == root_path and node["kind"] == "file"
            and node["tombstone"] == ""
            and node["name"].rsplit(".", 1)[-1].lower() in _video_exts()
        ]
        if root_files and root_path not in processed:
            unit_id = self._create_unit(
                {"boundary": root_path, "work_key": root_path},
                status="needs_review",
            )
            results.append(
                {
                    "work_key": root_path,
                    "boundary": root_path,
                    "status": "needs_review",
                    "unit_id": unit_id,
                    "note": "选中根目录存在未归属视频",
                }
            )
        self.failed_paths = failed_paths
        return results

    def _process_boundary(
        self,
        boundary: str,
        root_path: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict:
        """聚合作品外层（含结构段子目录）下的证据并生成 revision。"""
        _check_cancel(should_cancel)
        prefix = boundary.rstrip("/") + "/"
        video_rows = [
            row for row in catalog_store.list_nodes(self.root_id)
            if row["remote_path"].startswith(prefix) and row["kind"] == "file"
            and row["tombstone"] == ""
            and _is_video_name(row["name"])
        ]
        unit: dict = {"boundary": boundary, "work_key": boundary, "work_title": ""}
        source = self._source_for_root()
        for row in video_rows:
            boundary_name = boundary.rsplit("/", 1)[-1] or boundary
            relative_to_boundary = _relative_to_root(row["remote_path"], boundary)
            evidence = self.recognize(
                row["name"], relative_to_boundary or row["remote_path"],
                source=source,
                existing_work_title=boundary_name if boundary != root_path else "",
            )
            if not unit["work_title"]:
                unit["work_title"] = evidence.get("work_title") or ""
            if evidence.get("needs_review"):
                unit["needs_review"] = True
        if not unit["work_title"] or unit.get("needs_review"):
            unit_id = self._create_unit(unit, status="needs_review")
            return {
                "work_key": unit["work_key"], "boundary": boundary,
                "status": "needs_review", "unit_id": unit_id,
            }
        return self._process_unit(unit, should_cancel=should_cancel)

    def _derive_real_path(self, remote_path: str) -> str:
        """logical_locator 缺失时按来源推导真实播放路径（不退化相对路径）。

        以 root 为基准取相对部分（TXT 快照的 remote_path 含分类层），
        再拼接到本地挂载根，得到完整播放路径。
        用 Path 拼接保证 Windows 分隔符统一（避免混合斜杠导致 strm 内容
        与旧链路不一致、镜像冲突）。
        """
        from pathlib import Path as _Path

        root = catalog_store.get_source_root(self.root_id)
        if root is None:
            return remote_path
        root_remote = (root.remote_locator or "").rstrip("/")
        if remote_path.startswith(root_remote + "/") and root_remote:
            relative = remote_path[len(root_remote) + 1:]
        else:
            relative = remote_path.lstrip("/")
        base = (root.local_locator or "").rstrip("/\\")
        if not base:
            return remote_path
        result = _Path(base)
        for part in PurePosixPath(relative).parts:
            result = result / part
        return str(result)

    def _provider_for_boundary(self, boundary: str) -> tuple[str, str]:
        """按来源返回 (provider_id, route_id)。

        - openlist：匹配 OpenList 路由，未命中回退 openlist 兼容值；
        - pan115 / baidu / local：直接使用来源自身的 provider（不走 OpenList 路由）。
        """
        source = self._source_for_root()
        if source != "openlist":
            from app.integrations.openlist.providers import compat_provider

            return compat_provider(source), ""
        from app.core.config import load_config
        from app.integrations.openlist.providers import (
            compat_provider,
            match_route,
        )

        try:
            config = load_config()
            routes = list(config.openlist_routes or [])
            route = match_route(routes, boundary)
            if route is not None:
                return route.provider_id, route.route_id
        except Exception:
            pass
        return compat_provider("openlist"), ""

    def _create_unit(self, unit: dict, *, status: str) -> str:
        conn = get_connection()
        timestamp = catalog_store.now_iso()
        boundary = unit.get("boundary") or ""
        work_key = unit.get("work_key") or ""
        # 复用稳定 media_unit：同一 root + boundary 的既有单元不新建，
        # 保证增量 revision 有 parent 链（每次扫描不产生孤儿 unit）。
        existing = conn.execute(
            """
            SELECT unit_id FROM media_units
            WHERE root_id = ? AND boundary = ?
            ORDER BY created_at ASC LIMIT 1
            """,
            (self.root_id, boundary),
        ).fetchone()
        if existing is not None:
            unit_id = existing["unit_id"]
            conn.execute(
                """
                UPDATE media_units
                SET work_key = ?, status = ?, updated_at = ?
                WHERE unit_id = ?
                """,
                (work_key, status, timestamp, unit_id),
            )
            conn.commit()
            return unit_id
        unit_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO media_units (
                unit_id, batch_id, root_id, discovery_scope, boundary, work_key,
                status, closure_generation, current_revision_id, created_at, updated_at
            ) VALUES (?, '', ?, ?, ?, ?, ?, 0, '', ?, ?)
            """,
            (unit_id, self.root_id, boundary, boundary, work_key, status, timestamp, timestamp),
        )
        conn.commit()
        return unit_id

    def _process_unit(self, unit: dict, *, should_cancel: Callable[[], bool] | None = None) -> dict:
        """从 source_nodes 流式构建 RawSnapshot → 现有识别 → 不可变 revision。"""
        _check_cancel(should_cancel)
        from app.import_plan import revision_store
        from app.raw.models import RawFile, RawSnapshot
        from app.recognition.plan_recognizer import recognize_import_plan_media
        from app.recognition.planner import build_draft_import_plan

        boundary = unit["boundary"]
        prefix = boundary.rstrip("/") + "/"
        rows = [
            row for row in catalog_store.list_nodes(self.root_id)
            if row["remote_path"].startswith(prefix) and row["kind"] == "file"
            and row["tombstone"] == ""
        ]

        if not rows:
            unit_id = self._create_unit(unit, status="needs_review")
            return {
                "work_key": unit["work_key"], "boundary": boundary,
                "status": "needs_review", "unit_id": unit_id,
                "note": "作品外层没有可入库视频",
            }

        files: list[RawFile] = []
        source = self._source_for_root()
        # 提供商事实：openlist 走路由匹配；115/百度/本地直接用来源自身
        provider_id, source_route_id = self._provider_for_boundary(boundary)
        for row in rows:
            # 识别必须看到根到文件的完整相对路径；只保留 boundary 内路径会
            # 丢掉作品名和分类证据，导致 draft 无法形成可执行的镜像计划。
            relative = _relative_to_root(row["remote_path"], catalog_store.get_source_root(self.root_id).remote_locator)
            logical_locator = row["logical_locator"] or ""
            # 真实播放路径：优先 logical_locator（stage 已保留），否则按来源推导；
            # Path 归一化统一 Windows 分隔符（避免混合斜杠导致路径不可访问 /
            # strm 内容与旧链路不一致）。
            real_path = logical_locator or self._derive_real_path(row["remote_path"])
            if real_path:
                real_path = str(Path(real_path))
            files.append(
                RawFile(
                    id=hashlib.md5(f"{self.source_id}:{row['remote_path']}".encode()).hexdigest(),
                    snapshot_id="",
                    source=source,
                    source_root=catalog_store.get_source_root(self.root_id).remote_locator,
                    relative_path=relative,
                    real_path=real_path,
                    name=row["name"],
                    stem=row["name"].rsplit(".", 1)[0] if "." in row["name"] else row["name"],
                    ext=row["name"].rsplit(".", 1)[-1].lower() if "." in row["name"] else "",
                    is_file=True,
                    size=row["size"],
                    mtime=row["mtime"],
                )
            )
            # 回写节点：provider/route 是目录级事实（同一 boundary 下共享），
            # 随首个入库节点持久化，供后续镜像/刮削直接读取。
            catalog_store.update_node_provider(
                self.root_id, row["remote_path"], provider_id, source_route_id,
            )
        snapshot = RawSnapshot(
            snapshot_id=uuid.uuid4().hex,
            source=source,
            provider_id=provider_id,
            ingest_method=(
                "directory_tree" if source in ("pan115", "baidu")
                else "local_scan" if source == "local" else "openlist_api"
            ),
            source_route_id=source_route_id,
            source_root=catalog_store.get_source_root(self.root_id).remote_locator,
            root_container=PurePosixPath(
                catalog_store.get_source_root(self.root_id).remote_locator
            ).name or "",
            import_family=(
                catalog_store.get_source_root(self.root_id).import_family or ""
            ),
            file_count=len(files),
            video_count=sum(1 for item in files if item.ext in _video_exts()),
            files=files,
        )
        plan = recognize_import_plan_media(build_draft_import_plan(snapshot))
        items = [
            {
                "id": item.id,
                "source": source,
                "provider_id": item.provider_id or provider_id,
                "relative_path": item.relative_path,
                "real_path": item.real_path,
                "logical_locator": item.real_path,
                "resource_type": item.resource_type,
                "action": item.action,
                "work_id": item.work_id,
                "work_title": item.work_title,
                "original_title": item.original_title,
                "year": item.year,
                "media_type": item.media_type,
                "show_type": item.show_type,
                "series_group": item.series_group,
                "card_type": item.card_type,
                "belongs_to_series": item.belongs_to_series,
                "relation_type": item.relation_type,
                "group_type": item.group_type,
                "season_number": item.season_number,
                "episode_number": item.episode_number,
                "special_number": item.special_number,
                "title": item.title,
                "target_dir": item.target_dir,
                "target_strm_path": item.target_strm_path,
                "confidence": item.confidence,
                "needs_review": item.needs_review,
                "availability": item.availability,
                "warnings": list(item.warnings),
                "reasons": list(item.reasons),
                "user_override_id": item.user_override_id or "",
            }
            for item in plan.items
        ]
        unit_id = self._create_unit(unit, status="plan_ready")
        parent = revision_store.latest_confirmed_revision(unit_id)
        revision = revision_store.create_revision(
            unit_id=unit_id,
            source_generation=self.generation,
            items=items,
            parent_revision_id=parent["revision_id"] if parent else "",
            status="draft",
        )
        return {
            "work_key": unit["work_key"],
            "boundary": boundary,
            "work_title": unit["work_title"],
            "status": "plan_ready",
            "unit_id": unit_id,
            "revision_id": revision["revision_id"],
            "video_count": snapshot.video_count,
        }


def _video_exts() -> set[str]:
    from app.recognition.resource_type import VIDEO_EXTS

    return {ext.lstrip(".").lower() for ext in VIDEO_EXTS}


def _is_video_name(name: str) -> bool:
    """按扩展名判断是否为视频文件（发现层过滤非视频附件）。"""
    dot = (name or "").rfind(".")
    if dot <= 0:
        return False
    return name[dot + 1:].strip().lower() in _video_exts()


def _relative_to_root(remote_path: str, root_path: str) -> str:
    if remote_path == root_path:
        return ""
    if root_path == "/":
        return remote_path.lstrip("/")
    if remote_path.startswith(root_path.rstrip("/") + "/"):
        return remote_path[len(root_path.rstrip("/")) + 1:]
    return ""


def _common_ancestor(left: str, right: str) -> str:
    left_parts = left.split("/")
    right_parts = right.split("/")
    common: list[str] = []
    for a, b in zip(left_parts, right_parts, strict=False):
        if a == b:
            common.append(a)
        else:
            break
    return "/".join(common) or "/"
