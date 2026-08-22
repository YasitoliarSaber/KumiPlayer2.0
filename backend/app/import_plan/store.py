"""ImportPlan 和 UserOverrides 的 JSON 文件存储

第一版使用 JSON 文件保存，不使用 SQLite。
"""

import json
from dataclasses import asdict
from pathlib import Path

from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK
from app.import_plan.models import ImportPlan
from app.import_plan.overrides import UserOverride, UserOverridesFile


def _get_data_dir() -> Path:
    """获取数据目录，不存在则创建"""
    from app.core.paths import get_data_dir
    return get_data_dir()


def _get_import_plans_dir() -> Path:
    """获取 import_plans 目录，不存在则创建"""
    plans_dir = _get_data_dir() / "import_plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    return plans_dir


def _get_user_overrides_path() -> Path:
    """获取 user_overrides.json 路径"""
    return _get_data_dir() / "user_overrides.json"


# ============================================================
# ImportPlan 存储
# ============================================================

def save_import_plan(plan: ImportPlan, update_latest: bool = True) -> str:
    """保存 ImportPlan 到 JSON 文件

    保存到：
    - data/import_plans/{plan_id}.json
    - data/import_plans/{source}_latest.json

    返回:
        保存路径
    """
    with DATA_WRITE_LOCK:
        plans_dir = _get_import_plans_dir()
        plan_dict = asdict(plan)
        plan_path = plans_dir / f"{plan.plan_id}.json"
        write_json_atomic(plan_path, plan_dict)
        if update_latest:
            latest_path = plans_dir / f"{plan.source}_latest.json"
            write_json_atomic(latest_path, plan_dict)
        return str(plan_path)


def load_import_plan(plan_id: str | None = None, source: str | None = None) -> ImportPlan | None:
    """加载 ImportPlan

    参数:
        plan_id: 指定 plan_id 加载（V3 SQLite revision 优先，legacy JSON 兜底）
        source: 无 plan_id 时，加载 {source}_latest.json（保持 legacy 行为）

    V3（OpenList 新链路）以 SQLite Import Revision 为唯一事实源：明确传
    plan_id 时先查 revision_store，存在即返回 SQLite；同 ID 同时存在 SQLite
    draft 与 JSON confirmed 时，必须读到 SQLite draft（可编辑的当前草稿），
    避免人工修正与确认读到旧 JSON 的 split-brain。

    返回:
        ImportPlan 或 None
    """
    plans_dir = _get_import_plans_dir()

    if plan_id:
        # V3 优先：SQLite Import Revision（OpenList 新链路唯一事实源）
        from app.import_plan.revision_store import load_plan

        sqlite_plan = load_plan(plan_id)
        if sqlite_plan is not None:
            return sqlite_plan
        # 回退 legacy JSON（115/百度/本地旧来源）
        plan_path = plans_dir / f"{plan_id}.json"
        if not plan_path.exists():
            return None
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        return _dict_to_import_plan(data)

    if source:
        plan_path = plans_dir / f"{source}_latest.json"
        if not plan_path.exists():
            return None
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        return _dict_to_import_plan(data)

    return None


def load_latest_confirmed_import_plan(source: str) -> ImportPlan | None:
    """加载指定 source 最新的 confirmed/executed ImportPlan

    扫描 data/import_plans/ 目录，找最新且 status=confirmed 或 executed 的 plan。
    executed 表示该计划已经生成过镜像，仍然是媒体库扫描和增量 diff 的有效基准。
    优先检查 {source}_latest.json，如果不是可用状态，再扫描所有文件。
    """
    plans_dir = _get_import_plans_dir()
    usable_statuses = {"confirmed", "executed"}

    # 1. 先检查 {source}_latest.json
    latest_path = plans_dir / f"{source}_latest.json"
    if latest_path.exists():
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        plan = _dict_to_import_plan(data)
        if (
            plan
            and plan.status in usable_statuses
            and plan.source == source
            and not _is_scoped_tracking_plan(plan)
            and plan.import_scope != "seasonal"
        ):
            return plan

    # 2. 扫描所有文件，找最新可用计划
    best_plan = None
    best_rank = (-1, 0.0)
    for f in plans_dir.glob("*.json"):
        if "_latest" in f.name:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("status") not in usable_statuses:
                continue
            if data.get("source") != source:
                continue
            plan = _dict_to_import_plan(data)
            if _is_scoped_tracking_plan(plan):
                continue
            # 新番目录是来源子集：只在没有任何全量计划时才作为回退。
            rank = (0 if plan.import_scope == "seasonal" else 1, f.stat().st_mtime)
            if rank > best_rank:
                best_rank = rank
                best_plan = plan
        except (json.JSONDecodeError, KeyError):
            continue

    return best_plan


