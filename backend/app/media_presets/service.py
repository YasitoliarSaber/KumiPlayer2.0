import hashlib
import os
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock

from fastapi import HTTPException, UploadFile

from app.core.paths import get_data_dir, sanitize_filename
from app.import_plan.diff import compute_diff
from app.import_plan.incremental import build_incremental_plan, merge_incremental_plan
from app.import_plan.service import build_preview
from app.import_plan.store import load_import_plan, save_diff_result, save_import_plan
from app.integrations.openlist.providers import compat_ingest, compat_provider
from app.media_presets.models import MediaLibraryPreset, MediaTreeVersion
from app.media_presets.store import get_presets_root, list_presets, save_preset, version_archive_dir
from app.raw.store import load_raw_snapshot, save_raw_snapshot

_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_ALLOWED_SUFFIXES = {".txt", ".tree", ".log"}
_PAN115_TREE_LINE = re.compile(r"^(?:\|——.+|(?:\| )*\|-.+)$", re.MULTILINE)
_BAIDU_TREE_LINE = re.compile(
    r"^(?:(?:│ {2,3})|(?: {3,4}))*[├└]─{1,2}\s?.+$",
    re.MULTILINE,
)

# Confirmation authority lock：在单进程 FastAPI 生命周期内串行化
# TXT baseline target 前进与 root-generation confirm，避免 target 在
# validation 与 mutation 之间前进造成 stale root 部分确认。
CONFIRMATION_AUTHORITY_LOCK = RLock()


