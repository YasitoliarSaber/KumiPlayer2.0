"""SourceScan handler registry（C 段）。

``scan_kind`` → 显式 handler。重建 adapter 所需的非敏感参数来自
``ScanTask.request``（source_scan_requests.request_json）；Token/密码等
凭据绝不入库，OpenList handler 在执行期通过凭据管理器现场解析。handler
只做「扫描阶段」，finalizer 由 runner 统一调度。

扫描函数从 ``app.api.media_v4`` 惰性导入：adapter 的 monkeypatch 面与
既有测试、调用方保持一致。
"""

from __future__ import annotations

HANDLERS: dict[str, object] = {}


def _handler(*kinds: str):
    def decorate(fn):
        for kind in kinds:
            HANDLERS[kind] = fn
        return fn

    return decorate


def _callbacks(runtime) -> dict:
    return {
        "should_cancel": runtime.cancellation_requested,
        "on_evidence_batch": runtime.persist_evidence_batch,
        "on_progress": runtime.report_progress,
    }


def _assert_scan_identity(task, returned_scan_id, evidence) -> None:
    """内置扫描适配器必须从创建证据起使用 durable scan_id。"""

    if returned_scan_id != task.scan_id or any(
        item.scan_id != task.scan_id or item.root_id != task.root_id
        for item in (evidence or ())
    ):
        raise ValueError(
            "扫描适配器返回的证据未使用登记的 scan_id/root_id，拒绝写入"
        )


#: 请求预算耗尽后的自动缓冲：时长与最大自动轮数。
#: 缓冲保留风控意义（不是连发请求），但不再要求用户手动点"继续扫描"；
#: 只有连续多轮仍撞预算（例如对方持续限流）才交回人工，且 frontier 保留可续扫。
SCAN_BUDGET_COOLDOWN_SECONDS = 15.0
SCAN_BUDGET_MAX_AUTO_COOLDOWNS = 20


def _wait_for_scan_budget(database, *, scan_id: str, runtime=None) -> None:
    """预算耗尽后的缓冲等待：分批睡眠，期间刷新扫描心跳并响应取消。"""

    import time
    from datetime import UTC, datetime

    from app.media_v4.sources.scanner import SourceScanCancelled

    deadline = time.monotonic() + SCAN_BUDGET_COOLDOWN_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if runtime is not None and runtime.cancellation_requested():
            raise SourceScanCancelled()
        # 心跳必须持续刷新：缓冲累计时长可能超过"扫描失联"阈值，
        # 否则会被维护逻辑当成僵尸扫描。
        with database.connect() as conn:
            conn.execute(
                "UPDATE source_scans SET heartbeat_at = ? WHERE scan_id = ?",
                (datetime.now(UTC).isoformat(), scan_id),
            )
        time.sleep(min(0.5, remaining))


def _tree_resolution(task):
    """恢复注册期完成的纯词法解析结果；归档文本 + 登记期证据已足够。"""

    from app.media_v4.sources.tree_root import TreeRootResolution

    request = task.request
    return TreeRootResolution(
        root=str(request.get("effective_root") or ""),
        ok=bool(request.get("resolution_ok", True)),
        reason=str(request.get("resolution_reason") or ""),
        hits=0,
        total=0,
        candidates=tuple(str(item) for item in (request.get("resolution_candidates") or ())),
    )


def _read_archive_text(task) -> str:
    from app.media_v4.sources.input_archive import resolve_archive_path
    from app.media_v4.sources.scanner import read_directory_tree_text

    return read_directory_tree_text(resolve_archive_path(task.archive_path))


