"""discovery_scan durable handler：Source Catalog 扫描 → 证据发现 → revision → 即时入队镜像。

与规划员要求对应：
- 按作品 closure 回调立即入队镜像：DiscoveryEngine.run(on_unit=...) 在边界结算后
  立刻 confirmed revision 并 enqueue_mirror，不等整棵树扫完；
- 背压真正接入扫描器：frontier 循环每轮检查 orchestrator.should_backoff()，
  队列水位 >= 50 时协作式休眠等待，降到 25 以下恢复；
- 单一 root 串行扫描（扫描 worker 专用线程），不同 root 的 scan job 由
  resource_key=scan:{root_id} 互斥。
"""

from __future__ import annotations

import time

from app.catalog import store as catalog_store
from app.catalog import source_health
from app.catalog.discovery import DiscoveryCancelled, DiscoveryEngine
from app.import_plan import revision_store
from app.integrations.openlist.models import (
    OpenListRateLimitedError,
    OpenListRiskControlError,
    OpenListSourceCoolingDownError,
)
from app.jobs.models import JobCancelledError, JobDeferredError
from app.jobs.registry import register
from app.pipeline import orchestrator

#: 扫描过程中必须整棵中止并转 JobDeferredError 的来源级安全错误
_RISK_ABORT_TYPES = (
    OpenListRiskControlError,
    OpenListRateLimitedError,
    OpenListSourceCoolingDownError,
)

#: 风控延后的默认等待秒数（cooldown_until 缺失时兜底）
_DEFER_FALLBACK_SECONDS = 3600.0


def _defer_until(health_key: str) -> float:
    """计算延后重试时间：优先 source_health 冷却结束时刻，缺失时按阈值兜底。"""
    record = source_health.get_health(health_key)
    now = time.time()
    if record.cooldown_until > now:
        return record.cooldown_until
    return now + _DEFER_FALLBACK_SECONDS


_DEFER_MESSAGE = "远端网盘疑似触发访问保护，KumiPlayer 已暂停该来源的自动请求，冷却结束后自动重试"


def _build_openlist_client(source_id: str):
    """按 source 记录构造 OpenList 客户端（凭据只存内存，不进 payload）。"""
    from app.core.config import load_config
    from app.integrations.openlist.client import OpenListClient

    config = load_config()
    return OpenListClient(
        config.openlist_server_url,
        config.openlist_username,
        config.openlist_password,
    )


def _build_scanner(root: dict):
    """按来源构造统一扫描器（补完 5：115/百度/本地接入 Source Catalog）。

    - openlist：OpenList 客户端分页枚举；
    - local：本地分页枚举（adapter 直通）；
    - pan115 / baidu：目录树 TXT 一次性快照（adapter.snapshot_entries）。
    """
    from app.catalog.scanner import SourceCatalogScanner
    from app.core.config import load_config
    from app.integrations.openlist.client import OpenListClient
    from app.sources.registry import get_source_adapter

    source = str(root.get("source_id") or "")
    if source.startswith("openlist") or source == "openlist":
        config = load_config()
        client = OpenListClient(
            config.openlist_server_url,
            config.openlist_username,
            config.openlist_password,
        )
        return SourceCatalogScanner(source="openlist", client=client)
    if source.startswith("local"):
        adapter = get_source_adapter("local")
        return SourceCatalogScanner(source="local", adapter=adapter, source_root=root.get("remote_locator") or "/")
    # pan115 / baidu：目录树 TXT 输入文件必须从 job payload 显式传入
    input_path = str(root.get("input_path") or "")
    if not input_path:
        raise ValueError(
            f"{source} 来源的 discovery job 缺少 input_path（目录树 TXT 路径）"
        )
    adapter = get_source_adapter("pan115" if source.startswith("pan115") else "baidu")
    return SourceCatalogScanner(
        source="pan115" if source.startswith("pan115") else "baidu",
        adapter=adapter, input_path=input_path, source_root=root.get("remote_locator") or "/",
    )


def _wait_for_backpressure(should_cancel) -> None:
    """队列水位达到上限时协作式等待（不丢任务、不失败），降到恢复线继续。"""
    while orchestrator.should_backoff():
        if should_cancel is not None and should_cancel():
            raise DiscoveryCancelled("取消请求：背压等待中")
        time.sleep(1.0)


