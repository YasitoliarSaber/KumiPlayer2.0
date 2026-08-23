"""KumiPlayer 2.0 FastAPI 应用入口"""

import asyncio
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
from app.api.library_v4 import router as library_router
from app.api.media_v4 import router as media_v4_router
from app.api.openlist_v4 import router as openlist_router
from app.api.playback_v4 import router as playback_router
from app.api.system import router as system_router
from app.api.tasks_v4 import router as tasks_router
from app.api.tracking_v4 import router as tracking_router
from app.core.api_security import ApiSessionMiddleware
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.runtime import initialize_runtime


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：初始化 V4 唯一媒体状态源并启动心跳监控。"""
    from app.system.heartbeat import get_heartbeat_manager

    database = initialize_runtime()
    stop_jobs = asyncio.Event()

    async def run_v4_jobs() -> None:
        runner = V4JobRunner(database)
        while not stop_jobs.is_set():
            try:
                await asyncio.to_thread(runner.process_available)
            except Exception:
                # 单个任务会在自己的 handler 中记录失败；worker 继续消费后续任务。
                pass
            try:
                await asyncio.wait_for(stop_jobs.wait(), timeout=0.25)
            except TimeoutError:
                continue

    job_worker = asyncio.create_task(run_v4_jobs())
    manager = get_heartbeat_manager()
    manager.start_monitor()
    try:
        yield
    finally:
        stop_jobs.set()
        await job_worker
        manager.stop_monitor()


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

# V4 是媒体导入、执行和投影的唯一运行时入口。
app.include_router(library_router)
app.include_router(heartbeat_router)
app.include_router(config_router)
app.include_router(bangumi_router)
app.include_router(error_log_router)
app.include_router(assets_router)
app.include_router(media_v4_router)
app.include_router(playback_router)
app.include_router(tracking_router)
app.include_router(tasks_router)
app.include_router(openlist_router)
app.include_router(system_router)


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
