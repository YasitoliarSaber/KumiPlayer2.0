"""KumiPlayer 2.0 FastAPI 应用入口"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.assets import router as assets_router
from app.api.bangumi import router as bangumi_router
from app.api.config import router as config_router
from app.api.error_log import router as error_log_router
from app.api.heartbeat import router as heartbeat_router
from app.api.imports import router as imports_router
from app.api.library import router as library_router
from app.api.media_presets import router as media_presets_router
from app.api.mirror import router as mirror_router
from app.api.openlist import router as openlist_router
from app.api.playback import router as playback_router
from app.api.scrape import router as scrape_router
from app.api.sources import router as sources_router
from app.api.system import router as system_router
from app.api.tasks import router as tasks_router
from app.api.tracking import router as tracking_router
from app.core.api_security import ApiSessionMiddleware
from app.pipeline.handlers import register_pipeline_handlers

# 模块加载即注册 durable handlers：重启恢复时 JobRunner 领取任务必须能
# 从注册表解析 job_type；注册是幂等的，lifespan 中重复调用无副作用。
register_pipeline_handlers()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库、恢复持久任务队列和心跳监控，关闭时停止"""
    from app.db import init_db
    from app.db.tasks import mark_interrupted_tasks_failed
    from app.jobs.runner import JobRunner
    from app.system.heartbeat import get_heartbeat_manager

    # 初始化数据库
    init_db()
    mark_interrupted_tasks_failed()

    # 持久任务 worker：durable handlers 已在模块顶层注册，此处再次调用仅为防御。
    # 独立 worker 分流：扫描（discovery_scan）、镜像（mirror_revision）、
    # 刮削（scrape_revision）+ 媒体库重建（library_rebuild）各自专用线程；
    # 无类型通用 worker 会抢走 pipeline job，因此这里不创建通用 worker；
    # 同类型任务内部由 resource_key 互斥，背压由 discovery handler 内队列水位检查控制。
    register_pipeline_handlers()
    job_runner_scan = JobRunner(job_types=["discovery_scan"], claim_limit=1, worker_id="worker-scan")
    job_runner_scan.start()
    job_runner_mirror = JobRunner(job_types=["mirror_revision"], claim_limit=2, worker_id="worker-mirror")
    job_runner_mirror.start()
    job_runner_scrape = JobRunner(job_types=["scrape_revision"], claim_limit=1, worker_id="worker-scrape")
    job_runner_scrape.start()
    job_runner_library = JobRunner(job_types=["library_rebuild"], claim_limit=1, worker_id="worker-library")
    job_runner_library.start()

    manager = get_heartbeat_manager()
    manager.start_monitor()
    try:
        yield
    finally:
        manager.stop_monitor()
        job_runner_scan.stop()
        job_runner_mirror.stop()
        job_runner_scrape.stop()
        job_runner_library.stop()


_BUNDLED_RUNTIME = os.environ.get("KUMIPLAYER_RUNTIME_KIND") == "bundled"

app = FastAPI(
    title="KumiPlayer",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if _BUNDLED_RUNTIME else "/docs",
    redoc_url=None if _BUNDLED_RUNTIME else "/redoc",
    openapi_url=None if _BUNDLED_RUNTIME else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://tauri.localhost",
        "tauri://localhost",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-KumiPlayer-Token"],
)
app.add_middleware(ApiSessionMiddleware)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)

# 注册路由
app.include_router(imports_router)
app.include_router(mirror_router)
app.include_router(scrape_router)
app.include_router(library_router)
app.include_router(playback_router)
app.include_router(heartbeat_router)
app.include_router(sources_router)
app.include_router(config_router)
app.include_router(assets_router)
app.include_router(bangumi_router)
app.include_router(system_router)
app.include_router(tasks_router)
app.include_router(error_log_router)
app.include_router(tracking_router)
app.include_router(media_presets_router)
app.include_router(openlist_router)


@app.get("/api/health")
def health():
    """健康检查"""
    return {
        "status": "ok",
        "app": "KumiPlayer",
        "runtime_kind": os.environ.get("KUMIPLAYER_RUNTIME_KIND", "source"),
        "runtime_id": os.environ.get("KUMIPLAYER_RUNTIME_ID", ""),
        "instance_id": os.environ.get("KUMIPLAYER_INSTANCE_ID", ""),
    }


# ============================================================
# 前端静态文件托管
# ============================================================

# 前端构建产物目录（Tauri / Vite 构建输出）
_FRONTEND_DIST = Path(__file__).parent.parent.parent / "dist"


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """根路径返回前端 index.html"""
    index_path = _FRONTEND_DIST / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        "<h1>KumiPlayer 2.0</h1>"
        "<p>前端未构建。请运行 <code>npm run build</code></p>"
        "<p>后端 API 已就绪：<a href='/docs'>/docs</a></p>"
    )


# 挂载前端静态资源
if _FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="static-assets")


@app.exception_handler(404)
async def spa_fallback(request: Request, exc):
    """SPA 路由回退：非 API 路径返回 index.html"""
    path = request.url.path
    # API 和 WebSocket 路径不回退
    if path.startswith("/api") or path.startswith("/ws") or path.startswith("/docs") or path.startswith("/openapi"):
        # 不要把业务接口原本可操作的错误（例如“媒体库预设不存在”）
        # 覆盖成没有上下文的 Not Found，同时确保 API 始终返回 JSON。
        detail = getattr(exc, "detail", "请求的接口不存在")
        return JSONResponse(status_code=404, content={"detail": detail})
    # 前端路由回退到 index.html
    index_path = _FRONTEND_DIST / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        "<h1>KumiPlayer 2.0</h1>"
        "<p>前端未构建。请运行 <code>npm run build</code></p>"
    )