@_handler("tree_snapshot", "tree_baseline")
def scan_tree_source(database, task, runtime):
    """TXT 来源：归档文本 → 流式证据 → 验证事实持久化（混合基线另存状态）。"""

    from app.api.media_v4 import _container_name, _persist_tree_scan, build_directory_tree_evidence

    request = task.request
    tree_text = _read_archive_text(task)
    resolution = _tree_resolution(task)
    _scan_id, evidence = build_directory_tree_evidence(
        tree_text,
        root_id=task.root_id,
        provider=str(request.get("provider") or ""),
        source_root=resolution.root,
        source_route_id=str(request.get("route_id") or ""),
        scan_id=task.scan_id,
        **_callbacks(runtime),
    )
    _assert_scan_identity(task, _scan_id, evidence)
    _persist_tree_scan(
        database,
        root_id=task.root_id,
        provider=str(request.get("provider") or ""),
        scan_id=task.scan_id,
        route_id=str(request.get("route_id") or ""),
        effective_root=resolution.root,
        source_locator=(
            str(request.get("remote_root") or "")
            if task.source_mode == "tree_openlist"
            else resolution.root
        ),
        playback_locator=resolution.root or str(request.get("identity_root") or ""),
        root_container=_container_name(resolution.root or str(request.get("identity_root") or "")),
        evidence=evidence,
        resolution=resolution,
        source_mode=task.source_mode,
        last_scan_mode=str(request.get("scan_mode") or task.source_mode),
        durable=True,
        save_evidence=False,
    )
    if task.scan_kind == "tree_baseline":
        from app.media_v4.sources.incremental import build_tree_baseline_state, stage_scan_state

        stage_scan_state(
            task.scan_id,
            build_tree_baseline_state(task.root_id, str(request.get("remote_root") or ""), evidence),
        )
    # 流式 adapter 的证据会先在批次回调落库；runner 结算时仍用完整返回值
    # 补齐未抽查的基线条目，再从数据库读取证据全集。
    return evidence


@_handler("local")
def scan_local_source(database, task, runtime):
    """本地目录：枚举与证据构建都是流式回调，不保留进程内全集。"""

    from app.api.media_v4 import _configured_cloud_roots, scan_local_directory
    from app.core.config import load_config

    config = load_config()
    _root_id, _inner_scan_id, evidence = scan_local_directory(
        str(task.request.get("root_path") or ""),
        excluded_roots=_configured_cloud_roots(config),
        scan_id=task.scan_id,
        **_callbacks(runtime),
    )
    _assert_scan_identity(task, _inner_scan_id, evidence)
    return evidence


def _routes_from(request) -> tuple:
    """把 request_json 里的路由 dict 还原为 OpenListRouteConfig。"""

    from app.integrations.openlist.providers import OpenListRouteConfig

    return tuple(
        OpenListRouteConfig(
            route_id=str(item.get("route_id") or ""),
            remote_prefix=str(item.get("remote_prefix") or ""),
            provider_id=str(item.get("provider_id") or ""),
            enabled=bool(item.get("enabled", True)),
        )
        for item in (request.get("routes") or ())
    )