def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def openlist_preset_state(catalog_root_id: str, *, require_all: bool = False) -> dict:
    """OpenList 来源卡状态投影：从 SourceRoot 的 current MediaUnits / revisions /
    durable jobs 投影识别单元数、需处理数与是否已建立媒体库，**绝不依赖不存在
    的 current_plan_id**（来源卡代表 SourceRoot 生命周期，不是单个 ImportPlan）。

    状态机沿每个 current unit 自己的 ``mirror → scrape → library`` job chain
    判断（library rebuild 的 ``succeeded`` 才代表 LibraryIndex 已经发布）：

    - draft revision → attention（人工确认入口）；
    - mirror / scrape / library 任一 durable stage ``failed/cancelled`` → attention；
    - 只有**至少一个 current unit 真正到达 library_rebuild=succeeded** 才
      ``is_library_indexed=true``；``needs_review`` unit 不阻塞其他已成功发布
      的 unit（保持人工确认与自动发布并存）。
    """
    import json

    from app.db.database import get_connection

    empty = {"unit_count": 0, "attention_count": 0, "is_library_indexed": False}
    if not catalog_root_id:
        return empty
    conn = get_connection()
    units = conn.execute(
        "SELECT unit_id, current_revision_id, status, boundary, work_key "
        "FROM media_units WHERE root_id = ?",
        (catalog_root_id,),
    ).fetchall()
    if not units:
        return empty
    unit_count = len(units)
    attention_count = 0
    indexed = False
    published_count = 0

    def _latest_stage_job(job_type: str, resource_key: str):
        return conn.execute(
            "SELECT * FROM jobs WHERE job_type = ? AND resource_key = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (job_type, resource_key),
        ).fetchone()

    def _job_result(row) -> dict:
        try:
            return (json.loads(row["payload"] or "{}").get("result") or {})
        except (TypeError, ValueError):
            return {}

    def _latest_scrape_job(revision_id: str):
        """本 revision 的 scrape job：mirror result 链优先，payload 精确兜底。"""
        mirror_row = _latest_stage_job("mirror_revision", f"mirror:{revision_id}")
        if mirror_row is not None:
            scrape_id = _job_result(mirror_row).get("scrape_job_id") or ""
            if scrape_id:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (scrape_id,)
                ).fetchone()
                if row is not None:
                    return row
        return conn.execute(
            "SELECT * FROM jobs WHERE job_type = 'scrape_revision' AND payload LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f'%"revision_id": "{revision_id}"%',),
        ).fetchone()

    def _latest_library_job(unit_id: str, revision_id: str):
        """本 unit 的 library rebuild job：scrape result 链优先，payload 精确兜底。"""
        scrape_row = _latest_scrape_job(revision_id)
        if scrape_row is not None:
            lib_id = _job_result(scrape_row).get("library_rebuild_job") or ""
            if lib_id:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (lib_id,)
                ).fetchone()
                if row is not None:
                    return row
        return conn.execute(
            "SELECT * FROM jobs WHERE job_type = 'library_rebuild' AND payload LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f'%"unit_id": "{unit_id}"%',),
        ).fetchone()

    needs_review_units: list[dict] = []
    # RWK-40（P0）：media_units.current_revision_id 是「上一次已确认版本」的指针，
    # 不代表当前 generation 的 draft / needs_review 事实。require_all 的 Provider
    # TXT 必须让当前 target generation 的可执行状态覆盖旧指针：
    # - unit 在 target generation 有 draft revision → attention / not indexed；
    # - unit.status == needs_review → attention（不管旧 current_revision_id）；
    # - 都没有待处理事实时，才允许沿旧 current_revision_id 的 published pipeline 判断。
    from app.catalog import store as catalog_store

    _root = catalog_store.get_source_root(catalog_root_id)
    target = int(getattr(_root, "baseline_target_generation", 0) or 0) if _root else 0
    for unit in units:
        unit_id = str(unit["unit_id"] or "")
        unit_status = str(unit["status"] or "")
        if target > 0:
            draft_row = conn.execute(
                "SELECT COUNT(*) AS c FROM import_revisions r "
                "JOIN media_units u ON u.unit_id = r.unit_id "
                "WHERE u.unit_id = ? AND u.root_id = ? "
                "AND r.source_generation = ? AND r.status = 'draft'",
                (unit_id, catalog_root_id, target),
            ).fetchone()
            if draft_row and int(draft_row["c"] or 0) > 0:
                # 当前 generation 有待确认 draft → 不得沿旧指针显示已发布
                attention_count += 1
                continue
        if unit_status == "needs_review":
            # 当前 generation 需要人工处理，不管旧 current_revision_id 指向什么
            attention_count += 1
            needs_review_units.append(
                {
                    "unit_id": unit_id,
                    "boundary": str(unit["boundary"] or ""),
                    "work_key": str(unit["work_key"] or ""),
                    "status": unit_status,
                }
            )
            continue
        revision_id = str(unit["current_revision_id"] or "")
        if not revision_id:
            continue
        rev = conn.execute(
            "SELECT status FROM import_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if rev is None:
            # current_revision_id 指向的 revision 行缺失（数据不一致）→ attention
            attention_count += 1
            continue
        rev_status = str(rev["status"] or "")
        if rev_status == "draft":
            attention_count += 1
            continue
        if rev_status in ("failed", "cancelled", "superseded"):
            attention_count += 1
            continue
        if rev_status not in ("confirmed", "executed"):
            continue
        mirror = _latest_stage_job("mirror_revision", f"mirror:{revision_id}")
        if mirror is None:
            # 确认后尚未入队：等待，不计 attention 也不发布
            continue
        mirror_status = str(mirror["status"] or "")
        if mirror_status in ("failed", "cancelled"):
            attention_count += 1
            continue
        if mirror_status in ("queued", "running"):
            continue
        if mirror_status != "succeeded":
            continue
        scrape = _latest_scrape_job(revision_id)
        if scrape is None:
            # 链路尚未推进（或无需刮削）：等待
            continue
        scrape_status = str(scrape["status"] or "")
        if scrape_status in ("failed", "cancelled"):
            attention_count += 1
            continue
        if scrape_status in ("queued", "running"):
            continue
        if scrape_status != "succeeded":
            continue
        library = _latest_library_job(unit_id, revision_id)
        if library is None:
            # library rebuild 尚未入队：等待
            continue
        library_status = str(library["status"] or "")
        if library_status in ("failed", "cancelled"):
            attention_count += 1
            continue
        if library_status in ("queued", "running"):
            continue
        if library_status == "succeeded":
            # LibraryIndex 已经发布（handle_library_rebuild 完成）
            published_count += 1
            indexed = True
    if require_all:
        indexed = unit_count > 0 and published_count == unit_count and attention_count == 0
    return {
        "unit_count": unit_count,
        "attention_count": attention_count,
        "is_library_indexed": indexed,
        "needs_review_units": needs_review_units,
    }


def ensure_provider_source_root(
    *,
    provider: str,
    local_mount_root: str,
    import_family: str,
    import_scope: str = "",
    remote_locator: str = "",
) -> str:
    """RWK-17：建立/复用 Provider（pan115/baidu）SourceRoot，返回 root_id。

    bootstrap-tree 与现有 115/百度 TXT 导入路径共用同一实现：
    - Provider source identity = {provider}-{sha256(local_mount_root)[:16]}，
      与 OpenList 完全解耦；
    - root 复用/创建遵循 lifecycle 覆盖解析（exact/ancestor/promote/create）；
    - 同一本地挂载根永远只产生一个 Provider root（幂等）。

    0 OpenList 请求；root 建立后 preset.catalog_root_id 由调用方关联。
    """
    from app.catalog import lifecycle
    from app.catalog import store as catalog_store
    from app.db.database import init_db

    init_db()
    provider = (provider or "").strip().lower()
    if provider not in {"pan115", "baidu"}:
        raise ValueError("provider 仅支持 pan115 或 baidu")
    mount_root = (local_mount_root or "").strip()
    if not mount_root:
        raise ValueError("缺少本地挂载根")
    effective_root = Path(mount_root).expanduser()
    # 归一化后再哈希：尾斜杠/大小写差异不产生第二套 source（与 preset 复用判定一致）
    norm_key = str(effective_root).strip().rstrip("\\/").casefold()

    provider_key = hashlib.sha256(
        norm_key.encode("utf-8")
    ).hexdigest()[:16]
    source_id = f"{provider}-{provider_key}"
    catalog_store.create_source(
        source_id=source_id,
        source_type=provider,
        provider_id=provider,
        ingest_method="directory_tree",
        connection_key=source_id,
        display_name="115 目录树" if provider == "pan115" else "百度目录树",
    )

    normalized = remote_locator.strip() or (
        "/" + str(effective_root).replace("\\", "/").strip("/")
    )
    resolution = lifecycle.resolve_root_for_import(
        source_id,
        normalized,
        import_family=(import_family or "anime").strip(),
        import_scope=(import_scope or "").strip(),
        local_locator=str(effective_root),
    )
    if resolution.action == "create":
        root = catalog_store.create_source_root(
            source_id=source_id,
            remote_locator=normalized,
            local_locator=str(effective_root),
            import_family=(import_family or "anime").strip(),
            import_scope=(import_scope or "").strip(),
            scan_policy="standard",
        )
    elif resolution.action == "promote_parent":
        resolution = lifecycle.promote_parent_root(
            source_id,
            normalized,
            local_locator=str(effective_root),
            import_family=(import_family or "anime").strip(),
            import_scope=(import_scope or "").strip(),
            child_root_ids=resolution.covered_root_ids,
        )
        root = catalog_store.get_source_root(resolution.canonical_root_id)
        if root is None:
            raise ValueError("来源根归并失败，请重试")
    else:
        root = catalog_store.get_source_root(resolution.canonical_root_id)
        if root is None:
            raise ValueError("来源根解析失败，请重新导入")
        if resolution.action == "reuse_ancestor":
            # 只在显式提供了非空语义时更新元数据——空 scope 不得清空共享
            # 祖先 root 的既有 scope（季节/子目录导入最后写入者不得胜出）
            family = (import_family or "anime").strip()
            scope = (import_scope or "").strip()
            catalog_store.update_root_metadata(
                root.root_id,
                import_family=family,
                import_scope=scope,
            )
    return root.root_id


def bootstrap_provider_catalog_from_tree(
    *,
    provider: str,
    tree_archive: str,
    local_mount_root: str,
    import_family: str,
    import_scope: str = "",
    remote_locator: str = "",
) -> dict:
    """RWK-21：把已归档的目录树 TXT 建立为 Provider SourceRoot 的完整 Source Catalog baseline。

    语义（路线 A）：TXT → Provider Source Catalog 全量基线，0 OpenList 请求。
    - ensure/复用 Provider source + root（幂等，同一挂载根不产生第二套）；
    - enqueue durable discovery job（scan_channel=snapshot_{provider}, full）——
      source_nodes / source_directories（complete frontier）/
      media_units / revisions 由 durable handler 从归档 TXT 建立；
    - 返回 {"root_id", "job_id", "generation", "source_id"}。

    调用方（multipart create / import-local-tree）负责 preset.catalog_root_id 关联。
    归档 TXT 已落在 KumiPlayer 数据目录（MediaTreeVersion 承载），restart 可恢复。
    """
    from app.catalog import store as catalog_store
    from app.db.database import init_db
    from app.pipeline import orchestrator

    init_db()
    root_id = ensure_provider_source_root(
        provider=provider,
        local_mount_root=local_mount_root,
        import_family=import_family,
        import_scope=import_scope,
        remote_locator=remote_locator,
    )
    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise ValueError("来源根建立失败")
    generation = catalog_store.bump_generation(root_id)
    # RWK-30：新 TXT baseline 入队即把 target 前进到该 generation——
    # 旧 completed fact 不再代表当前基线（ready 失效），直到新版本完整完成。
    catalog_store.set_baseline_target(root_id, generation)
    job_id = orchestrator.enqueue_scan(
        root_id,
        generation,
        root.source_id,
        input_path=tree_archive,
        scan_mode="full",
        scan_channel=f"snapshot_{provider}",
    )
    return {
        "root_id": root_id,
        "job_id": job_id,
        "generation": generation,
        "source_id": root.source_id,
    }


def bootstrap_provider_catalog_sync(
    *,
    provider: str,
    tree_archive: str,
    local_mount_root: str,
    import_family: str,
    import_scope: str = "",
    remote_locator: str = "",
) -> dict:
    """RWK-34：同步执行 TXT → Source Catalog baseline（**不创建 queued job**）。

    与 ``bootstrap_provider_catalog_from_tree`` 的区别：不在 durable queue 创建
    discovery job，而是直接构造 payload 单次调用 handler——消除「API 同步调
    handler + worker 再 claim 同一 queued job」的双执行窗口与重复 draft revision。

    TXT 导入是同步用户流程：成功即数据落库（Source Catalog 持久化），
    失败当场可见可重试，无需 restart 恢复（v2 更新同理）。若调用方需要
    durable 后台/重启恢复语义，请用 ``bootstrap_provider_catalog_from_tree``。

    返回 {"root_id", "generation", "source_id", "summary", "revision_ids"}。
    """
    from app.catalog import store as catalog_store
    from app.db.database import init_db
    from app.pipeline.discovery_handler import handle_discovery_scan

    init_db()
    root_id = ensure_provider_source_root(
        provider=provider,
        local_mount_root=local_mount_root,
        import_family=import_family,
        import_scope=import_scope,
        remote_locator=remote_locator,
    )
    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise ValueError("来源根建立失败")
    generation = catalog_store.bump_generation(root_id)
    catalog_store.set_baseline_target(root_id, generation)
    # RWK-37：同步执行必须与 enqueue_scan 等价地先 prepare_scan(full)——
    # 否则既有 complete 目录不在 pending frontier，engine 不会重扫，
    # 新版本 TXT（v2 新增作品）会被遗漏（仅扫 root）。
    catalog_store.prepare_scan(root_id, generation=generation, mode="full")
    payload = {
        "root_id": root_id,
        "generation": generation,
        "source_id": root.source_id,
        "input_path": tree_archive,
        "scan_mode": "full",
        "scan_channel": f"snapshot_{provider}",
    }
    summary = handle_discovery_scan(payload).get("summary", {})
    # 收集该 generation 的全部 draft revision ids（root 级确认身份）
    from app.db.database import get_connection

    rows = get_connection().execute(
        """
        SELECT r.revision_id FROM import_revisions r
        JOIN media_units u ON u.unit_id = r.unit_id
        WHERE u.root_id = ? AND r.source_generation = ? AND r.status = 'draft'
        ORDER BY r.created_at ASC
        """,
        (root_id, generation),
    ).fetchall()
    revision_ids = [str(r["revision_id"]) for r in rows]
    return {
        "root_id": root_id,
        "generation": generation,
        "source_id": root.source_id,
        "summary": summary,
        "revision_ids": revision_ids,
    }


def preset_to_dict(preset: MediaLibraryPreset) -> dict:
    data = asdict(preset)
    data["is_library_indexed"] = preset.lifecycle_status == "ready"
    # OpenList 来源卡：is_library_indexed 与识别单元/需处理数来自 SourceRoot 的
    # current MediaUnits/revisions/jobs 投影，不再用目录树 lifecycles 的 ready
    # 判定（来源卡从不经过 directory_tree 的 lifecycle 推进）。
    authority = str(preset.execution_authority or "")
    durable_source_card = authority == "durable_root" and preset.source in {"pan115", "baidu"}
    if preset.catalog_root_id and (preset.source == "openlist" or durable_source_card):
        state = openlist_preset_state(
            preset.catalog_root_id,
            require_all=durable_source_card,
        )
        data["is_library_indexed"] = state["is_library_indexed"]
        data["openlist_unit_count"] = state["unit_count"]
        data["openlist_attention_count"] = state["attention_count"]
        data["openlist_needs_review_units"] = state.get("needs_review_units") or []
        if durable_source_card:
            if state["is_library_indexed"]:
                data["lifecycle_status"] = "ready"
            elif state["attention_count"] > 0:
                data["lifecycle_status"] = "needs_attention"
    # RWK-39（P0-2）：durable confirmation identity 可恢复投影——
    # 一旦 preset 进入 Source Catalog durable authority（catalog_root_id 关联
    # **或** execution_authority=durable_root——含 baseline 失败且 root 未写入
    # 的场景），restart 后仍从 preset 恢复身份与阻断状态，绝不重新识别为
    # legacy-executable。
    is_durable = authority == "durable_root" or bool(preset.catalog_root_id)
    if is_durable:
        gen, ready = preset_confirmation_state(preset.catalog_root_id or "")
        data["confirmation_root_id"] = preset.catalog_root_id or None
        data["confirmation_generation"] = gen
        data["confirmation_ready"] = ready
        # RWK-40（P0）：durable TXT 且当前 target generation 仍有 draft
        # （confirmation_ready=true）→ 不得对用户宣称「已全部正式完成」。
        # current_revision_id 可能仍指上一代 published 指针，必须被当前 draft 覆盖。
        # 只把已投影为 ready 的状态拉回 needs_attention；非 ready（如 rebind 后
        # 的 draft）保持原值，避免过度改写既有 lifecycle 语义。
        if preset.catalog_root_id and preset.source in {"pan115", "baidu"} and ready:
            data["is_library_indexed"] = False
            if data["lifecycle_status"] == "ready":
                data["lifecycle_status"] = "needs_attention"
        state = str(preset.confirmation_state or "")
        if authority == "durable_root" and state in ("failed", "pending"):
            # hard exception / 未完成基线：身份存在但确认被硬阻断
            data["confirmation_blocked"] = True
        elif preset.catalog_root_id and not ready:
            # 基线未完成/已消费：同样不可确认（legacy 绝不回退）
            data["confirmation_blocked"] = True
        else:
            data["confirmation_blocked"] = not ready
    return data


def preset_confirmation_state(catalog_root_id: str) -> tuple[int, bool]:
    """恢复 TXT durable confirmation 身份：(generation, ready)。

    ready = 基线已真实完成（completed == target > 0）且该 generation 仍有
    draft revisions 可确认。draft 全部确认/执行后 ready=False（避免重复确认）。
    """
    from app.catalog import store as catalog_store
    from app.db.database import get_connection

    root = catalog_store.get_source_root(catalog_root_id)
    if root is None:
        return 0, False
    target = int(getattr(root, "baseline_target_generation", 0) or 0)
    completed = int(getattr(root, "baseline_completed_generation", 0) or 0)
    if target <= 0 or completed != target:
        return target, False
    row = get_connection().execute(
        """
        SELECT COUNT(*) AS c FROM import_revisions r
        JOIN media_units u ON u.unit_id = r.unit_id
        WHERE u.root_id = ? AND r.source_generation = ? AND r.status = 'draft'
        """,
        (catalog_root_id, target),
    ).fetchone()
    return target, bool(row and int(row["c"]) > 0)


_LIFECYCLE_ORDER = {
    "draft": 0,
    "confirmed": 1,
    "mirrored": 2,
    "needs_attention": 3,
    "ready": 4,
}


def mark_preset_lifecycle(plan_id: str, status: str) -> MediaLibraryPreset | None:
    """按当前计划推进目录树档案状态，旧版本任务不能覆盖新版状态。"""
    if status not in _LIFECYCLE_ORDER:
        raise ValueError(f"不支持的媒体库状态: {status}")
    preset = next((item for item in list_presets() if item.current_plan_id == plan_id), None)
    if preset is None:
        return None
    current_rank = _LIFECYCLE_ORDER.get(preset.lifecycle_status, 0)
    target_rank = _LIFECYCLE_ORDER[status]
    if status == "needs_attention" or target_rank >= current_rank:
        preset.lifecycle_status = status
        preset.updated_at = now_iso()
        save_preset(preset)
    return preset


def _number_label(number: int) -> str:
    values = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}
    return values.get(number, str(number))


