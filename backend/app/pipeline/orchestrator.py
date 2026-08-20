"""流水线编排器。

- Source Catalog 提交目录后同事务创建/合并 discovery scan job；
- revision confirmed 后创建 mirror job；mirror 成功后、单元 closure 时创建 scrape job；
- 待处理 recognition/mirror/scrape jobs >= 50 时 scanner 暂停领取（背压）；
- 各阶段 resource_key 互斥：scan 每 root、mirror 每 revision、scrape 全局单通道。
"""

from __future__ import annotations

from app.catalog.closure import is_boundary_complete
from app.db.database import get_connection
from app.jobs import store as job_store
from app.pipeline.handlers import register_pipeline_handlers

#: 背压水位
BACKOFF_QUEUE_LIMIT = 50
BACKOFF_RECOVER_LIMIT = 25

_PIPELINE_HANDLERS_REGISTERED = False


def _ensure_handlers() -> None:
    global _PIPELINE_HANDLERS_REGISTERED
    if not _PIPELINE_HANDLERS_REGISTERED:
        register_pipeline_handlers()
        _PIPELINE_HANDLERS_REGISTERED = True


def queue_depth() -> int:
    """当前 queued/running 的 recognition/mirror/scrape 任务数（背压水位）。"""
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM jobs
        WHERE job_type IN ('mirror_revision', 'scrape_revision')
          AND status IN ('queued', 'running')
        """
    ).fetchone()
    return int(row[0]) if row else 0


#: 滞回状态：进入背压后必须降到恢复线以下才解除（避免水位抖动反复开关）
_BACKOFF_ACTIVE = False


def should_backoff() -> bool:
    """背压滞回：队列 >= 50 进入背压，降到 <= 25 才解除。"""
    global _BACKOFF_ACTIVE
    depth = queue_depth()
    if _BACKOFF_ACTIVE:
        if depth <= BACKOFF_RECOVER_LIMIT:
            _BACKOFF_ACTIVE = False
        return True
    if depth >= BACKOFF_QUEUE_LIMIT:
        _BACKOFF_ACTIVE = True
        return True
    return False


def reset_backoff() -> None:
    """测试辅助：清除滞回状态。"""
    global _BACKOFF_ACTIVE
    _BACKOFF_ACTIVE = False


def enqueue_scan(
    root_id: str,
    generation: int,
    source_id: str = "",
    input_path: str = "",
    scan_mode: str = "incremental",
    scan_channel: str = "",
) -> str:
    """创建/合并 discovery scan job。

    - 锁粒度：OpenList 连接（source_id）共享一把锁（scan:conn:{source_id}），
      同一连接下的多个 root 串行扫描（2 req/s 限流是连接级）；不同连接可并行；
    - 115/百度目录树 TXT 的 input_path 随 payload 持久化，重启后仍可恢复；
    - scan_channel（HYB-1）：显式扫描通道（openlist/snapshot_pan115/
      snapshot_baidu/local），为空时 discovery handler 按 source_id 前缀
      fallback——旧 job 不带该字段仍可正常恢复。
    """
    _ensure_handlers()
    from app.catalog import store as catalog_store

    catalog_store.prepare_scan(root_id, generation=generation, mode=scan_mode)
    conn_key = f"scan:conn:{source_id or 'default'}"
    existing = job_store.list_jobs(job_type="discovery_scan", status="queued", limit=100)
    for job in existing:
        if (
            job.resource_key == conn_key
            and job.payload.get("root_id") == root_id
            and int(job.payload.get("generation") or 0) >= generation
        ):
            return job.job_id
    job = job_store.create_job(
        job_type="discovery_scan",
        resource_key=conn_key,
        payload={
            "root_id": root_id,
            "generation": generation,
            "source_id": source_id,
            "input_path": input_path,
            "scan_mode": scan_mode,
            "scan_channel": scan_channel,
        },
    )
    return job.job_id


def enqueue_mirror(revision_id: str, unit_id: str) -> str:
    """revision confirmed 后创建 mirror job（每 revision 幂等 get-or-create）。

    已有任意状态（queued/running/succeeded/failed/cancelled）的 mirror job 都
    复用同一 job 身份（durable jobs 自带 attempt/retry），不创建第二个业务任务；
    并发 confirm / crash 后重确认只会得到同一个 job_id。
    """
    _ensure_handlers()
    job, _created = job_store.get_or_create_job(
        job_type="mirror_revision",
        resource_key=f"mirror:{revision_id}",
        payload={"revision_id": revision_id, "unit_id": unit_id},
    )
    return job.job_id


def unit_is_closed(unit_id: str) -> bool:
    """closure：unit 的 boundary 下所有当前有效目录必须全部 complete 才收口
    （唯一实现 catalog.closure.is_boundary_complete；failed 同样阻塞）。"""
    conn = get_connection()
    unit = conn.execute(
        "SELECT * FROM media_units WHERE unit_id = ?", (unit_id,)
    ).fetchone()
    if unit is None or not unit["boundary"]:
        return False
    return is_boundary_complete(unit["root_id"], unit["boundary"])


def enqueue_scrape(revision_id: str, source: str, *, unit_id: str = "") -> str:
    """mirror 成功且单元 closure 后创建 scrape job（全局单通道）。"""
    _ensure_handlers()
    job = job_store.create_job(
        job_type="scrape_revision",
        resource_key="scrape:global",
        payload={"revision_id": revision_id, "source": source, "unit_id": unit_id},
    )
    return job.job_id


def enqueue_library_rebuild(*, unit_id: str = "") -> str:
    """刮削完成后重建媒体库索引（全局单通道，合并入队）。

    Module 5：library:global 最多 1 running + 1 trailing queued——
    大量刮削连续完成不积累几十个全局 rebuild；一个 pending rebuild
    即可覆盖最新 SQLite 状态。
    """
    _ensure_handlers()
    job, _created = job_store.enqueue_coalesced_job(
        job_type="library_rebuild",
        resource_key="library:global",
        payload={"unit_id": unit_id},
    )
    return job.job_id