def _is_scoped_tracking_plan(plan: ImportPlan) -> bool:
    """追更作品切片是增量记录，不是来源级全量基线。"""
    return bool(
        plan.summary.get("tracking_binding_id")
        or "_tracking_" in (plan.plan_id or "")
    )


def _dict_to_import_plan(data: dict) -> ImportPlan:
    """从 dict 还原 ImportPlan"""
    from app.import_plan.models import ImportPlanItem

    items = []
    for item_data in data.get("items", []):
        # 处理 None 值字段
        for key in ("year", "season_number", "episode_number", "special_number", "user_override_id"):
            if key in item_data and item_data[key] is not None:
                pass
        items.append(ImportPlanItem(**item_data))

    return ImportPlan(
        plan_id=data.get("plan_id", ""),
        source=data.get("source", ""),
        provider_id=data.get("provider_id", ""),
        ingest_method=data.get("ingest_method", ""),
        source_route_id=data.get("source_route_id", ""),
        source_snapshot_id=data.get("source_snapshot_id", ""),
        import_family=data.get("import_family", ""),
        import_scope=data.get("import_scope", ""),
        created_at=data.get("created_at", ""),
        updated_at=data.get("updated_at", ""),
        status=data.get("status", "draft"),
        items=items,
        warnings=data.get("warnings", []),
        summary=data.get("summary", {}),
    )


# ============================================================
# UserOverrides 存储
# ============================================================

def load_user_overrides() -> UserOverridesFile:
    """加载 user_overrides.json"""
    path = _get_user_overrides_path()
    if not path.exists():
        return UserOverridesFile()

    data = json.loads(path.read_text(encoding="utf-8"))
    items = []
    for item_data in data.get("items", []):
        items.append(UserOverride(**item_data))
    return UserOverridesFile(version=data.get("version", 1), items=items)


def save_user_override(override: UserOverride) -> None:
    """追加一条 UserOverride 到 user_overrides.json"""
    with DATA_WRITE_LOCK:
        overrides_file = load_user_overrides()
        overrides_file.items = [
            o for o in overrides_file.items if o.item_id != override.item_id
        ]
        overrides_file.items.append(override)
        path = _get_user_overrides_path()
        write_json_atomic(path, asdict(overrides_file))


# ============================================================
# DiffResult 存储
# ============================================================

def save_diff_result(diff_result) -> str:
    """保存 DiffResult（与同文件其余 store 一致持 DATA_WRITE_LOCK）"""
    with DATA_WRITE_LOCK:
        diffs_dir = _get_data_dir() / "diffs"
        diffs_dir.mkdir(parents=True, exist_ok=True)
        path = diffs_dir / f"{diff_result.diff_id}.json"
        write_json_atomic(path, asdict(diff_result))
    return str(path)


def load_diff_result(diff_id: str):
    """加载 DiffResult"""
    from app.import_plan.diff import DiffItem, DiffResult, DiffSafety
    diffs_dir = _get_data_dir() / "diffs"
    path = diffs_dir / f"{diff_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        items = [DiffItem(**item) for item in data.get("items", [])]
        safety = DiffSafety(**data.get("safety", {}))
        return DiffResult(
            diff_id=data.get("diff_id", ""),
            source=data.get("source", ""),
            old_snapshot_id=data.get("old_snapshot_id", ""),
            new_snapshot_id=data.get("new_snapshot_id", ""),
            created_at=data.get("created_at", ""),
            old_video_count=data.get("old_video_count", 0),
            new_video_count=data.get("new_video_count", 0),
            added_count=data.get("added_count", 0),
            missing_count=data.get("missing_count", 0),
            moved_count=data.get("moved_count", 0),
            renamed_count=data.get("renamed_count", 0),
            unchanged_count=data.get("unchanged_count", 0),
            replaced_count=data.get("replaced_count", 0),
            uncertain_count=data.get("uncertain_count", 0),
            safety=safety,
            items=items,
        )
    except (json.JSONDecodeError, KeyError):
        return None