def next_preset_name(import_family: str, import_scope: str) -> str:
    prefix = "新番" if import_scope == "seasonal" else "动画" if import_family == "anime" else "剧集"
    count = sum(
        1 for item in list_presets()
        if item.import_family == import_family and item.import_scope == import_scope
    )
    return f"{prefix}{_number_label(count + 1)}"


def _normalized_library_root(value: str) -> str:
    if not value:
        return ""
    normalized = os.path.abspath(os.path.expanduser(value))
    return os.path.normcase(os.path.normpath(normalized))


def find_matching_preset(
    source: str,
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    tree_name: str = "",
    sha256: str = "",
) -> MediaLibraryPreset | None:
    """按解析后的实际媒体根复用卡片，TXT 文件名不参与媒体库身份。"""
    expected_root = _normalized_library_root(source_root)
    candidates = [
        item
        for item in list_presets()
        if item.source == source
        and item.import_family == import_family
        and item.import_scope == import_scope
        and _normalized_library_root(item.source_root) == expected_root
    ]
    if sha256:
        exact = next((item for item in candidates if find_version_by_sha256(item, sha256)), None)
        if exact is not None:
            return exact
    return min(candidates, key=lambda item: (item.created_at or item.updated_at, item.preset_id)) if candidates else None


def _tree_name_anchor(value: str) -> str:
    stem = Path(value).stem.casefold().strip()
    stem = re.sub(r"(?:[_\-\s]*(?:文件目录|目录树))?[_\-\s]*\d{8,14}$", "", stem)
    return re.sub(r"[_\-\s]+", "", stem)


