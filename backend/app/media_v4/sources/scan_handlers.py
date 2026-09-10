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
    from app.media_v4.sources.incremental import build_full_scan_state, stage_scan_state

    request = task.request
    config = load_config()
    directory_observations: dict[str, float | None] = {}
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
        **_callbacks(runtime),
    )
    _assert_scan_identity(task, _scan_id, evidence)
    stage_scan_state(
        task.scan_id,
        build_full_scan_state(task.root_id, str(request.get("remote_root") or ""), directory_observations),
    )
    return evidence


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
