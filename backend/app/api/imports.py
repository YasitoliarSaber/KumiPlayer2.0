# -*- coding: utf-8 -*-
"""导入计划 API 端点

GET  /api/imports/{source}/preview
PATCH /api/imports/{source}/items/{item_id}
POST /api/imports/{source}/confirm
POST /api/imports/{source}/diff
GET  /api/imports/{source}/diff/{diff_id}
POST /api/imports/{source}/incremental/preview
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.import_plan.models import ImportPlan
from app.import_plan.preview import ImportPreview
from app.import_plan.service import build_preview, patch_plan_item, confirm_plan
from app.import_plan.store import load_import_plan, save_import_plan

router = APIRouter(prefix="/api/imports", tags=["imports"])


# ============================================================
# 请求/响应模型
# ============================================================

class PatchRequest(BaseModel):
    """修正条目请求"""
    plan_id: str
    patch: dict


class ConfirmRequest(BaseModel):
    """确认计划请求。低置信 warning 默认延后处理，不阻塞确认。"""
    plan_id: str
    force: bool = False


# ============================================================
# 辅助函数
# ============================================================

def _load_plan_or_404(plan_id: Optional[str] = None, source: Optional[str] = None) -> ImportPlan:
    """加载 ImportPlan，找不到则抛 404"""
    plan = load_import_plan(plan_id=plan_id, source=source)
    if plan is None:
        raise HTTPException(status_code=404, detail="ImportPlan 不存在")
    return plan


# ============================================================
# 端点
# ============================================================

@router.get("/{source}/preview")
def get_preview(source: str, plan_id: Optional[str] = None):
    """获取导入预览

    有 plan_id 时读取指定 plan，无 plan_id 时读取 {source}_latest.json。
    """
    plan = _load_plan_or_404(plan_id=plan_id, source=source)
    preview = build_preview(plan)
    return _preview_to_dict(preview)


@router.patch("/{source}/items/{item_id}")
def patch_item(source: str, item_id: str, req: PatchRequest):
    """修正单个 ImportPlanItem

    只允许 patch 白名单字段，patch 后返回更新后的 item 和 preview summary。
    V3（SQLite revision）走 patch_draft_revision_item，事务内更新 SQLite、
    不回写 legacy JSON（data/import_plans/<revision_id>.json 不出现）；
    legacy JSON plan 保持原路径。
    """
    from app.import_plan import revision_store
    from app.db.database import get_connection
    revision = revision_store.load_revision(req.plan_id)
    if revision is not None:
        revision_source = str(revision.get("source") or "")
        if revision_source and revision_source != source:
            raise HTTPException(
                status_code=400,
                detail=f"revision.source={revision_source} 与 URL source={source} 不匹配",
            )
        plan = revision_store.load_plan(req.plan_id)
        owns_item = plan is not None and any(it.id == item_id for it in plan.items)
        if not owns_item:
            # RWK-38（E）：aggregate durable preview 的 plan_id 是第一个 revision，
            # 但多作品时 item 属于其所在 revision——跨该 root 全部 draft revisions
            # 按 item_id 定位并 patch（同 root-generation 确认身份）。
            unit_id = str(revision.get("unit_id") or "")
            root_id = ""
            if unit_id:
                row = get_connection().execute(
                    "SELECT root_id FROM media_units WHERE unit_id = ?",
                    (unit_id,),
                ).fetchone()
                if row is not None:
                    root_id = str(row["root_id"])
            if root_id:
                try:
                    return _patch_txt_baseline_item(root_id, item_id, req.patch, source)
                except _TxtBaselinePatchMiss:
                    pass  # 该 item 不属于任何 draft revision → 退回完整错误
        try:
            revision_store.patch_draft_revision_item(req.plan_id, item_id, req.patch)
        except revision_store.RevisionStatusError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        plan = revision_store.load_plan(req.plan_id)
        if plan is None:
            raise HTTPException(status_code=400, detail="无法装载 revision")
        item = next((it for it in plan.items if it.id == item_id), None)
        if item is None:
            raise HTTPException(status_code=400, detail=f"未找到 item_id={item_id}")
        preview = build_preview(plan)
        return {
            "item": _item_to_dict(item),
            "summary": preview.summary,
        }

    # RWK-37（C）：TXT baseline 场景——plan_id 是 legacy 展示计划（preview 用），
    # 但人工修正必须命中对应 durable revision。按 item 的 relative_path 在该
    # root 的 draft revisions 中定位并 patch（保证确认时用到修正后的事实）。
    preset = _preset_for_legacy_plan(req.plan_id, source)
    if preset is not None and preset.catalog_root_id:
        try:
            return _patch_txt_baseline_item(
                preset.catalog_root_id, item_id, req.patch, source,
            )
        except _TxtBaselinePatchMiss:
            # 该 item 不属于任何 draft revision（可能在确认中已确认）→ 回退 legacy
            pass

    plan = _load_plan_or_404(plan_id=req.plan_id)
    if plan.source != source:
        raise HTTPException(status_code=400, detail=f"plan.source={plan.source} 与 URL source={source} 不匹配")
    item, preview, error = patch_plan_item(plan, item_id, req.patch)
    if error:
        raise HTTPException(status_code=400, detail=error)

    # 保存更新后的 plan
    save_import_plan(plan)

    return {
        "item": _item_to_dict(item),
        "summary": preview.summary,
    }


@router.post("/{source}/confirm")
def confirm(source: str, req: ConfirmRequest):
    """确认导入计划

    - plan_id 是 SQLite revision（V3/OpenList 链路）→ 人工确认事务 + 入队
      durable mirror job，返回 execution_mode='durable' + job_id；
    - plan_id 是 legacy JSON plan → 保持 confirm_plan + mark_preset_lifecycle
      旧行为，不生成镜像、不返回 task_id。
    """
    from app.import_plan import revision_store
    from app.pipeline import orchestrator

    revision = revision_store.load_revision(req.plan_id)
    if revision is not None:
        # ---- V3 SQLite revision 路径 ----
        revision_source = str(revision.get("source") or "")
        if revision_source and revision_source != source:
            raise HTTPException(
                status_code=400,
                detail=f"revision.source={revision_source} 与 URL source={source} 不匹配",
            )
        if revision["status"] in ("confirmed", "executed"):
            # 重复 confirm：ensure 语义——缺失 mirror job 时自动补出
            # （crash self-healing），已有则复用同一 job 身份
            job_id = orchestrator.enqueue_mirror(req.plan_id, revision["unit_id"])
            return {
                "plan_id": req.plan_id,
                "source": source,
                "status": "confirmed",
                "execution_mode": "durable",
                "job_id": job_id,
                "message": "import revision already confirmed; mirror job ensured",
            }
        try:
            revision_store.confirm_revision_manually(req.plan_id, force=req.force)
        except revision_store.RevisionStatusError as exc:
            # 并发中间时序：本请求外层读到 draft 后另一请求已完整确认
            # （confirmed + mirror job）→ 重读当前状态：已 confirmed/executed
            # 按“并发完成”处理（enqueue_mirror ensure 幂等，返回同一 job_id）；
            # 仍 draft 才是真正的 optimistic conflict / validation failure。
            current = revision_store.load_revision(req.plan_id)
            if current is not None and current["status"] in ("confirmed", "executed"):
                job_id = orchestrator.enqueue_mirror(req.plan_id, current["unit_id"])
                return {
                    "plan_id": req.plan_id,
                    "source": source,
                    "status": "confirmed",
                    "execution_mode": "durable",
                    "job_id": job_id,
                    "message": "import revision already confirmed by concurrent request; mirror job ensured",
                }
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # ensure 语义：无论 transitioned True/False 都调用 enqueue_mirror
        # （get-or-create 幂等），始终返回稳定非空 job_id；并发 confirm 同 job_id
        job_id = orchestrator.enqueue_mirror(req.plan_id, revision["unit_id"])
        return {
            "plan_id": req.plan_id,
            "source": source,
            "status": "confirmed",
            "execution_mode": "durable",
            "job_id": job_id,
            "message": "import revision confirmed; mirror job enqueued",
        }

    # ---- legacy JSON 路径（旧行为不变）----
    plan = _load_plan_or_404(plan_id=req.plan_id)
    if plan.source != source:
        raise HTTPException(status_code=400, detail=f"plan.source={plan.source} 与 URL source={source} 不匹配")

    # 幂等：前端确认门面对 draft/confirmed 一视同仁，legacy confirmed 计划
    # 重复 confirm 直接 200 返回（validate_confirmation 只接受 draft）。
    # 不返回 execution_mode / job_id / task_id，前端自然落 legacy mirror 分支。
    if plan.status == "confirmed":
        return {
            "plan_id": plan.plan_id,
            "source": plan.source,
            "status": "confirmed",
            "message": "import_plan already confirmed",
        }

    confirmed_plan, error = confirm_plan(plan, force=req.force)
    if error:
        raise HTTPException(status_code=400, detail=error)

    from app.media_presets.service import mark_preset_lifecycle
    mark_preset_lifecycle(confirmed_plan.plan_id, "confirmed")

    return {
        "plan_id": confirmed_plan.plan_id,
        "source": confirmed_plan.source,
        "status": confirmed_plan.status,
        "message": "import_plan confirmed; review warnings deferred",
    }


class ConfirmRootRequest(BaseModel):
    """RWK-38：root 级批量确认请求（多作品 TXT baseline 一次确认全部 revisions）。

    确认身份必须是 (root_id, generation)——generation 由前端在导入响应中保存，
    点击确认时回传；后端在任何 mutation 前校验与当前 baseline 状态一致，
    杜绝 TOCTOU（用户确认的内容 ≠ 实际执行的内容）。
    """
    root_id: str
    generation: int
    force: bool = False


@router.post("/{source}/confirm-root")
def confirm_root(source: str, req: ConfirmRootRequest):
    """RWK-38：一次性确认 Provider root 指定 generation 的全部 draft revisions。

    确认身份 = (root_id, generation)，由导入响应下发、前端保存、点击确认回传。
    任何 mutation 前做三重校验：
      1. URL source 与 Provider root 的 source 一致；
      2. req.generation == root.baseline_target_generation（用户看到的确认页
         必须是当前基线；期间有新版导入 → target 前进 → 409 stale）；
      3. req.generation == root.baseline_completed_generation（基线必须真实
         完成——target 已设但未完成的中间态不允许确认）。
    全部通过才逐个 durable confirm + 幂等 enqueue mirror。

    返回 {root_id, generation, confirmed_count, job_ids, revision_ids}。
    """
    from app.catalog import store as catalog_store
    from app.db.database import get_connection
    from app.import_plan import revision_store
    from app.pipeline import orchestrator

    root_id = (req.root_id or "").strip()
    if not root_id:
        raise HTTPException(status_code=400, detail="缺少 root_id")
    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise HTTPException(status_code=404, detail="来源根不存在")

    # 1) URL source 与 Provider root source 一致（source_id = {provider}-{hash}）
    if not _source_matches((root.source_id or ""), source):
        raise HTTPException(
            status_code=400,
            detail=f"root.source_id={root.source_id} 与 URL source={source} 不一致",
        )

    # 2) generation fence：用户确认的必须是当前 target（TOCTOU 防护）
    target = int(getattr(root, "baseline_target_generation", 0) or 0)
    if req.generation != target:
        raise HTTPException(
            status_code=409,
            detail=(
                f"确认身份过期：确认页 generation={req.generation}，"
                f"当前基线 generation={target}；期间有新版本导入，请刷新确认页"
            ),
        )
    if target <= 0:
        raise HTTPException(status_code=400, detail="该来源根没有进行中的目录树基线")

    # 3) 基线必须真实完成（completed == target）
    completed = int(getattr(root, "baseline_completed_generation", 0) or 0)
    if completed != target:
        raise HTTPException(
            status_code=409,
            detail=(
                f"基线尚未完成（target={target} completed={completed}），"
                f"不允许确认；请稍候或刷新确认页"
            ),
        )

    rows = get_connection().execute(
        """
        SELECT r.revision_id, r.unit_id FROM import_revisions r
        JOIN media_units u ON u.unit_id = r.unit_id
        WHERE u.root_id = ? AND r.source_generation = ? AND r.status = 'draft'
        ORDER BY r.created_at ASC
        """,
        (root_id, req.generation),
    ).fetchall()
    if not rows:
        raise HTTPException(status_code=400, detail="没有可确认的草稿修订")

    confirmed_ids = []
    job_ids = []
    for row in rows:
        revision_id = str(row["revision_id"])
        unit_id = str(row["unit_id"])
        try:
            revision_store.confirm_revision_manually(revision_id, force=req.force)
        except revision_store.RevisionStatusError:
            current = revision_store.load_revision(revision_id)
            if current is None or current["status"] not in ("confirmed", "executed"):
                raise HTTPException(
                    status_code=409, detail=f"修订 {revision_id} 确认冲突，请刷新后重试"
                ) from None
        job_id = orchestrator.enqueue_mirror(revision_id, unit_id)
        confirmed_ids.append(revision_id)
        job_ids.append(job_id)

    return {
        "root_id": root_id,
        "generation": target,
        "source": source,
        "execution_mode": "durable",
        "confirmed_count": len(confirmed_ids),
        "revision_ids": confirmed_ids,
        "job_ids": job_ids,
    }
@router.get("/{source}/confirm-root-preview")
def confirm_root_preview(source: str, root_id: str, generation: int):
    """RWK-38（P1）：root-generation 聚合 durable preview。

    确认页展示的唯一真相来源——从该 (root, generation) 的全部 draft
    revisions 聚合 items/issues/groups/summary（合并为一个 plan 后重新
    build_preview，跨作品重复集数等检查自动正确）。patch 后前端刷新此
    端点，而不是重新读旧 legacy JSON preview（避免两份确认事实漂移）。

    前置校验与 confirm_root 相同（source 匹配 + generation == target），
    只读不 mutation。
    """
    from app.catalog import store as catalog_store
    from app.db.database import get_connection
    from app.import_plan import revision_store
    from app.import_plan.service import build_preview
    from app.import_plan.models import ImportPlan

    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise HTTPException(status_code=404, detail="来源根不存在")
    if not _source_matches((root.source_id or ""), source):
        raise HTTPException(
            status_code=400,
            detail=f"root.source_id={root.source_id} 与 URL source={source} 不一致",
        )
    target = int(getattr(root, "baseline_target_generation", 0) or 0)
    if generation != target:
        raise HTTPException(
            status_code=409,
            detail=f"确认身份过期：generation={generation}，当前 target={target}",
        )

    rows = get_connection().execute(
        """
        SELECT r.revision_id FROM import_revisions r
        JOIN media_units u ON u.unit_id = r.unit_id
        WHERE u.root_id = ? AND r.source_generation = ? AND r.status = 'draft'
        ORDER BY r.created_at ASC
        """,
        (root_id, generation),
    ).fetchall()
    if not rows:
        raise HTTPException(status_code=400, detail="没有可确认的草稿修订")

    merged = ImportPlan()
    merged.items = []
    revision_ids: list[str] = []
    for row in rows:
        revision_id = str(row["revision_id"])
        plan = revision_store.load_plan(revision_id)
        if plan is None or not plan.items:
            continue
        revision_ids.append(revision_id)
        merged.items.extend(plan.items)
    if not merged.items:
        raise HTTPException(status_code=400, detail="没有可预览的草稿修订条目")

    preview = build_preview(merged)
    return {
        "plan_id": revision_ids[0],
        "source": source,
        "status": "draft",
        "import_scope": "",
        "summary": preview.summary,
        "parse_logs": [],
        "issues": [
            {
                "code": issue.code,
                "level": issue.level,
                "message": issue.message,
                "item_ids": issue.item_ids,
            }
            for issue in preview.issues
        ],
        "groups": [
            {
                "work_id": g.work_id,
                "work_title": g.work_title,
                "year": g.year,
                "card_type": g.card_type,
                "media_type": g.media_type,
                "show_type": g.show_type,
                "series_group": g.series_group,
                "group_type": g.group_type,
                "season_number": g.season_number,
                "item_count": g.item_count,
                "item_ids": g.item_ids,
                "warnings": g.warnings,
            }
            for g in preview.groups
        ],
        "items": [_item_to_dict(item) for item in merged.items],
        "revision_ids": revision_ids,
    }


def _source_matches(source_id: str, source: str) -> bool:
    """source_id 形如 ``{provider}-{hash}``（Provider 身份），URL source 是 provider。"""
    return source_id == source or source_id.startswith(source + "-")


class _TxtBaselinePatchMiss(Exception):
    """RWK-37（C）：item 不属于该 root 的任何 draft revision。"""
def _preset_for_legacy_plan(plan_id: str, source: str):
    """按 legacy plan_id 找关联 preset（含 catalog_root_id 的 TXT baseline 卡）。"""
    from app.media_presets.store import list_presets

    for preset in list_presets():
        if preset.source != source:
            continue
        if not preset.catalog_root_id:
            continue
        if preset.current_plan_id == plan_id:
            return preset
    return None


def _preset_for_legacy_plan_by_root(root_id: str, source: str):
    """按 root_id 找关联 preset（TXT baseline 卡）。"""
    from app.media_presets.store import list_presets

    for preset in list_presets():
        if preset.source == source and preset.catalog_root_id == root_id:
            return preset
    return None


def _patch_txt_baseline_item(root_id: str, item_id: str, patch: dict, source: str) -> dict:
    """RWK-37（C）：在 root 的 draft revisions 中按 item.relative_path 定位并 patch。

    多作品 TXT 的人工修正是逐作品（preview item）——item 属于哪个 revision 由
    relative_path 匹配（legacy 与 revision items 共用 relative_path 语义）。
    """
    from app.catalog import store as catalog_store
    from app.db.database import get_connection
    from app.import_plan import revision_store

    root = catalog_store.get_source_root(root_id)
    if root is None:
        raise _TxtBaselinePatchMiss()
    target = int(getattr(root, "baseline_target_generation", 0) or 0)
    if target <= 0:
        raise _TxtBaselinePatchMiss()

    # 加载该 root 全部 draft revision 的 plans，按 relative_path 索引
    rows = get_connection().execute(
        """
        SELECT r.revision_id FROM import_revisions r
        JOIN media_units u ON u.unit_id = r.unit_id
        WHERE u.root_id = ? AND r.source_generation = ? AND r.status = 'draft'
        """,
        (root_id, target),
    ).fetchall()
# 先按 legacy plan 找到该 item 的 relative_path（聚合 durable preview 的
    # item 是 revision id；legacy confirm 页的 item 是 legacy id——两者都支持）
    from app.import_plan.store import load_import_plan

    preset = _preset_for_legacy_plan_by_root(root_id, source)
    legacy_plan = None
    if preset is not None and preset.current_plan_id:
        legacy_plan = load_import_plan(plan_id=preset.current_plan_id)
    if legacy_plan is None:
        legacy_plan = load_import_plan(plan_id=None, source=source)
    legacy_item = None
    rel_path = ""
    if legacy_plan is not None:
        legacy_item = next((it for it in legacy_plan.items if it.id == item_id), None)
        if legacy_item is not None:
            rel_path = str(getattr(legacy_item, "relative_path", "") or "")

# 先按 item_id 直接命中（聚合 durable preview 的 item 就是 revision id）；
    # 未命中再按 relative_path 桥接（legacy confirm 页 item 是 legacy id）。
    for row in rows:
        revision_id = str(row["revision_id"])
        plan = revision_store.load_plan(revision_id)
        if plan is None:
            continue
        item = next((it for it in plan.items if it.id == item_id), None)
        if item is None and rel_path:
            item = next(
                (it for it in plan.items if str(getattr(it, "relative_path", "") or "") == rel_path),
                None,
            )
        if item is None:
            continue
        # 命中对应 revision → durable patch（用 revision 自己的 item_id）
        rev_item_id = item.id
        try:
            revision_store.patch_draft_revision_item(revision_id, rev_item_id, patch)
        except revision_store.RevisionStatusError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        plan_after = revision_store.load_plan(revision_id)
        item_after = next((it for it in plan_after.items if it.id == rev_item_id), None)
        if item_after is None:
            raise HTTPException(status_code=400, detail=f"未找到 item_id={rev_item_id}")
        from app.import_plan.service import build_preview

        preview = build_preview(plan_after)
        return {
            "item": _item_to_dict(item_after),
            "summary": preview.summary,
            "revision_id": revision_id,
        }
    raise _TxtBaselinePatchMiss()


# ============================================================
# 序列化辅助
# ============================================================

def _derive_parse_logs(preview: ImportPreview) -> list:
    """派生确认页解析摘要。

    解析阶段没有逐条真实执行时间，因此每项只返回 kind/message，不伪造 time。
    它不是运行时任务日志；确认页只展示它，不得混入 task.result.logs。
    """
    summary = preview.summary or {}
    work_count = int(summary.get("work_count", 0))
    video_count = int(summary.get("video_count", 0))
    total_items = int(summary.get("total_items", 0))

    logs = [
        {"kind": "done", "message": f"已整理 {total_items} 个来源条目，其中视频 {video_count} 个"},
        {"kind": "done", "message": f"作品识别完成：{work_count} 部作品、{len(preview.groups)} 个媒体分组"},
    ]
    issues = [issue for issue in preview.issues if issue.level in {"warning", "error"}]
    if issues:
        logs.append({"kind": "warn", "message": f"发现 {len(issues)} 个需要处理的问题"})
    else:
        logs.append({"kind": "done", "message": "未发现会阻止继续的识别问题"})
    logs.append({"kind": "info", "message": "当前阶段只确认识别结果；确认后才创建镜像并补充资料。"})
    return logs


def _preview_to_dict(preview: ImportPreview) -> dict:
    """将 ImportPreview 转为 dict"""
    return {
        "plan_id": preview.plan_id,
        "source": preview.source,
        "status": preview.status,
        "import_scope": preview.import_scope,
        "summary": preview.summary,
        "parse_logs": _derive_parse_logs(preview),
        "issues": [
            {
                "code": issue.code,
                "level": issue.level,
                "message": issue.message,
                "item_ids": issue.item_ids,
            }
            for issue in preview.issues
        ],
        "groups": [
            {
                "work_id": g.work_id,
                "work_title": g.work_title,
                "year": g.year,
                "card_type": g.card_type,
                "media_type": g.media_type,
                "show_type": g.show_type,
                "series_group": g.series_group,
                "group_type": g.group_type,
                "season_number": g.season_number,
                "item_count": g.item_count,
                "item_ids": g.item_ids,
                "warnings": g.warnings,
            }
            for g in preview.groups
        ],
        "items": [_item_to_dict(item) for item in preview.items],
    }


def _item_to_dict(item) -> dict:
    """将 ImportPlanItem 转为 dict"""
    return {
        "id": item.id,
        "plan_id": item.plan_id,
        "raw_file_id": item.raw_file_id,
        "source": item.source,
        "relative_path": item.relative_path,
        "real_path": item.real_path,
        "source_size": item.source_size,
        "source_mtime": item.source_mtime,
        "source_fingerprint": item.source_fingerprint,
        "availability": item.availability,
        "resource_type": item.resource_type,
        "action": item.action,
        "work_id": item.work_id,
        "canonical_work_id": item.canonical_work_id,
        "work_title": item.work_title,
        "original_title": item.original_title,
        "year": item.year,
        "media_type": item.media_type,
        "show_type": item.show_type,
        "tmdb_hint_id": item.tmdb_hint_id,
        "tmdb_hint_type": item.tmdb_hint_type,
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
        "target_filename": item.target_filename,
        "target_strm_path": item.target_strm_path,
        "confidence": item.confidence,
        "needs_review": item.needs_review,
        "reasons": item.reasons,
        "warnings": item.warnings,
        "user_override_id": item.user_override_id,
    }


# ============================================================
# Diff / 增量端点
# ============================================================

class DiffRequest(BaseModel):
    old_snapshot_id: Optional[str] = None
    new_snapshot_id: Optional[str] = None


class IncrementalCommitRequest(BaseModel):
    plan_id: str
    auto_scrape: bool = True


@router.post("/{source}/diff")
def create_diff(source: str, req: DiffRequest = DiffRequest()):
    """生成增量 diff

    对比新旧 RawSnapshot，返回新增 / 缺失 / 移动 / 改名结果。
    """
    from app.import_plan.diff import compute_diff
    old_snapshot, new_snapshot = _load_diff_snapshots(source, req)

    diff_result = compute_diff(old_snapshot, new_snapshot)

    # 保存 diff
    from app.import_plan.store import save_diff_result
    save_diff_result(diff_result)

    return _diff_to_dict(diff_result)


@router.get("/{source}/diff/{diff_id}")
def get_diff(source: str, diff_id: str):
    """获取已保存的 diff 结果"""
    from app.import_plan.store import load_diff_result
    diff = load_diff_result(diff_id)
    if diff is None:
        raise HTTPException(status_code=404, detail=f"diff 不存在: {diff_id}")
    if diff.source != source:
        raise HTTPException(status_code=400, detail=f"diff.source={diff.source} 与 URL source={source} 不匹配")
    return _diff_to_dict(diff)


@router.post("/{source}/incremental/preview")
def incremental_preview(source: str, req: DiffRequest = DiffRequest()):
    """基于 diff 的 added 文件生成增量导入预览"""
    from app.import_plan.diff import compute_diff
    from app.import_plan.incremental import build_incremental_plan, merge_incremental_plan
    from app.import_plan.store import load_latest_confirmed_import_plan

    old_plan = load_latest_confirmed_import_plan(source)
    if old_plan is None:
        raise HTTPException(status_code=404, detail="未找到旧 baseline plan")
    if old_plan.source != source:
        raise HTTPException(status_code=400, detail=f"old_plan.source={old_plan.source} 与 URL source={source} 不匹配")

    old_snapshot, new_snapshot = _load_diff_snapshots(source, req, old_plan=old_plan)

    diff_result = compute_diff(old_snapshot, new_snapshot)

    if diff_result.safety.blocked:
        raise HTTPException(
            status_code=409,
            detail=f"增量扫描被安全阈值阻断: {'; '.join(diff_result.safety.reasons)}",
        )

    incremental_plan = build_incremental_plan(
        diff_result, source, new_snapshot.source_root, new_snapshot=new_snapshot,
    )
    if incremental_plan is None:
        return {"items": [], "summary": {"total_items": 0, "message": "无新增视频文件"}}

    cumulative_plan = merge_incremental_plan(old_plan, incremental_plan, diff_result, status="draft")
    save_import_plan(cumulative_plan, update_latest=False)
    preview = build_preview(cumulative_plan)
    return _preview_to_dict(preview)


@router.post("/{source}/incremental/commit")
def incremental_commit(source: str, req: IncrementalCommitRequest):
    plan = _load_plan_or_404(plan_id=req.plan_id)
    if plan.source != source:
        raise HTTPException(status_code=400, detail="计划来源与 URL 不匹配")
    if plan.status != "draft":
        raise HTTPException(status_code=409, detail="只有待确认的增量计划可以提交")
    from app.import_pipeline.service import run_auto_import_pipeline
    from app.tasks.registry import get_task_manager
    try:
        task = get_task_manager().submit(
            "import_incremental", source, run_auto_import_pipeline,
            source, plan.plan_id, include_scrape=req.auto_scrape,
            message="提交增量导入计划",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task_id": task.task_id, "status": task.status, "plan_id": plan.plan_id}


def load_latest_plan_for_diff(source: str) -> Optional[ImportPlan]:
    """加载最新的 confirmed/executed plan 作为 diff baseline"""
    from app.import_plan.store import load_latest_confirmed_import_plan
    return load_latest_confirmed_import_plan(source)


def _load_diff_snapshots(source: str, req: DiffRequest, old_plan: Optional[ImportPlan] = None):
    from app.raw.store import load_latest_raw_snapshot, load_raw_snapshot
    if old_plan is None:
        old_plan = load_latest_plan_for_diff(source)
    old_id = req.old_snapshot_id or (old_plan.source_snapshot_id if old_plan else "")
    old_snapshot = load_raw_snapshot(old_id) if old_id else None
    new_snapshot = load_raw_snapshot(req.new_snapshot_id) if req.new_snapshot_id else load_latest_raw_snapshot(source)
    if old_snapshot is None:
        raise HTTPException(status_code=404, detail="未找到旧 RawSnapshot baseline")
    if new_snapshot is None:
        raise HTTPException(status_code=404, detail="未找到新 RawSnapshot，请先解析或扫盘")
    if old_snapshot.source != source or new_snapshot.source != source:
        raise HTTPException(status_code=400, detail="RawSnapshot 来源与 URL source 不匹配")
    return old_snapshot, new_snapshot


def _plan_to_approximate_snapshot(plan: ImportPlan):
    """从 ImportPlan 构造近似 RawSnapshot（用于 diff）"""
    from app.raw.models import RawSnapshot, RawFile
    files = []
    for item in plan.items:
        rf = RawFile(
            id=item.raw_file_id,
            source=item.source,
            relative_path=item.relative_path,
            real_path=item.real_path,
            name=item.relative_path.split("/")[-1] if item.relative_path else "",
            ext="." + item.relative_path.split(".")[-1] if item.relative_path and "." in item.relative_path else "",
            resource_hint=item.resource_type,
        )
        files.append(rf)
    return RawSnapshot(
        snapshot_id=plan.source_snapshot_id or plan.plan_id,
        source=plan.source,
        created_at=plan.created_at,
        file_count=len(files),
        video_count=sum(1 for f in files if f.resource_hint == "video"),
        files=files,
    )


def _diff_to_dict(diff) -> dict:
    return {
        "diff_id": diff.diff_id,
        "source": diff.source,
        "old_snapshot_id": diff.old_snapshot_id,
        "new_snapshot_id": diff.new_snapshot_id,
        "old_video_count": diff.old_video_count,
        "new_video_count": diff.new_video_count,
        "added_count": diff.added_count,
        "missing_count": diff.missing_count,
        "moved_count": diff.moved_count,
        "renamed_count": diff.renamed_count,
        "unchanged_count": diff.unchanged_count,
        "replaced_count": diff.replaced_count,
        "uncertain_count": diff.uncertain_count,
        "safety": {
            "blocked": diff.safety.blocked,
            "delete_ratio": diff.safety.delete_ratio,
            "path_change_ratio": diff.safety.path_change_ratio,
            "total_change_ratio": diff.safety.total_change_ratio,
            "reasons": diff.safety.reasons,
        },
        "items": [
            {
                "item_id": i.item_id,
                "change_type": i.change_type,
                "source": i.source,
                "old_relative_path": i.old_relative_path,
                "new_relative_path": i.new_relative_path,
                "resource_type": i.resource_type,
                "confidence": i.confidence,
                "reasons": i.reasons,
                "needs_review": i.needs_review,
            }
            for i in diff.items
        ],
    }