def find_version_by_sha256(preset: MediaLibraryPreset, sha256: str) -> MediaTreeVersion | None:
    return next((item for item in preset.versions if sha256 and item.sha256 == sha256), None)


def snapshots_have_same_media(left, right) -> bool:
    """比较解析后的视频集合，忽略 TXT 文件名、编码和换行差异。"""
    from app.recognition.resource_type import VIDEO_EXTS

    def signature(snapshot) -> tuple[tuple[str, int, str], ...]:
        return tuple(sorted(
            (
                os.path.normcase(os.path.normpath(item.relative_path or item.real_path)),
                int(item.size or 0),
                item.content_fingerprint or "",
            )
            for item in snapshot.files
            if item.is_file and item.ext.lower() in VIDEO_EXTS
        ))

    return bool(left and right) and signature(left) == signature(right)


def discard_candidate_snapshot(snapshot_id: str) -> None:
    """删除尚未激活到任何卡片的解析快照。"""
    if not snapshot_id:
        return
    path = get_data_dir() / "raw_snapshots" / f"{snapshot_id}.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def discard_archived_version(version: MediaTreeVersion) -> None:
    """丢弃尚未写入预设索引的临时归档，并清理空的临时预设目录。"""
    if version.archive_path:
        archive = get_data_dir() / version.archive_path
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            return
    temporary_root = get_presets_root() / version.preset_id
    try:
        versions_dir = temporary_root / "versions"
        if versions_dir.is_dir() and not any(versions_dir.iterdir()):
            versions_dir.rmdir()
        if temporary_root.is_dir() and not any(temporary_root.iterdir()):
            temporary_root.rmdir()
    except OSError:
        pass



def move_archived_version(version: MediaTreeVersion, preset_id: str) -> Path:
    """把识别阶段的临时归档转入已存在媒体库的受控版本目录。"""
    archive = get_data_dir() / version.archive_path
    destination = version_archive_dir(preset_id) / archive.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(archive, destination)
    old_preset_id = version.preset_id
    version.preset_id = preset_id
    version.archive_path = destination.relative_to(get_data_dir()).as_posix()
    temporary_root = get_presets_root() / old_preset_id
    try:
        versions_dir = temporary_root / "versions"
        if versions_dir.is_dir() and not any(versions_dir.iterdir()):
            versions_dir.rmdir()
        if temporary_root.is_dir() and not any(temporary_root.iterdir()):
            temporary_root.rmdir()
    except OSError:
        pass
    return destination


def _validate_tree_bytes(data: bytes) -> None:
    if not data:
        raise HTTPException(status_code=400, detail="目录树文件为空")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="目录树文件超过 64 MB")


def _archive_tree_bytes(
    preset_id: str,
    original_name: str,
    data: bytes,
    source_tree_path: str = "",
    provider_id: str = "",
    ingest_method: str = "",
    source_route_id: str = "",
) -> tuple[MediaTreeVersion, Path]:
    version_id = uuid.uuid4().hex
    safe_name = sanitize_filename(original_name)
    archive = version_archive_dir(preset_id) / f"{version_id}-{safe_name}"
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, archive)
    relative = archive.relative_to(get_data_dir()).as_posix()
    version = MediaTreeVersion(
        version_id=version_id,
        preset_id=preset_id,
        original_name=original_name,
        archive_path=relative,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        created_at=now_iso(),
        source_tree_path=source_tree_path,
        provider_id=provider_id,
        ingest_method=ingest_method,
        source_route_id=source_route_id,
    )
    return version, archive


async def archive_upload(preset_id: str, upload: UploadFile) -> tuple[MediaTreeVersion, Path]:
    original_name = Path(upload.filename or "目录树.txt").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail="仅支持 .txt、.tree 或 .log 目录树文件")
    data = await upload.read(_MAX_UPLOAD_BYTES + 1)
    _validate_tree_bytes(data)
    return _archive_tree_bytes(preset_id, original_name, data)


_MOUNT_FS_ERROR_HINT = (
    "目录树文件所在挂载盘不支持文件系统解析，请确认挂载在线后重试，"
    "或在本地磁盘选择该 TXT 副本"
)


def _is_mount_fs_error(exc: OSError) -> bool:
    """判断 OSError 是否来自挂载层拒绝文件系统解析。

    WinError 1005 = ERROR_UNRECOGNIZED_VOLUME：115 挂载盘（WinFSP）允许
    打开与读取文件，却不支持真实路径解析（等价 GetFinalPathNameByHandle）。
    这类错误不是“文件不存在”或“文件损坏”，需要与普通 OSError 区分。
    ``OSError(1005, ...)`` 手工构造时 errno=1005，真实 WinError 时
    winerror=1005，两个属性都判断。
    """
    return exc.errno == 1005 or getattr(exc, "winerror", None) == 1005


def _read_dropped_tree(path: Path) -> bytes:
    if path.suffix.lower() != ".txt":
        raise HTTPException(status_code=400, detail="拖放导入仅支持 .txt 目录树文件")
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="拖放目录树必须使用本机绝对路径")
    if not path.is_file():
        # is_file 会吞掉 OSError 并返回 False，先区分“路径不存在”
        # 与“路径存在但不是文件”，避免把挂载层问题误报成文件缺失。
        if not path.exists():
            raise HTTPException(status_code=400, detail="目录树文件不存在或路径错误")
        raise HTTPException(status_code=400, detail="拖放路径不是可读取文件")
    try:
        size = path.stat().st_size
    except OSError as exc:
        if _is_mount_fs_error(exc):
            raise HTTPException(status_code=400, detail=_MOUNT_FS_ERROR_HINT) from exc
        raise HTTPException(status_code=400, detail="无法读取拖放目录树文件") from exc
    if size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="目录树文件超过 64 MB")
    try:
        data = path.read_bytes()
    except OSError as exc:
        if _is_mount_fs_error(exc):
            raise HTTPException(status_code=400, detail=_MOUNT_FS_ERROR_HINT) from exc
        raise HTTPException(status_code=400, detail="无法读取拖放目录树文件") from exc
    _validate_tree_bytes(data)
    return data


def _decode_tree_candidates(data: bytes) -> list[str]:
    encodings = ["utf-8-sig", "gb18030"]
    if data[:2] in {b"\xff\xfe", b"\xfe\xff"}:
        encodings.insert(0, "utf-16")
    else:
        encodings.append("utf-16")

    candidates: list[str] = []
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if text not in candidates:
            candidates.append(text)
    return candidates


def _detect_tree_source_bytes(data: bytes) -> str:
    detected: set[str] = set()
    for text in _decode_tree_candidates(data):
        pan115_count = len(_PAN115_TREE_LINE.findall(text))
        baidu_count = len(_BAIDU_TREE_LINE.findall(text))
        if pan115_count and not baidu_count:
            detected.add("pan115")
        elif baidu_count and not pan115_count:
            detected.add("baidu")

    if len(detected) != 1:
        raise HTTPException(
            status_code=400,
            detail="无法识别目录树来源，请确认 TXT 是 115 或百度网盘导出的目录树",
        )
    return detected.pop()


def detect_tree_source(tree_path: Path | str) -> str:
    """根据目录树正文识别 115 或百度格式，不使用易误判的文件名。"""
    path = Path(tree_path).expanduser()
    return _detect_tree_source_bytes(_read_dropped_tree(path))


