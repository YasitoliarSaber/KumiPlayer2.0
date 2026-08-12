# -*- coding: utf-8 -*-
"""ScrapeMap 持久化"""

import json
import os
import traceback
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Optional

from app.scrape.models import ScrapeMap, ScrapeMapItem
from app.core.atomic_json import write_json_atomic
from app.core.data_lock import DATA_WRITE_LOCK


def _get_scrape_dir() -> Path:
    from app.core.paths import get_data_dir
    scrape_dir = get_data_dir() / "scrape"
    scrape_dir.mkdir(parents=True, exist_ok=True)
    return scrape_dir


def load_scrape_map() -> ScrapeMap:
    path = _get_scrape_dir() / "scrape_map.json"
    if not path.exists():
        return ScrapeMap()
    data = json.loads(path.read_text(encoding="utf-8"))
    items = [ScrapeMapItem(**it) for it in data.get("items", [])]
    return ScrapeMap(version=data.get("version", 1), items=items)


def save_scrape_map(scrape_map: ScrapeMap) -> str:
    with DATA_WRITE_LOCK:
        path = _get_scrape_dir() / "scrape_map.json"
        write_json_atomic(path, asdict(scrape_map))
        return str(path)


def upsert_scrape_map_item(item: ScrapeMapItem) -> None:
    """新增或更新映射，兼容 ImportPlan 变化导致的 target ID 漂移。"""
    with DATA_WRITE_LOCK:
        sm = load_scrape_map()
        new_path = _map_path_key(item.nfo_path)
        sm.items = [
            existing
            for existing in sm.items
            if existing.scrape_target_id != item.scrape_target_id
            and not (
                new_path
                and existing.source == item.source
                and _map_path_key(existing.nfo_path) == new_path
            )
        ]
        sm.items.append(item)
        save_scrape_map(sm)


def _map_path_key(value: str) -> str:
    if not value:
        return ""
    return os.path.abspath(value).casefold()


def load_failed_cases() -> list:
    path = _get_scrape_dir() / "failed_cases.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_failed_case(case: dict) -> None:
    case = _normalize_failed_case(case)
    with DATA_WRITE_LOCK:
        cases = load_failed_cases()
        cases.append(case)
        path = _get_scrape_dir() / "failed_cases.json"
        write_json_atomic(path, cases)
    _save_failed_case_to_db(case)
    _save_failed_case_to_error_log(case)


def _save_failed_case_to_error_log(case: dict) -> None:
    """将 failed case 同步写入统一错误日志"""
    try:
        from app.core.error_log import log_error
        context = _compact_error_context(case)
        log_error(
            stage="scrape",
            category="scrape_failed",
            message=_failure_message(case),
            level="error",
            source=case.get("source", ""),
            context=context,
        )
    except Exception:
        pass


