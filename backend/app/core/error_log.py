# -*- coding: utf-8 -*-
"""统一错误日志

全局错误日志入口，覆盖 import_plan / scrape / mirror 全流程。
日志文件写在 data/logs/error/ 下，按日期滚动。

文件结构：
  data/logs/error/
    errors_YYYY-MM-DD.json   — 当天错误日志（JSON Lines，每行一条）
    errors_latest.json       — 最近 7 天聚合（前端读取入口）

每条记录字段：
  {
    "id": "err_<uuid8>",
    "timestamp": "ISO 8601",
    "stage": "import_plan | scrape | mirror | library | other",
    "category": "needs_review | scrape_failed | mirror_failed | ...",
    "level": "warning | error",
    "message": "人类可读摘要",
    "context": { ... },           # 结构化上下文，不同 stage 字段不同
    "source": "pan115 | baidu | local | all",
    "resolved": false              # 前端可标记已处理
  }
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from app.core.atomic_json import write_text_atomic
from app.core.data_lock import DATA_WRITE_LOCK

tz_cn = timezone(timedelta(hours=8))


def _get_error_log_dir() -> Path:
    """错误日志目录：data/logs/error"""
    from app.core.paths import get_data_dir
    log_dir = get_data_dir() / "logs" / "error"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _now_iso() -> str:
    return datetime.now(tz_cn).isoformat()


def _gen_id() -> str:
    return f"err_{uuid.uuid4().hex[:8]}"


def log_error(
    stage: str,
    category: str,
    message: str,
    level: str = "error",
    source: str = "",
    context: Optional[dict] = None,
) -> str:
    """写入一条错误日志

    参数:
        stage: 流程阶段 (import_plan / scrape / mirror / library / other)
        category: 错误分类 (needs_review / scrape_failed / ...)
        message: 人类可读摘要
        level: warning / error
        source: 来源 (pan115 / baidu / local / all)
        context: 结构化上下文

    返回:
        生成的错误 ID
    """
    entry = {
        "id": _gen_id(),
        "timestamp": _now_iso(),
        "stage": stage,
        "category": category,
        "level": level,
        "message": message,
        "source": source or "",
        "context": context or {},
        "resolved": False,
    }

    _append_to_daily_file(entry)
    return entry["id"]


def _append_to_daily_file(entry: dict) -> None:
    """追加到当天日志文件（JSON Lines 格式）"""
    with DATA_WRITE_LOCK:
        log_dir = _get_error_log_dir()
        today = datetime.now(tz_cn).strftime("%Y-%m-%d")
        path = log_dir / f"errors_{today}.jsonl"
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_recent_errors(days: int = 7) -> list[dict]:
    """读取最近 N 天的错误日志（用于前端展示）

    按时间倒序返回。
    """
    log_dir = _get_error_log_dir()
    entries: list[dict] = []

    for i in range(days):
        day = datetime.now(tz_cn) - timedelta(days=i)
        fname = f"errors_{day.strftime('%Y-%m-%d')}.jsonl"
        path = log_dir / fname
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries


def resolve_error(error_id: str) -> bool:
    with DATA_WRITE_LOCK:
        return _resolve_error_unlocked(error_id)


def _resolve_error_unlocked(error_id: str) -> bool:
    """标记某条错误为已处理"""
    log_dir = _get_error_log_dir()
    for path in log_dir.glob("errors_*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        new_lines: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if entry.get("id") == error_id:
                entry["resolved"] = True
                changed = True
            new_lines.append(json.dumps(entry, ensure_ascii=False))
        if changed:
            write_text_atomic(path, "\n".join(new_lines) + "\n")
            return True
    return False


def delete_error(error_id: str) -> bool:
    with DATA_WRITE_LOCK:
        return _delete_error_unlocked(error_id)


def _delete_error_unlocked(error_id: str) -> bool:
    """从错误日志文件中删除某条记录。"""
    log_dir = _get_error_log_dir()
    for path in log_dir.glob("errors_*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        new_lines: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if entry.get("id") == error_id:
                changed = True
                continue
            new_lines.append(json.dumps(entry, ensure_ascii=False))
        if changed:
            if new_lines:
                write_text_atomic(path, "\n".join(new_lines) + "\n")
            else:
                path.unlink(missing_ok=True)
            return True
    return False


def purge_errors(
    source: Optional[str] = None,
    stage: Optional[str] = None,
    category: Optional[str] = None,
    resolved: Optional[bool] = None,
) -> int:
    with DATA_WRITE_LOCK:
        return _purge_errors_unlocked(source, stage, category, resolved)


def _purge_errors_unlocked(
    source: Optional[str] = None,
    stage: Optional[str] = None,
    category: Optional[str] = None,
    resolved: Optional[bool] = None,
) -> int:
    """删除匹配条件的错误日志。None 表示该条件不过滤。"""
    log_dir = _get_error_log_dir()
    count = 0

    for path in log_dir.glob("errors_*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        changed = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if _matches_purge(entry, source, stage, category, resolved):
                count += 1
                changed = True
                continue
            new_lines.append(json.dumps(entry, ensure_ascii=False))
        if changed:
            if new_lines:
                write_text_atomic(path, "\n".join(new_lines) + "\n")
            else:
                path.unlink(missing_ok=True)
    return count


def _matches_purge(
    entry: dict,
    source: Optional[str],
    stage: Optional[str],
    category: Optional[str],
    resolved: Optional[bool],
) -> bool:
    if source and source != "all" and entry.get("source") != source:
        return False
    if stage and entry.get("stage") != stage:
        return False
    if category and entry.get("category") != category:
        return False
    if resolved is not None and bool(entry.get("resolved")) != resolved:
        return False
    return True


def resolve_all(stage: Optional[str] = None, category: Optional[str] = None) -> int:
    with DATA_WRITE_LOCK:
        return _resolve_all_unlocked(stage, category)


def _resolve_all_unlocked(stage: Optional[str] = None, category: Optional[str] = None) -> int:
    """批量标记为已处理

    参数:
        stage: 只处理指定阶段，None 则全部
        category: 只处理指定分类，None 则全部

    返回:
        标记数量
    """
    log_dir = _get_error_log_dir()
    count = 0

    for path in log_dir.glob("errors_*.jsonl"):
        lines = path.read_text(encoding="utf-8").splitlines()
        new_lines: list[str] = []
        changed = False
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                new_lines.append(line)
                continue
            if entry.get("resolved"):
                new_lines.append(json.dumps(entry, ensure_ascii=False))
                continue
            if stage and entry.get("stage") != stage:
                new_lines.append(json.dumps(entry, ensure_ascii=False))
                continue
            if category and entry.get("category") != category:
                new_lines.append(json.dumps(entry, ensure_ascii=False))
                continue
            entry["resolved"] = True
            count += 1
            changed = True
            new_lines.append(json.dumps(entry, ensure_ascii=False))
        if changed:
            write_text_atomic(path, "\n".join(new_lines) + "\n")

    return count


def get_error_stats() -> dict:
    """获取错误统计（用于前端概览）"""
    errors = load_recent_errors()
    unresolved = [e for e in errors if not e.get("resolved")]

    by_stage: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for e in unresolved:
        s = e.get("stage", "other")
        c = e.get("category", "unknown")
        by_stage[s] = by_stage.get(s, 0) + 1
        by_category[c] = by_category.get(c, 0) + 1

    return {
        "total": len(errors),
        "unresolved": len(unresolved),
        "by_stage": by_stage,
        "by_category": by_category,
    }