def archive_local_tree(
    preset_id: str,
    tree_path: str,
) -> tuple[str, MediaTreeVersion, Path, str]:
    """只读复制桌面拖入 / 选择器选中的 TXT，并返回自动识别的来源。

    新根路径合同：用户选择的 TXT 绝对路径是权威事实，source_root 就是它的父目录。
    返回 (source, version, archive, source_root)。

    挂载网盘（WinFSP / 115 官方挂载）可能不支持真实路径解析：
    ``resolve(strict=True)`` 会抛 WinError 1005。与 OpenList 链路一致，
    只验证可访问性，不强制解析符号链接。
    """
    path = Path(tree_path).expanduser()
    resolved = path.absolute()  # 不调用 resolve(strict=True)，挂载盘可能抛 WinError 1005
    data = _read_dropped_tree(resolved)
    source = _detect_tree_source_bytes(data)
    source_root = str(resolved.parent)
    version, archive = _archive_tree_bytes(
        preset_id,
        resolved.name,
        data,
        source_tree_path=str(resolved),
        provider_id=compat_provider(source),
        ingest_method=compat_ingest(source),
    )
    return source, version, archive, source_root


def parse_archived_tree(
    source: str,
    archive: Path,
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    build_plan: bool = True,
    tree_name: str = "",
    source_root_is_exact: bool = False,
):
    from app.api.sources import (
        _import_scope_for_baidu_tree_content,
        _import_scope_for_tree,
        _normalize_import_family,
        _normalize_import_scope,
        _resolve_final_import_scope,
        _resolve_tree_source_root,
    )
    from app.sources.path_validation import (
        resolve_baidu_snapshot_root,
        validate_snapshot_paths,
    )
    from app.sources.registry import get_source_adapter, get_source_root

    if source not in {"pan115", "baidu"}:
        raise HTTPException(status_code=400, detail="媒体库预设当前仅支持 115 或百度目录树")
    family = _normalize_import_family(import_family)
    manual_scope = _normalize_import_scope(import_scope, family)
    tree_hint = tree_name or str(archive)
    try:
        resolved_root = get_source_root(source, source_root)
        # 手动重新绑定时直接使用用户选择的精确根；常规导入则按原始文件名
        # 在配置根的直属子目录中解析作用域，例如 01动画 或新番。
        effective_root = (
            Path(resolved_root)
            if source_root_is_exact
            else _resolve_tree_source_root(tree_hint, resolved_root)
        )
        snapshot = get_source_adapter(source).parse(str(archive), str(effective_root))
        path_validation = (
            resolve_baidu_snapshot_root(snapshot, tree_hint, str(effective_root))
            if source == "baidu"
            else validate_snapshot_paths(snapshot)
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"解析失败: {exc}") from exc
    snapshot.import_family = family
    auto_scope = _import_scope_for_tree(tree_hint, Path(snapshot.source_root))
    if source == "baidu":
        auto_scope = _resolve_final_import_scope(auto_scope, _import_scope_for_baidu_tree_content(snapshot))
    snapshot.import_scope = _resolve_final_import_scope(manual_scope, auto_scope)
    # 预设版本在用户激活前只是候选快照，不应改变来源级增量基线。
    save_raw_snapshot(snapshot, update_latest=False)
    if not build_plan:
        return snapshot, None, path_validation
    plan = build_plan_for_snapshot(snapshot)
    return snapshot, plan, path_validation


def build_plan_for_snapshot(snapshot):
    from app.api.sources import _build_and_recognize_plan

    plan = _build_and_recognize_plan(snapshot)
    save_import_plan(plan, update_latest=False)
    return plan


def rebuild_provider_catalog_sync(
    *,
    root_id: str,
    tree_archive: str,
    local_locator: str,
) -> dict:
    """RWK-40（P0-1）：在既有 SourceRoot 上重建 Provider TXT baseline（root-scoped）。

    与 ``bootstrap_provider_catalog_sync`` 的区别：**已有 durable preset 的所有
    后续 TXT baseline（v2 update / same-TXT recovery / rebind）都必须走这里**——
    以 ``root_id`` 为权威身份，绝不从 local_mount_root 重新推导 source/root 身份
    （那是首次创建才做的）。保持 root_id / source_id / media_unit identity 不变，
    仅更新 local_locator、推进 generation、重跑 snapshot baseline。

    返回与 bootstrap 同构的 {"root_id", "generation", "source_id", "summary",
    "revision_ids"}，供调用方继续统一处理 confirmation state。
    """
    from app.catalog import store as catalog_store
    from app.db.database import get_connection, init_db
    from app.pipeline.discovery_handler import handle_discovery_scan

    init_db()
    if not root_id:
        raise HTTPException(status_code=409, detail="缺少 durable 来源根")
    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise HTTPException(status_code=409, detail="durable 来源根不存在，请重新导入目录树")
    if not tree_archive or not Path(tree_archive).is_file():
        raise HTTPException(status_code=409, detail="当前媒体库的目录树归档不存在，请重新导入")
    source_id = str(root.source_id or "")
    if source_id.startswith("pan115"):
        provider = "pan115"
    elif source_id.startswith("baidu"):
        provider = "baidu"
    else:
        raise HTTPException(status_code=409, detail="该来源根不是可重扫的 Provider 来源")
    with CONFIRMATION_AUTHORITY_LOCK:
        catalog_store.update_root_local_locator(
            root_id, str(Path(local_locator).expanduser())
        )
        generation = catalog_store.bump_generation(root_id)
        catalog_store.set_baseline_target(root_id, generation)
        catalog_store.prepare_scan(root_id, generation=generation, mode="full")
        payload = {
            "root_id": root_id,
            "generation": generation,
            "source_id": source_id,
            "input_path": tree_archive,
            "scan_mode": "full",
            "scan_channel": f"snapshot_{provider}",
        }
        summary = handle_discovery_scan(payload).get("summary", {})
    rows = get_connection().execute(
        """
        SELECT r.revision_id FROM import_revisions r
        JOIN media_units u ON u.unit_id = r.unit_id
        WHERE u.root_id = ? AND r.source_generation = ? AND r.status = 'draft'
        ORDER BY r.created_at ASC
        """,
        (root_id, generation),
    ).fetchall()
    revision_ids = [str(r["revision_id"]) for r in rows]
    return {
        "root_id": root_id,
        "generation": generation,
        "source_id": source_id,
        "summary": summary,
        "revision_ids": revision_ids,
    }


def rebuild_durable_baseline_for_rebind(
    preset: MediaLibraryPreset, new_local_mount_root: str, tree_archive: str
) -> dict:
    """RWK-39/40：rebind 实际视频文件夹时在同一 SourceRoot 上重建 durable baseline。

    复用 ``rebuild_provider_catalog_sync``（root-scoped，绝不再派生 root 身份）；
    在既有 root 上切换 local_locator、推进 generation、用归档 TXT 重跑 baseline。
    返回新 (confirmation_root_id, confirmation_generation) + revision_ids；旧
    generation 的 patch/confirm 由 confirm-root 的 generation fence 自动 409 stale。
    """
    root_id = (preset.catalog_root_id or "").strip()
    info = rebuild_provider_catalog_sync(
        root_id=root_id,
        tree_archive=tree_archive,
        local_locator=new_local_mount_root,
    )
    failed_count = int(info["summary"].get("failed_count", 0) or 0)
    preset.execution_authority = "durable_root"
    preset.confirmation_state = "failed" if failed_count > 0 else "ready"
    preset.updated_at = now_iso()
    save_preset(preset)
    return {
        "root_id": info["root_id"],
        "generation": info["generation"],
        "status": "baseline_failed" if failed_count > 0 else "baseline_queued",
        "revision_ids": info["revision_ids"],
        "confirmation_root_id": info["root_id"],
        "confirmation_generation": info["generation"],
        "summary": info["summary"],
    }