def build_failed_case(
    *,
    target: Any = None,
    error: Any = "",
    stage: str = "",
    tmdb_id: Optional[int] = None,
    tmdb_type: str = "",
    candidate: Any = None,
    candidates: Optional[list[Any]] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Build a detailed scrape failure record for later rule debugging."""
    case = dict(extra or {})
    target_data = _safe_dataclass_dict(target)
    candidate_data = _safe_dataclass_dict(candidate)
    candidate_list = [_safe_dataclass_dict(c) for c in (candidates or [])[:8]]

    if target_data:
        case["target"] = _target_snapshot(target_data)
        case.setdefault("scrape_target_id", target_data.get("scrape_target_id", ""))
        case.setdefault("source", target_data.get("source", ""))
    if candidate_data:
        case["selected_candidate"] = _candidate_snapshot(candidate_data, include_reasons=True)
        case.setdefault("tmdb_id", candidate_data.get("tmdb_id"))
        case.setdefault("tmdb_type", candidate_data.get("tmdb_type", ""))
    if candidate_list:
        case["candidates"] = [
            _candidate_snapshot(item, include_reasons=True)
            for item in candidate_list
        ]
    if tmdb_id is not None:
        case["tmdb_id"] = tmdb_id
    if tmdb_type:
        case["tmdb_type"] = tmdb_type
    case["stage"] = stage or case.get("stage", "")
    case["error"] = str(error) if error else case.get("error", "")
    if isinstance(error, BaseException):
        case["exception_type"] = error.__class__.__name__
        case["traceback"] = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    elif error:
        case["exception_type"] = ""
        case["traceback"] = ""
    return case


def _normalize_failed_case(case: dict) -> dict:
    data = _jsonable(case)
    if not data.get("timestamp"):
        from datetime import datetime, timezone, timedelta
        data["timestamp"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    if "target" not in data:
        data["target"] = _target_snapshot(data)
    return data


def _safe_dataclass_dict(value: Any) -> dict:
    if value is None:
        return {}
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _target_snapshot(data: dict) -> dict:
    keys = [
        "scrape_target_id", "source", "import_plan_id", "work_id",
        "card_type", "media_type", "show_type", "group_type",
        "series_group", "local_title", "original_title", "source_subwork_dir",
        "local_year", "local_season_number", "scrape_title", "scrape_year",
        "scrape_type", "tmdb_hint_id", "tmdb_hint_type", "target_dir",
        "target_nfo_path", "target_poster_path", "target_fanart_path",
        "target_clearlogo_path", "item_ids", "needs_review", "warnings",
    ]
    return {key: data.get(key) for key in keys if key in data}


def _candidate_snapshot(data: dict, include_reasons: bool = False) -> dict:
    keys = [
        "candidate_id", "provider", "tmdb_id", "tmdb_type", "title",
        "original_title", "year", "score", "popularity", "vote_average",
        "poster_path",
    ]
    result = {key: data.get(key) for key in keys if key in data}
    if include_reasons:
        result["reasons"] = data.get("reasons", [])
        raw = data.get("raw") or {}
        result["identity_evidence"] = {
            "provider_title_aliases": raw.get("provider_title_aliases") or [],
            "provider_tmdb_link": raw.get("provider_tmdb_link") or "",
        }
    return result


def _compact_error_context(case: dict) -> dict:
    target = case.get("target") or {}
    return {
        "scrape_target_id": case.get("scrape_target_id") or target.get("scrape_target_id", ""),
        "source": case.get("source") or target.get("source", ""),
        "stage": case.get("stage", ""),
        "timestamp": case.get("timestamp", ""),
        "error": case.get("error", ""),
        "exception_type": case.get("exception_type", ""),
        "tmdb_id": case.get("tmdb_id"),
        "tmdb_type": case.get("tmdb_type", ""),
        "target": target,
        "selected_candidate": case.get("selected_candidate") or {},
        "candidates": case.get("candidates") or [],
        "provider": case.get("provider", ""),
    }


def _failure_message(case: dict) -> str:
    target = case.get("target") or {}
    title = target.get("scrape_title") or target.get("local_title") or target.get("series_group") or case.get("scrape_target_id") or "未知目标"
    stage = case.get("stage") or "scrape"
    error = case.get("error") or "刮削失败"
    return f"{title} / {stage}: {error}"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except TypeError:
        return str(value)


def _save_failed_case_to_db(case: dict) -> None:
    """将 failed case 双写到 SQLite；失败不影响 JSON 主流程。"""
    try:
        from app.db.database import close_connection, get_connection, init_db
        init_db()
        conn = get_connection()
        conn.execute("""
            INSERT INTO failed_cases
            (scrape_target_id, tmdb_id, tmdb_type, error, stage, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            case.get("scrape_target_id", ""),
            case.get("tmdb_id"),
            case.get("tmdb_type", ""),
            case.get("error", ""),
            case.get("stage", ""),
            case.get("timestamp", ""),
        ))
        conn.commit()
    except Exception:
        pass
    finally:
        try:
            from app.db.database import close_connection
            close_connection()
        except Exception:
            pass