def handle_discovery_scan(payload: dict, progress_callback=None, should_cancel=None) -> dict:
    """扫描一个 source root，逐作品单元生成 revision 并即时入队 mirror。"""
    root_id = str(payload.get("root_id") or "")
    generation = int(payload.get("generation") or 0)
    if not root_id:
        raise ValueError("discovery payload 缺少 root_id")
    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise ValueError(f"source root 不存在: {root_id}")

    # 模块 1 冷却拦截：OpenList 来源在构造扫描器之前检查连接健康。
    # 冷却中不构造扫描器、不跑 engine、不请求远程；保留 Source Catalog 已有数据。
    # 与 OpenListClient 上报一致：连接键 = sha256(server_url|username)
    health_key = ""
    source = str(root.source_id or "")
    if source.startswith("openlist") or source == "openlist":
        from app.core.config import load_config
        from app.integrations.openlist.governor import governor_connection_key

        config = load_config()
        health_key = governor_connection_key(
            config.openlist_server_url, config.openlist_username
        )
        allowed, record = source_health.peek_request_allowed(health_key)
        if not allowed:
            # 冷却中：延后而非成功。job 回到 queued + not_before=cooldown_until，
            # 不消耗 attempt、不显示 succeeded/failed。
            raise JobDeferredError(
                until_unix=record.cooldown_until or _defer_until(health_key),
                message=_DEFER_MESSAGE,
            )

    scanner = _build_scanner(
        {
            "source_id": root.source_id,
            "remote_locator": root.remote_locator,
            "local_locator": root.local_locator,
            "import_family": root.import_family,
            "import_scope": root.import_scope,
            "input_path": str(payload.get("input_path") or ""),
        }
    )
    engine = DiscoveryEngine(
        scanner,
        source_id=root.source_id,
        root_id=root_id,
        generation=generation,
    )

    summary = {"plan_ready": 0, "needs_review": 0, "mirror_enqueued": 0}

    def on_unit(result: dict) -> None:
        if result.get("status") == "plan_ready" and result.get("revision_id"):
            # closure 后仍须通过同一确认门槛；不确定的识别只能进入复核，
            # 绝不能因“渐进导入”而绕过安全检查。
            confirmed, _reason = revision_store.try_auto_confirm_revision(result["revision_id"])
            if confirmed:
                orchestrator.enqueue_mirror(result["revision_id"], result["unit_id"])
                summary["plan_ready"] += 1
                summary["mirror_enqueued"] += 1
            else:
                summary["needs_review"] += 1
        elif result.get("status") == "needs_review":
            summary["needs_review"] += 1
        if progress_callback is not None:
            progress_callback(
                50, f"发现作品「{result.get('work_title') or result.get('boundary') or ''}」",
                {
                    "phase": "discovery_unit",
                    "status": result.get("status"),
                    "work_title": result.get("work_title") or "",
                    "boundary": result.get("boundary") or "",
                },
            )

    def rate_limiter() -> None:
        _wait_for_backpressure(should_cancel)

    try:
        results = engine.run(
            should_cancel=should_cancel,
            progress_callback=progress_callback,
            on_unit=on_unit,
            rate_limiter=rate_limiter,
        )
    except DiscoveryCancelled as exc:
        raise JobCancelledError(str(exc) or "任务已取消") from None
    except _RISK_ABORT_TYPES as exc:
        # 扫描中途触发来源级风控/限流/冷却：整棵扫描立即停止，转延后
        # 等待冷却（不标 succeeded、不标 failed）。真实 OpenListClient 已
        # 上报 record_failure 写入冷却，这里取 cooldown_until 作为重试时间；
        # 未上报（测试假客户端等）时按阈值兜底。
        raise JobDeferredError(
            until_unix=_defer_until(health_key),
            message=_DEFER_MESSAGE,
        ) from None

    failed_paths = list(getattr(engine, "failed_paths", []))
    summary["failed_count"] = len(failed_paths)
    summary["failed_paths"] = failed_paths[:100]
    if failed_paths and progress_callback is not None:
        progress_callback(
            100, f"扫描完成，{len(failed_paths)} 个目录暂不可用（再次导入可重试）",
            {"phase": "discovery_done", "failed_count": len(failed_paths)},
        )

    return {
        "root_id": root_id,
        "generation": generation,
        "units": results,
        "summary": summary,
    }


def register_discovery_handler() -> None:
    register("discovery_scan", handle_discovery_scan)