def rebind_preset_source_root(
    preset: MediaLibraryPreset, source_root: str, *, rebuild_durable: bool = True
):
    """用用户选择的精确目录重新解析当前归档，不创建新版本或触碰真实媒体。

    durable TXT preset（execution_authority=durable_root 或已关联 catalog_root_id）
    在 rebind 新实际视频文件夹时，额外在同一 SourceRoot 上重建 durable baseline：
    local_locator 切换、generation 前进、旧 generation patch/confirm 自动 409 stale。
    revalidate（同路径）传 rebuild_durable=False 不重建。
    返回 (snapshot, plan, version, path_validation, baseline)；baseline 为 None
    表示非 durable 预设或未重建（revalidate）。
    """
    if not source_root.strip():
        raise HTTPException(status_code=400, detail="请先选择实际视频文件夹")
    selected_root = Path(source_root).expanduser()
    if not selected_root.is_dir():
        raise HTTPException(status_code=400, detail="选择的实际视频文件夹不存在或不可访问")
    version = next(
        (item for item in preset.versions if item.version_id == preset.current_version_id),
        preset.versions[-1] if preset.versions else None,
    )
    if version is None or not version.archive_path:
        raise HTTPException(status_code=409, detail="当前媒体库缺少已归档目录树，无法重新验证")
    archive = get_data_dir() / version.archive_path
    if not archive.is_file():
        raise HTTPException(status_code=409, detail="当前媒体库的目录树归档不存在，请重新导入")

    snapshot, plan, path_validation = parse_archived_tree(
        preset.source,
        archive,
        str(selected_root),
        preset.import_family,
        preset.import_scope,
        tree_name=version.original_name,
        source_root_is_exact=True,
    )
    preview = build_preview(plan)
    version.snapshot_id = snapshot.snapshot_id
    version.plan_id = plan.plan_id
    version.summary = dict(preview.summary)
    version.path_validation = path_validation.to_dict()
    preset.source_root = snapshot.source_root
    preset.current_snapshot_id = snapshot.snapshot_id
    preset.current_plan_id = plan.plan_id
    preset.lifecycle_status = "draft"
    preset.updated_at = now_iso()
    baseline = None
    if (
        rebuild_durable
        and preset.source in {"pan115", "baidu"}
        and (preset.execution_authority == "durable_root" or preset.catalog_root_id)
    ):
        # RWK-40（P1 residual）：rebind 的 authority transition 必须全程持同一把
        # confirmation authority 锁——mark pending → persist preset → local locator
        # switch → advance target → full rebuild。否则窄窗口里旧 generation 仍是
        # current target，另一个 confirm-root 请求可能抢先确认旧 generation。
        with CONFIRMATION_AUTHORITY_LOCK:
            preset.confirmation_state = "pending"
            save_preset(preset)
            baseline = rebuild_durable_baseline_for_rebind(
                preset, str(selected_root), str(archive)
            )
    else:
        save_preset(preset)
    return snapshot, plan, version, path_validation, baseline


def create_preset_record(
    source: str,
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    update_mode: str = "directory_tree",
    provider_id: str = "",
    ingest_method: str = "",
    source_route_id: str = "",
    catalog_root_id: str = "",
) -> MediaLibraryPreset:
    """创建媒体库预设；新记录显式写入 provider/ingest 元数据。

    未显式传入时按 source 兼容映射（115/百度目录树 -> directory_tree，
    local -> local_scan，openlist -> openlist_api + quark 回填）。
    """
    timestamp = now_iso()
    return MediaLibraryPreset(
        preset_id=uuid.uuid4().hex,
        name=next_preset_name(import_family, import_scope),
        source=source,
        source_root=source_root,
        import_family=import_family,
        import_scope=import_scope,
        update_mode=update_mode,
        provider_id=provider_id or compat_provider(source),
        ingest_method=ingest_method or compat_ingest(source),
        source_route_id=source_route_id or "",
        catalog_root_id=catalog_root_id or "",
        created_at=timestamp,
        updated_at=timestamp,
    )


def _normalized_remote_locator(value: str) -> str:
    """OpenList 远端定位归一化：去尾部斜杠，用于来源卡复用判定。"""
    value = (value or "").strip()
    if not value:
        return ""
    value = value.replace("\\", "/")
    while len(value) > 1 and value.endswith("/"):
        value = value[:-1]
    return value


def find_openlist_preset_by_root(
    catalog_root_id: str,
    remote_locator: str,
    provider_id: str = "",
    source_route_id: str = "",
) -> MediaLibraryPreset | None:
    """按 Source Catalog root 或归一化远端定位复用 OpenList 来源卡。

    优先精确匹配 ``catalog_root_id``（权威关联）；旧记录没有该字段时回退
    到 ``remote_locator`` 归一化匹配（exact reuse / promote 后的 canonical
    locator 可能变化，因此 locator 匹配只作兼容兜底）。
    """
    if catalog_root_id:
        exact = next(
            (item for item in list_presets()
             if item.source == "openlist" and item.catalog_root_id == catalog_root_id),
            None,
        )
        if exact is not None:
            return exact
    expected = _normalized_remote_locator(remote_locator)
    if expected:
        candidates = [
            item for item in list_presets()
            if item.source == "openlist"
            and _normalized_remote_locator(item.remote_locator) == expected
        ]
        if candidates:
            # 多个历史卡指向同一 locator（旧数据无 root 关联）：优先带
            # catalog_root_id 的，其次最早的。
            with_root = [item for item in candidates if item.catalog_root_id]
            pool = with_root or candidates
            return min(pool, key=lambda item: (item.created_at or item.updated_at, item.preset_id))
    return None


def sync_openlist_source_preset(
    *,
    catalog_root_id: str,
    remote_locator: str,
    local_locator: str = "",
    provider_id: str = "",
    source_route_id: str = "",
    import_family: str = "anime",
    import_scope: str = "",
) -> tuple[MediaLibraryPreset, bool]:
    """OpenList import-batch 的来源卡同步：复用已有关卡或创建一张新卡。

    来源卡代表用户选中的 OpenList SourceRoot（长期媒体管理入口），不是
    某一部作品；同一 canonical SourceRoot 再次导入必须复用同一张卡，
    不得制造重复来源卡。

    返回 ``(preset, created)``。
    """
    existing = find_openlist_preset_by_root(
        catalog_root_id, remote_locator, provider_id, source_route_id
    )
    if existing is not None:
        # 复用：刷新权威关联与元数据（不覆盖用户改名/归档）
        changed = False
        if catalog_root_id and existing.catalog_root_id != catalog_root_id:
            existing.catalog_root_id = catalog_root_id
            changed = True
        if provider_id and existing.provider_id != provider_id:
            existing.provider_id = provider_id
            changed = True
        if source_route_id and existing.source_route_id != source_route_id:
            existing.source_route_id = source_route_id
            changed = True
        if existing.import_family != import_family or existing.import_scope != import_scope:
            existing.import_family = import_family
            existing.import_scope = import_scope
            changed = True
        if changed:
            existing.updated_at = now_iso()
            save_preset(existing)
        return existing, False
    preset = create_preset_record(
        "openlist",
        local_locator,
        import_family,
        import_scope,
        update_mode="openlist_scan",
        provider_id=provider_id,
        ingest_method="openlist_api",
        source_route_id=source_route_id,
        catalog_root_id=catalog_root_id,
    )
    preset.remote_locator = _normalized_remote_locator(remote_locator)
    preset.name = _normalized_remote_locator(remote_locator).rsplit("/", 1)[-1] or "OpenList 媒体库"
    save_preset(preset)
    return preset, True


