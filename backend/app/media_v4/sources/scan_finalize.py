"""SourceScan 的离线 draft finalizer（C 段共享实现）。

从 durable_scan 闭包中抽出：解析、归一化与 draft 创建都只依赖数据库中
已持久化的 evidence，不依赖进程内对象。进程失联后的恢复路径（完整证据
续跑）与首次执行共用同一实现，保证幂等语义只有一份。
"""

from __future__ import annotations

from app.media_v4.persistence.database import V4Database
from app.media_v4.sources.scanner import SourceScanCancelled


def draft_finalizer(
    database: V4Database,
    *,
    revision_id: str,
    source_display_name: str = "",
    root_id: str,
    scan_id: str,
):
    """返回 finalize(evidence, should_cancel, on_progress) 闭包。

    revision_id 为空时返回 None：本次扫描不创建草稿，evidence 持久化即
    终态。取消合同：draft 开始创建前有最后一道取消检查，草稿一旦开始
    创建就让扫描自然完成，避免「cancelled 状态却挂着 draft」。
    """

    normalized_revision = str(revision_id or "").strip()
    if not normalized_revision:
        return None
    display_name = str(source_display_name or "")

    def finalize(evidence: list, should_cancel=None, on_progress=None) -> None:
        from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
        from app.media_v4.persistence.repositories import V4Repository
        from app.media_v4.revisions.service import V4RevisionService

        with database.connect() as conn:
            root = conn.execute(
                """
                SELECT provider, source_locator, playback_locator, route_id,
                       display_name, root_container, source_mode
                FROM source_roots WHERE root_id = ?
                """,
                (root_id,),
            ).fetchone()
        if root is None:
            raise ValueError("来源根记录不存在，请重新扫描")
        root_container = str(root["root_container"] or "")
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        parser = V4Parser()
        repository = V4Repository(database)
        # 大型目录树分批解析并持续上报进度，但编号归一化必须在完整来源批次
        # 上执行：128 条只是数据库写入边界，不能把同一季度切成两个语义批次。
        # 解析完成后再统一归一化并分批落盘；取消仍在每个解析/写入批次检查。
        raw_parsed = []
        total = len(evidence)
        if on_progress is not None:
            on_progress(stage="parsing", processed_count=0, total_count=total)
        progress_checkpoint_size = 16
        for offset in range(0, total, 128):
            if should_cancel is not None and should_cancel():
                # 已解析的前缀属于不可变审计事实。取消时先按当前已观察批次
                # 完成保守归一化并落盘，但绝不构建 revision；这样既不会把
                # 半成品导入媒体库，也不会让用户等待过的识别工作完全消失。
                if raw_parsed:
                    partial = normalize_batch_parsed_facts(raw_parsed)
                    repository.save_parsed_facts_bulk(
                        [facts for _evidence, facts in partial]
                    )
                raise SourceScanCancelled()
            batch = []
            for index, item in enumerate(evidence[offset : offset + 128], start=1):
                batch.append((item, parser.parse(item, root_container=root_container)))
                if on_progress is not None and (
                    index % progress_checkpoint_size == 0 or offset + index == total
                ):
                    on_progress(
                        stage="parsing",
                        processed_count=offset + index,
                        total_count=total,
                    )
            raw_parsed.extend(batch)
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        # 进入完整归一化前显式上报 normalizing，让界面不误以为还在读取清单。
        if on_progress is not None:
            on_progress(stage="normalizing", processed_count=total, total_count=total)
        parsed = normalize_batch_parsed_facts(raw_parsed)
        for offset in range(0, total, 128):
            if should_cancel is not None and should_cancel():
                raise SourceScanCancelled()
            repository.save_parsed_facts_bulk(
                [facts for _evidence, facts in parsed[offset : offset + 128]]
            )
        if should_cancel is not None and should_cancel():
            raise SourceScanCancelled()
        if on_progress is not None:
            on_progress(stage="preparing_preview", processed_count=total, total_count=total)
        V4RevisionService(database).create_draft(
            normalized_revision,
            parsed,
            root_id=root_id,
            scan_id=scan_id,
            source_provider=str(root["provider"] or (evidence[0].provider if evidence else "local")),
            source_metadata={
                "display_name": display_name or str(root["display_name"] or ""),
                "source_locator": str(root["source_locator"] or ""),
                "playback_locator": str(root["playback_locator"] or ""),
                "route_id": str(root["route_id"] or ""),
                "root_container": root_container,
            },
            source_mode=str(root["source_mode"] or ""),
            _evidence_already_persisted=True,
            _facts_already_persisted=True,
        )

    return finalize