@_handler("openlist_full")
def scan_openlist_full_source(database, task, runtime):
    """OpenList 完整扫描：凭据执行期解析，目录观察落为增量状态。"""

    from app.api.media_v4 import scan_openlist_directory
    from app.api.openlist_v4 import _client, _remote_root
    from app.core.config import load_config
    from app.media_v4.sources import scan_frontier
    from app.media_v4.sources.incremental import build_full_scan_state, stage_scan_state
    from app.media_v4.sources.scanner import (
        DEFAULT_FULL_SCAN_DIRECTORY_BUDGET,
        SourceScanPaused,
    )

    request = task.request
    config = load_config()
    directory_observations: dict[str, float | None] = {}
    scan_stats: dict = {}

    # 请求预算耗尽**不再直接停住等人点**：缓冲一小段时间后从目录级 frontier
    # 自动继续。旧行为是抛 SourceScanPaused → 扫描停在"已达请求预算"上，用户
    # 不点就什么都不做（实测 10 分钟后仍停着），既不像进度也谈不上安全缓冲。
    # 缓冲仍然保留风控意义（不是连发请求），只是把"人工点继续"换成"自动续跑"。
    cooldown_rounds = 0
    _scan_id = task.scan_id
    evidence: list = []
    while True:
        scan_stats.clear()
        _scan_id, evidence = scan_openlist_directory(
            _client(config),
            remote_root=str(request.get("remote_root") or ""),
            mapping_root=str(request.get("mapping_root") or _remote_root(config)),
            mount_root=str(request.get("mount_root") or ""),
            root_id=task.root_id,
            scan_id=task.scan_id,
            default_provider=str(request.get("provider") or ""),
            routes=_routes_from(request),
            directory_observations=directory_observations,
            directory_budget=int(
                request.get("directory_budget") or DEFAULT_FULL_SCAN_DIRECTORY_BUDGET
            ),
            scan_stats=scan_stats,
            # 目录级 frontier：完成一页写回游标、完成目录置 completed；中断（进程退出、
            # 风控 5xx、用户取消）后重新执行同一 scan_id 即可从断点继续。
            frontier_next=lambda: scan_frontier.next_pending_directory(
                database, scan_id=task.scan_id,
            ),
            frontier_mark=lambda **kwargs: scan_frontier.mark_directory(
                database, scan_id=task.scan_id, **kwargs,
            ),
            frontier_add=lambda **kwargs: scan_frontier.ensure_directories(
                database, scan_id=task.scan_id, **kwargs,
            ),
            **_callbacks(runtime),
        )
        if not scan_stats.get("budget_exhausted"):
            break
        cooldown_rounds += 1
        if cooldown_rounds > SCAN_BUDGET_MAX_AUTO_COOLDOWNS:
            # 兜底：连续多轮都撞预算（例如对方持续限流）时才交回人工，
            # 且保留 frontier，用户点"继续扫描"仍从断点接着跑。
            raise SourceScanPaused(
                f"本次巡检已达到请求预算（已读取 {scan_stats.get('directories_listed', 0)} 个目录，"
                f"自动缓冲 {cooldown_rounds - 1} 次仍未跑完），进度已保留，可稍后继续扫描"
            )
        _wait_for_scan_budget(database, scan_id=task.scan_id, runtime=runtime)

    _assert_scan_identity(task, _scan_id, evidence)
    # 扫描完整走完才清理 frontier；异常路径保留断点供续扫。
    scan_frontier.clear(database, scan_id=task.scan_id)
    stage_scan_state(
        task.scan_id,
        build_full_scan_state(task.root_id, str(request.get("remote_root") or ""), directory_observations),
    )
    # 多轮续跑的完整证据以数据库为准：本轮返回值只包含本轮的收集结果。
    from app.media_v4.persistence.repositories import V4Repository

    return V4Repository(database).list_scan_evidence(task.scan_id)


@_handler("openlist_incremental")
def scan_openlist_incremental_source(database, task, runtime):
    """OpenList 增量扫描：基线与状态从数据库重建，不依赖注册期进程内对象。"""

    from app.api.media_v4 import scan_openlist_incremental
    from app.api.openlist_v4 import _client, _remote_root
    from app.core.config import load_config
    from app.media_v4.persistence.repositories import V4Repository
    from app.media_v4.sources.incremental import (
        build_tree_baseline_state,
        load_active_state,
        stage_scan_state,
    )

    request = task.request
    config = load_config()
    baseline = V4Repository(database).list_confirmed_source_evidence(task.root_id)
    if not baseline:
        raise ValueError("此 OpenList 目录尚无已确认基线，请先完成并确认首次完整扫描")
    state = load_active_state(task.root_id)
    remote_root = str(request.get("remote_root") or "")
    mapping_root = str(request.get("mapping_root") or _remote_root(config))
    if not state or state.get("remote_root") != remote_root:
        state = build_tree_baseline_state(task.root_id, remote_root, baseline)
    _scan_id, evidence, next_state, _stats = scan_openlist_incremental(
        _client(config),
        baseline=baseline,
        state=state,
        mapping_root=mapping_root,
        mount_root=str(request.get("mount_root") or ""),
        scan_id=task.scan_id,
        default_provider=str(request.get("provider") or ""),
        routes=_routes_from(request),
        **_callbacks(runtime),
    )
    _assert_scan_identity(task, _scan_id, evidence)
    stage_scan_state(task.scan_id, next_state)
    return evidence