def scan_local_preset(
    source_root: str,
    import_family: str,
    import_scope: str,
    *,
    preset: MediaLibraryPreset | None = None,
):
    """扫描本地目录，并以实际路径为媒体库身份维护增量版本。"""
    from app.api.sources import _normalize_import_family, _normalize_import_scope
    from app.sources.local import LocalScanner
    from app.sources.path_validation import validate_snapshot_paths

    family = _normalize_import_family(import_family)
    scope = _normalize_import_scope(import_scope, family)
    root = Path(source_root).expanduser()
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail="本地目录不存在或不可访问") from exc
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="本地目录不存在或不可访问")

    existing = preset or next(
        (
            item for item in list_presets()
            if item.source == "local"
            and item.update_mode == "local_scan"
            and _normalized_library_root(item.source_root) == _normalized_library_root(str(root))
        ),
        None,
    )
    if existing is not None:
        if existing.source != "local" or existing.update_mode != "local_scan":
            raise HTTPException(status_code=409, detail="该媒体库不是本地扫描卡片")
        family = existing.import_family
        scope = existing.import_scope

    try:
        snapshot = LocalScanner().scan(
            str(root),
            source_root=str(root),
            logical_source="local",
            include_root=False,
            metadata_only=False,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"本地目录扫描失败: {exc}") from exc
    if snapshot.video_count <= 0:
        raise HTTPException(status_code=400, detail="所选本地目录中没有识别到视频文件")

    snapshot.import_family = family
    snapshot.import_scope = scope
    path_validation = validate_snapshot_paths(snapshot)
    if not path_validation.ok:
        raise HTTPException(status_code=400, detail=path_validation.message or "本地目录路径验证失败")
    save_raw_snapshot(snapshot, update_latest=False)

    version = MediaTreeVersion(
        version_id=uuid.uuid4().hex,
        original_name=f"{root.name or str(root)}（本地扫描）",
        created_at=now_iso(),
        path_validation=path_validation.to_dict(),
        provider_id=compat_provider("local"),
        ingest_method=compat_ingest("local"),
    )
    if existing is not None:
        current_snapshot = load_raw_snapshot(existing.current_snapshot_id)
        if snapshots_have_same_media(current_snapshot, snapshot):
            discard_candidate_snapshot(snapshot.snapshot_id)
            plan = load_import_plan(plan_id=existing.current_plan_id)
            if plan is None:
                raise HTTPException(status_code=409, detail="本地媒体库缺少当前导入计划，请重新导入")
            selected_version = next(
                (item for item in existing.versions if item.version_id == existing.current_version_id),
                existing.versions[-1],
            )
            return existing, selected_version, plan, None, True, True

        version.preset_id = existing.preset_id
        plan, diff = update_preset_from_tree(existing, version, snapshot)
        return existing, version, plan, diff, True, False

    plan = build_plan_for_snapshot(snapshot)
    plan.import_family = family
    plan.import_scope = scope
    save_import_plan(plan, update_latest=False)
    created = create_preset_record(
        "local",
        str(root),
        family,
        scope,
        update_mode="local_scan",
    )
    created.name = root.name or str(root)
    version.preset_id = created.preset_id
    activate_version(created, version, snapshot, plan)
    return created, version, plan, None, False, False


def create_preset_from_folder(
    source: str,
    source_root: str,
    import_family: str,
    import_scope: str,
):
    """从用户明确选择的挂载文件夹建立首个媒体库版本。

    复用追更扫描使用的 ``LocalScanner``，并强制 metadata-only：只枚举
    名称、路径、大小和修改时间，不打开视频、计算内容指纹或生成缩略图。
    """
    from app.api.sources import _normalize_import_family, _normalize_import_scope
    from app.sources.local import LocalScanner
    from app.sources.path_validation import validate_snapshot_paths

    if source != "baidu":
        raise HTTPException(status_code=400, detail="真实文件夹首导当前仅支持百度网盘新番")
    family = _normalize_import_family(import_family)
    scope = _normalize_import_scope(import_scope, family)
    if family != "anime" or scope != "seasonal":
        raise HTTPException(status_code=400, detail="真实文件夹首导仅用于动画新番（追更中）")

    root = Path(source_root).expanduser()
    if not root.is_dir():
        raise HTTPException(status_code=400, detail="选择的新番真实文件夹不存在或不可访问")

    try:
        snapshot = LocalScanner().scan(
            str(root),
            source_root=str(root),
            logical_source=source,
            include_root=True,
            metadata_only=True,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=f"文件夹扫描失败: {exc}") from exc
    if snapshot.video_count <= 0:
        raise HTTPException(status_code=400, detail="所选文件夹中没有识别到视频文件")

    snapshot.import_family = family
    snapshot.import_scope = scope
    path_validation = validate_snapshot_paths(snapshot)
    if not path_validation.ok:
        raise HTTPException(status_code=400, detail=path_validation.message or "文件夹路径验证失败")

    save_raw_snapshot(snapshot, update_latest=False)
    version = MediaTreeVersion(
        version_id=uuid.uuid4().hex,
        original_name=f"{root.name}（文件夹扫描）",
        created_at=now_iso(),
        path_validation=path_validation.to_dict(),
        provider_id=compat_provider(source),
        ingest_method=compat_ingest(source),
    )
    existing = find_matching_preset(source, str(root), family, scope)
    if existing is not None:
        version.preset_id = existing.preset_id
        plan, _ = update_preset_from_tree(existing, version, snapshot)
        return existing, version, plan, True

    plan = build_plan_for_snapshot(snapshot)
    plan.import_family = family
    plan.import_scope = scope
    save_import_plan(plan, update_latest=False)
    preset = create_preset_record(source, str(root), family, scope)
    version.preset_id = preset.preset_id
    activate_version(preset, version, snapshot, plan)
    return preset, version, plan, False


def activate_version(preset: MediaLibraryPreset, version: MediaTreeVersion, snapshot, plan) -> None:
    preview = build_preview(plan)
    version.snapshot_id = snapshot.snapshot_id
    version.plan_id = plan.plan_id
    version.summary = dict(preview.summary)
    preset.current_snapshot_id = snapshot.snapshot_id
    preset.current_plan_id = plan.plan_id
    preset.current_version_id = version.version_id
    preset.version_count += 1
    preset.work_count = len(preview.groups)
    preset.video_count = int(preview.summary.get("video_count", snapshot.video_count))
    # RWK-40（P0-2）：TXT preset 在 current_plan 对外可见的同一次 save 中
    # 先声明 durable_root/pending；baseline 完成后再由 _ensure_tree_baseline
    # 转为 ready/failed，消除 legacy-visible → durable 的并发窗口。
    if preset.source in {"pan115", "baidu"}:
        preset.execution_authority = "durable_root"
        preset.confirmation_state = "pending"
    # 新目录树仍需重新确认、生成镜像和刮削，不能沿用旧版本的完成状态。
    preset.lifecycle_status = "draft"
    preset.updated_at = now_iso()
    preset.versions.append(version)
    save_preset(preset)


