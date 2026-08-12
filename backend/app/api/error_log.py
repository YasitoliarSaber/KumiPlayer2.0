# -*- coding: utf-8 -*-
"""错误日志 API

GET  /api/error-log           — 获取错误列表
GET  /api/error-log/stats     — 获取错误统计
POST /api/error-log/resolve   — 标记单条已处理
POST /api/error-log/resolve-all — 批量标记已处理
"""

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import error_log

router = APIRouter(prefix="/api/error-log", tags=["error-log"])


class ResolveRequest(BaseModel):
    error_id: str


class ResolveAllRequest(BaseModel):
    source: Optional[str] = None
    stage: Optional[str] = None
    category: Optional[str] = None


@router.get("")
def get_errors(days: int = 7):
    """获取最近 N 天的错误日志"""
    errors = error_log.load_recent_errors(days=days)
    return {"errors": errors, "count": len(errors)}


@router.get("/stats")
def get_stats():
    """获取错误统计"""
    return error_log.get_error_stats()


@router.get("/export")
def export_errors(days: int = 90):
    """导出完整错误日志（JSON Lines 文本），供人工分析。

    与界面展示不同：这里返回全部字段（含结构化 context），
    并按时间正序整理为可读文本，方便粘贴到 issue / 日志分析。
    """
    from fastapi.responses import PlainTextResponse

    errors = error_log.load_recent_errors(days=days)
    errors.sort(key=lambda e: e.get("timestamp", ""), reverse=False)
    lines = []
    for entry in errors:
        lines.append(
            "[{}] {} · {} · {} · {}{}".format(
                entry.get("timestamp", ""),
                entry.get("level", ""),
                entry.get("stage", ""),
                entry.get("category", ""),
                entry.get("source", "all"),
                " · 已处理" if entry.get("resolved") else "",
            )
        )
        lines.append("    " + str(entry.get("message", "")))
        if entry.get("context"):
            import json as _json

            lines.append("    context: " + _json.dumps(entry.get("context"), ensure_ascii=False))
    body = "\n".join(lines)
    if not body:
        body = "（暂无错误日志）\n"
    return PlainTextResponse(body, media_type="text/plain; charset=utf-8")


@router.post("/resolve")
def resolve_error(req: ResolveRequest):
    """标记单条错误为已处理"""
    ok = error_log.resolve_error(req.error_id)
    return {"ok": ok, "error_id": req.error_id}


@router.post("/resolve-all")
def resolve_all(req: ResolveAllRequest):
    """批量标记为已处理"""
    count = error_log.resolve_all(stage=req.stage, category=req.category)
    return {"ok": True, "resolved_count": count}


@router.delete("/{error_id}")
def delete_error(error_id: str):
    """删除单条错误日志。"""
    ok = error_log.delete_error(error_id)
    return {"ok": ok, "error_id": error_id}


@router.post("/purge")
def purge_errors(req: ResolveAllRequest):
    """删除匹配条件的错误日志。空条件表示清空全部。"""
    count = error_log.purge_errors(source=req.source, stage=req.stage, category=req.category)
    return {"ok": True, "deleted_count": count}
