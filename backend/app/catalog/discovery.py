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
import re
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
from app.recognition.media import _is_baidu_category_dir, _is_group_folder


class DiscoveryCancelled(Exception):
    pass


#: 子目录是否是「作品内部结构段」（季/OVA/SP 等），排除纯顶层分类层。
#: is_structure_dirname 会把「动画/电影/剧集」等分类层也算作结构段（用于从
#: 作品路径中剥离分类层），但分类层目录不是“作品容器”的证据——root 直属只有
#: 分类层时，root 是媒体库容器，不应把整个媒体库聚合为一个全根 unit。
#: SPs/Specials 同时命中分类与季/结构集合，仍视为结构段。
def _is_work_structure_child(dirname: str) -> bool:
    if _is_baidu_category_dir(dirname) and not _is_group_folder(dirname):
        return False
    return is_structure_dirname(dirname)


#: 扫描必须立即中止（并向 handler 传播为 JobDeferredError）的来源级错误类型。
#: 普通 OpenListError（404/403/网络等）仍按目录失败隔离收集。
_ABORT_SCAN_KINDS = frozenset({"risk_control", "rate_limit", "source_cooling_down"})


def _check_cancel(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise DiscoveryCancelled()


def _stable_work_key(evidence: dict) -> str:
    return evidence.get("series_group") or evidence.get("work_id") or ""


#: 通用内容层目录名（作品内部内容段的兼容 fallback，大小写不敏感）。
#: 结构归属规则（同名 wrapper、父名前缀+季标记）承担正确性；这里只兜底
#: 真实世界常见内容段命名（tv / episodes / 正片 / 特典 及拼写变体），
#: 不得把整表当作作品边界判定依据。
_CONTENT_LAYER_DIRNAMES = frozenset({
    "tv", "episodes", "episode", "正片", "main", "extras", "extra",
    "sp", "sps", "special", "specials", "sprcial",
    "ova", "oad", "movie", "movies", "特典", "特典映像", "花絮",
})


def _content_layer_key(name: str) -> str:
    return (name or "").strip().casefold()


def _is_content_layer_dirname(child: str, parent: str) -> bool:
    """子目录是否只是父作品的内容层（应被父吸收为同一作品边界）。

    结构性规则优先（不依赖具体目录名）：
    1. 同名 wrapper（``钢之炼金术师FA/钢之炼金术师FA``）；
    2. 父名前缀 + 季/SP 标记（``虫师S1``、``更衣人偶坠入爱河Season 1``、
       ``WorkCS1``、``WorkDSeason 2``）；
    通用内容目录名（tv / episodes / 正片 / sprcial 等）仅作兼容 fallback。
    """
    child = (child or "").strip()
    parent = (parent or "").strip()
    if not child or not parent:
        return False
    if child == parent:
        return True
    if child.startswith(parent):
        rest = child[len(parent):].strip(" ._-·")
        if rest and re.fullmatch(
            r"(?:Season\s*\d+|S\d+|SPs?|S00|OVA|OAD|第\s*\d+\s*季)",
            rest, flags=re.IGNORECASE,
        ):
            return True
    return _content_layer_key(child) in _CONTENT_LAYER_DIRNAMES


def _resolve_boundary_owner(
    path: str,
    root_path: str,
    is_structure: Callable[[str], bool],
) -> str:
    """从候选锚点向上确定唯一作品边界（吸收内容层目录）。

    锚点 = 直接含视频或直接含结构子目录的目录（候选资格来源）。
    若锚点自身只是父作品的内容层（同名 wrapper / 父名前缀+季标记 /
    通用内容层目录名），候选资格上提到父目录，直到到达 root 或遇到
    真正的作品容器名。返回最终 boundary（完整远端路径）。
    """
    current = (path or "").rstrip("/") or "/"
    root = (root_path or "").rstrip("/") or "/"
    while current != root:
        parent = current.rsplit("/", 1)[0] if "/" in current else ""
        if not parent or parent == root:
            return current
        child_name = current.rsplit("/", 1)[-1]
        parent_name = parent.rsplit("/", 1)[-1] or parent
        if _is_content_layer_dirname(child_name, parent_name) or is_structure(child_name):
            current = parent
            continue
        return current
    return current


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
            children = catalog_store.list_current_children(self.root_id, path)
            return any(
                item["kind"] == "file" and _is_video_name(item["name"]) for item in children
            ) or any(
                item["kind"] == "dir" and _is_work_structure_child(item["name"])
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
            # 唯一 owner 收敛：候选若存在祖先候选，只保留最浅（祖先吸收后代）。
            # 例：WorkA 与 WorkA/WorkX 同时是候选 → WorkA 拥有整个 subtree，
            # WorkA/WorkX 不得再生成兄弟级 MediaUnit。
            shallow: set[str] = set()
            for candidate in sorted(candidates, key=lambda p: p.count("/")):
                if any(candidate.startswith(anc + "/") for anc in shallow):
                    continue
                shallow.add(candidate)
            candidates.clear()
            candidates.update(shallow)
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
                    candidates.add(
                        _resolve_boundary_owner(path, root_path, self.is_structure)
                    )
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
            node for node in catalog_store.list_current_children(self.root_id, root_path)
            if node["kind"] == "file"
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
            row for row in catalog_store.list_current_nodes_in_boundary(self.root_id, boundary)
            if row["remote_path"].startswith(prefix) and row["kind"] == "file"
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
            row for row in catalog_store.list_current_nodes_in_boundary(self.root_id, boundary)
            if row["remote_path"].startswith(prefix) and row["kind"] == "file"
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
            # 作品容器上下文来自 MediaUnit boundary，而不是整个 SourceRoot：
            # 多作品媒体库根（如「刮削好的动画」）不得进入每部作品的系列身份。
            root_container=_boundary_container_name(boundary, catalog_store.get_source_root(self.root_id).remote_locator),
            import_family=(
                catalog_store.get_source_root(self.root_id).import_family or ""
            ),
            file_count=len(files),
            video_count=sum(1 for item in files if item.ext in _video_exts()),
            files=files,
        )
        plan = recognize_import_plan_media(build_draft_import_plan(snapshot))
        # canonical 身份需要 unit_id（同一 root+boundary 跨 generation 复用），
        # 因此先登记/复用 media_unit，再构造 items。
        unit_id = self._create_unit(unit, status="plan_ready")
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
                "canonical_work_id": _derive_canonical_work_id(unit_id, item),
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


def _boundary_container_name(boundary: str, root_path: str) -> str:
    """MediaUnit 作品容器名：boundary 名（单作品根时退回 SourceRoot basename）。

    多作品媒体库根（如「刮削好的动画」）下每个 boundary 是具体作品目录，
    容器名必须是作品名；用户直接选中单作品根时 boundary == root，
    容器名就是根名（S1/S2 季归同一系列）。
    """
    if boundary and boundary.rstrip("/") != (root_path or "").rstrip("/"):
        return boundary.rstrip("/").rsplit("/", 1)[-1] or ""
    return PurePosixPath(root_path or "").name or ""


def _derive_canonical_work_id(unit_id: str, item) -> str:
    """从 MediaUnit lineage 派生稳定的 canonical work 身份。

    - main work（main_series 的季/SP/主条目）：``unit:{unit_id}``——
      unit_id 由同一 root+boundary 复用生成，跨 incremental generation 稳定，
      不依赖标题/系列名/TMDB；
    - standalone（独立电影/外传等独立卡片）：unit 内稳定子身份
      ``unit:{unit_id}:sub:{digest}``，digest 来自边界内相对路径
      （不来自标题），保证同一 unit 下多个 standalone 拥有互不相同的
      稳定 canonical ID，不会把整个 unit 强制合并成一个 canonical。
    """
    canonical = str(getattr(item, "canonical_work_id", "") or "")
    if canonical:
        return canonical
    if str(getattr(item, "card_type", "") or "") != "standalone":
        return f"unit:{unit_id}"
    relative = str(getattr(item, "relative_path", "") or "").replace("\\", "/")
    digest = hashlib.sha1(relative.encode("utf-8")).hexdigest()[:12]
    return f"unit:{unit_id}:sub:{digest}"


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