def update_preset_from_tree(
    preset: MediaLibraryPreset,
    version: MediaTreeVersion,
    snapshot,
    *,
    persist_on_block: bool = True,
):
    old_snapshot = load_raw_snapshot(preset.current_snapshot_id)
    base_plan = load_import_plan(plan_id=preset.current_plan_id)
    if old_snapshot is None or base_plan is None:
        raise HTTPException(status_code=409, detail="该媒体库缺少旧版基线，请重新创建预设")
    diff = compute_diff(old_snapshot, snapshot)
    save_diff_result(diff)
    version.snapshot_id = snapshot.snapshot_id
    version.diff_id = diff.diff_id
    # 纯新增对小型新番库会天然产生很高的“总量变化比例”，但没有删除风险。
    # 此时允许继续；只在存在缺失或不确定变更时执行安全阻断。
    unsafe_change = diff.safety.blocked and (diff.missing_count > 0 or diff.uncertain_count > 0)
    if unsafe_change:
        version.summary = {"blocked": True, "reasons": diff.safety.reasons}
        if persist_on_block:
            # 兼容既有调用方（create / scan / 旧 upload 更新）的历史行为。
            preset.versions.append(version)
            preset.version_count += 1
            preset.updated_at = now_iso()
            save_preset(preset)
        raise HTTPException(status_code=409, detail=f"更新被安全检查阻止：{'；'.join(diff.safety.reasons)}")

    delta = build_incremental_plan(
        diff,
        preset.source,
        snapshot.source_root,
        new_snapshot=snapshot,
        allow_blocked=diff.safety.blocked and not unsafe_change,
    )
    if delta is None:
        cumulative = base_plan
        cumulative.source_snapshot_id = snapshot.snapshot_id
    else:
        cumulative = merge_incremental_plan(base_plan, delta, diff, status="draft")
    cumulative.import_family = preset.import_family
    cumulative.import_scope = preset.import_scope
    if preset.source in {"pan115", "baidu"}:
        # 更新路径先持久化 authority，避免旧 current_plan 在新 plan 写入期间
        # 仍可被外部 legacy confirm/mirror 使用。
        preset.execution_authority = "durable_root"
        preset.confirmation_state = "pending"
        preset.updated_at = now_iso()
        save_preset(preset)
    save_import_plan(cumulative, update_latest=False)
    activate_version(preset, version, snapshot, cumulative)
    version.diff_id = diff.diff_id
    save_preset(preset)
    return cumulative, diff


def resolve_needs_review_unit(root_id: str, unit_id: str, work_title: str, generation: int = 0) -> dict:
    """RWK-40（P0-2）：为 needs_review 且无 revision 的 MediaUnit 生成可编辑 draft revision。

    事故链：TXT baseline 完整成功 → 作品识别失败 → needs_review unit 无 revision
    → confirmation_ready=false → durable「继续确认」按钮不存在 → 重导相同 TXT 又
    baseline_reused → 永久卡住。本函数让这类 unit 通过 DiscoveryEngine 从既有
    source_nodes 重建 snapshot/items（scanner 为 None 仅读 source_nodes，0 网络），
    生成 draft revision；用户提供 work_title 时复用 patch 规则强制写回作品身份。
    生成后该 unit 进入 root-generation 确认集合（confirmation_ready=true），后续
    确认走 confirm-root，**绝不依赖 legacy plan**。
    """
    from app.catalog import store as catalog_store
    from app.catalog.discovery import DiscoveryEngine
    from app.db.database import get_connection, init_db
    from app.import_plan import revision_store

    init_db()
    if not unit_id:
        raise HTTPException(status_code=400, detail="缺少识别单元")
    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise HTTPException(status_code=404, detail="来源根不存在")
    source_id = str(root.source_id or "")
    if not (source_id.startswith("pan115") or source_id.startswith("baidu")):
        # RWK-40（M1）：needs-review resolve 是 Provider TXT durable 专属处理入口，
        # 非 pan115/baidu 来源（纯 OpenList）不得走此路径（与 confirm-root 同约束）。
        raise HTTPException(status_code=409, detail="该来源根不支持人工识别处理")
    conn = get_connection()
    unit = conn.execute(
        "SELECT * FROM media_units WHERE unit_id = ? AND root_id = ?",
        (unit_id, root_id),
    ).fetchone()
    if unit is None:
        raise HTTPException(status_code=404, detail="识别单元不存在")
    if str(unit["status"]) != "needs_review":
        raise HTTPException(status_code=409, detail="该识别单元不在待处理状态")

    target = int(getattr(root, "baseline_target_generation", 0) or 0)
    completed = int(getattr(root, "baseline_completed_generation", 0) or 0)
    # RWK-40（P0-3）：与 confirm-root/patch 同源的 generation fence——resolve 必须
    # 精确作用在用户看到的 (root, generation) 上。target 已前进（页面陈旧）或基线
    # 未完成（completed < target，partial/failed 场景）一律 409，0 revision 生成、
    # 0 unit 变更。baseline 成功但全 needs_review（completed == target == generation，
    # 无 draft revision）是唯一允许 resolve 的状态。
    if generation <= 0 or target <= 0:
        # 与 confirm-root（imports.py）对齐的兜底：target=0（从未跑 baseline）或
        # generation=0 时三零边界（0==0）不得放行，防止在无基线 root 上生成孤儿 revision。
        raise HTTPException(status_code=409, detail="该来源根没有进行中的目录树基线")
    if generation != target:
        raise HTTPException(status_code=409, detail="识别单元已过期，目录树基线已更新，请刷新后重试")
    if completed != generation:
        raise HTTPException(status_code=409, detail="目录树基线尚未完成，暂不能人工处理识别单元")
    # RWK-40（P0）：旧的 current_revision_id 是上一代已确认指针（可能 confirmed/
    # executed/published），不得阻止当前 target generation 人工生成新 draft。
    # 只拒绝「当前 target generation 已存在 actionable revision」（draft/
    # confirmed/executed），老的 rev_v1 继续作为已发布事实存在即可。
    existing = conn.execute(
        "SELECT revision_id, status FROM import_revisions "
        "WHERE unit_id = ? AND source_generation = ?",
        (unit_id, target),
    ).fetchall()
    for row in existing:
        if str(row["status"]) in ("draft", "confirmed", "executed"):
            raise HTTPException(
                status_code=409, detail="该识别单元在当前目录树版本已有可处理版本"
            )

    engine = DiscoveryEngine(
        None,
        source_id=str(root.source_id or ""),
        root_id=root_id,
        generation=target,
    )
    boundary = str(unit["boundary"] or "")
    result = engine._process_unit(
        {
            "boundary": boundary,
            "work_key": str(unit["work_key"] or boundary),
            "work_title": "",
        },
        should_cancel=None,
    )
    new_revision_id = str(result.get("revision_id") or "")
    if not new_revision_id:
        raise HTTPException(
            status_code=409,
            detail="该识别单元下没有可入库视频，无法生成可处理版本",
        )
    if work_title and work_title.strip():
        cleaned_title = work_title.strip()
        loaded = revision_store.load_plan(new_revision_id)
        items = loaded.items if loaded else []
        for item in items:
            if getattr(item, "resource_type", "") == "video" and getattr(item, "action", "") == "generate_strm":
                try:
                    revision_store.patch_draft_revision_item(
                        new_revision_id,
                        item.id,
                        {"work_title": cleaned_title, "needs_review": False, "warnings": []},
                    )
                except Exception:
                    # 单 item patch 失败不阻塞整体（人工确认页仍可逐项修正）
                    pass
    # RWK-40（M2）：回写 current_revision_id → openlist_preset_state 把该 unit
    # 视为「有待处理 draft」（attention 可见），而非 needs_review 重复提示；
    # confirmation_ready 由该 draft revision 提供，与「继续确认」入口衔接。
    get_connection().execute(
        "UPDATE media_units SET status='plan_ready', current_revision_id=? "
        "WHERE unit_id=? AND root_id=?",
        (new_revision_id, unit_id, root_id),
    )
    get_connection().commit()
    return {
        "revision_id": new_revision_id,
        "unit_id": unit_id,
        "root_id": root_id,
        "generation": target,
    }

