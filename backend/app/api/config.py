# -*- coding: utf-8 -*-
"""配置 API 端点

GET   /api/config              获取脱敏配置
PATCH /api/config              部分更新配置
POST  /api/config/test/mpv     测试 mpv
POST  /api/config/test/tmdb    测试 TMDB 连通性
POST  /api/config/test/deepseek 测试 DeepSeek 连通性
POST  /api/config/test/media-paths 轻量验证网盘挂载与视频样本
"""

import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import (
    AppConfig,
    load_config,
    save_config,
)
from app.core.runtime import get_default_mirror_dir, get_mpv_config_dir, get_mpv_runtime_dir
from app.media_presets.store import list_presets
from app.playback.mpv_runtime import check_mpv_runtime
from app.raw.store import load_latest_raw_snapshot, load_raw_snapshot
from app.scrape.tmdb_client import TMDBClient
from app.integrations.bangumi import BangumiClient, BangumiError

router = APIRouter(prefix="/api/config", tags=["config"])
_MEDIA_PATH_PROBE_TIMEOUT_SECONDS = 4
_MEDIA_PATH_PROBE_CODE = (
    "import json, os, sys\n"
    "checks = json.loads(sys.stdin.read())\n"
    "states = []\n"
    "for item in checks:\n"
    "    try:\n"
    "        states.append(os.path.isdir(item['path']) if item['kind'] == 'dir' "
    "else os.path.isfile(item['path']))\n"
    "    except OSError:\n"
    "        states.append(False)\n"
    "sys.stdout.write(json.dumps(states))\n"
)


# ============================================================
# 请求模型
# ============================================================

class ConfigPatch(BaseModel):
    """配置更新请求（所有字段可选）"""

    model_config = ConfigDict(extra="forbid")

    mpv_path: Optional[str] = None
    server_port: Optional[int] = Field(default=None, ge=1, le=65535)
    mirror_dir: Optional[str] = None
    pan115_root: Optional[str] = None
    baidu_root: Optional[str] = None
    local_root: Optional[str] = None
    directory_tree_dir: Optional[str] = None
    openlist_server_url: Optional[str] = None
    openlist_remote_root: Optional[str] = None
    openlist_mount_root: Optional[str] = None
    openlist_username: Optional[str] = None
    openlist_password: Optional[str] = None
    openlist_cache_ttl_minutes: Optional[int] = None
    openlist_prefetch_limit: Optional[int] = None
    tmdb_bearer_token: Optional[str] = None
    tmdb_language: Optional[str] = None
    tmdb_certification_regions: Optional[str] = None
    anilist_enabled: Optional[bool] = None
    anilist_rate_limit: Optional[float] = None
    anilist_timeout: Optional[int] = None
    deepseek_api_key: Optional[str] = None
    tmdb_rate_limit: Optional[float] = None
    tmdb_max_retries: Optional[int] = None
    tmdb_timeout: Optional[int] = None
    bangumi_access_token: Optional[str] = None
    bangumi_user_agent: Optional[str] = None
    auto_play_next_episode: Optional[bool] = None
    mpv_anime4k_mode: Optional[str] = None
    mpv_anime4k_quality: Optional[str] = None
    series_card_image_mode: Optional[str] = None
    poster_size: Optional[int] = None
    heartbeat_enabled: Optional[bool] = None
    heartbeat_timeout: Optional[int] = None
    proxy_url: Optional[str] = None
    artwork_storage_mode: Optional[str] = None
    auto_shutdown_on_heartbeat_timeout: Optional[bool] = None


class SetupCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # mpv_path 仅保留数据兼容；首次引导不再要求用户选择外部 MPV，播放默认使用内置干净 MPV。
    mpv_path: str = ""
    mirror_dir: str
    pan115_root: str = ""
    baidu_root: str = ""
    local_root: str = ""
    directory_tree_dir: str = ""
    tmdb_bearer_token: str = ""
    bangumi_access_token: str = ""


class MpvPathRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mpv_path: str


def _probe_media_path_states(
    root: Path,
    sample_paths: list[Path],
) -> tuple[list[bool], bool]:
    """在可终止子进程中探测挂载路径，避免离线盘符无限阻塞请求线程。"""
    checks = [
        {"kind": "dir", "path": str(root)},
        *({"kind": "file", "path": str(path)} for path in sample_paths),
    ]
    run_kwargs = {}
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if create_no_window:
        run_kwargs["creationflags"] = create_no_window
    try:
        result = subprocess.run(
            [sys.executable, "-c", _MEDIA_PATH_PROBE_CODE],
            input=json.dumps(checks, ensure_ascii=True),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_MEDIA_PATH_PROBE_TIMEOUT_SECONDS,
            **run_kwargs,
        )
        if result.returncode != 0:
            return [False] * len(checks), False
        states = json.loads(result.stdout)
        if not isinstance(states, list) or len(states) != len(checks):
            return [False] * len(checks), False
        return [bool(state) for state in states], False
    except subprocess.TimeoutExpired:
        return [False] * len(checks), True
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return [False] * len(checks), False


# ============================================================
# 端点
# ============================================================

@router.get("")
def get_config():
    """获取脱敏后的配置"""
    config = load_config()
    public = config.to_public_dict()
    if not public["setup_completed"] and not public["mirror_dir"]:
        public["mirror_dir"] = str(get_default_mirror_dir())
    return public


def _ensure_writable_directory(path: Path) -> None:
    """创建目录并执行一次无残留的写入验证。"""
    probe_path: Optional[Path] = None
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".kumiplayer-write-test-",
            dir=path,
            delete=False,
        ) as probe:
            probe.write(b"ok")
            probe_path = Path(probe.name)
    except OSError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"镜像目录无法创建或不可写，请选择其他文件夹：{exc}",
        ) from exc
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError:
                pass


@router.patch("")
def patch_config(req: ConfigPatch):
    """部分更新配置

    只更新传入的非 None 字段。
    敏感字段传入空字符串表示不修改。
    """
    config = load_config()
    patch_dict = req.model_dump(exclude_none=True)

    if not patch_dict:
        raise HTTPException(status_code=400, detail="未传入任何字段")

    # 校验字段是否属于 AppConfig
    valid_keys = {f.name for f in AppConfig.__dataclass_fields__.values()}
    for key in patch_dict:
        if key not in valid_keys:
            raise HTTPException(status_code=400, detail=f"未知配置字段: {key}")

    # Anime4K 枚举校验：非法 mode/quality 拒绝，不传给 MPV
    if "mpv_anime4k_mode" in patch_dict and patch_dict["mpv_anime4k_mode"] not in {"off", "a", "b", "c", "a+a", "b+b", "c+a"}:
        raise HTTPException(status_code=400, detail="mpv_anime4k_mode 只能是 off|a|b|c|a+a|b+b|c+a")
    if "mpv_anime4k_quality" in patch_dict and patch_dict["mpv_anime4k_quality"] not in {"light", "balanced", "high"}:
        raise HTTPException(status_code=400, detail="mpv_anime4k_quality 只能是 light|balanced|high")

    # 敏感/凭据字段：空字符串表示不修改
    from app.core.config import _CREDENTIAL_FIELDS
    for key in _CREDENTIAL_FIELDS:
        if key in patch_dict and patch_dict[key] == "":
            # 空字符串表示不修改，跳过
            continue
        if key in patch_dict:
            setattr(config, key, patch_dict[key])

    # 非敏感字段直接更新
    for key, value in patch_dict.items():
        if key in _CREDENTIAL_FIELDS:
            continue
        setattr(config, key, value)

    save_config(config)

    # Anime4K 默认值变更时，向运行中的 MPV 同步“下一视频默认值”（不改变当前视频）
    if "mpv_anime4k_mode" in patch_dict or "mpv_anime4k_quality" in patch_dict:
        _sync_anime4k_default_to_active_mpv(config)

    return config.to_public_dict()


def _sync_anime4k_default_to_active_mpv(config: AppConfig) -> None:
    """把新的 Anime4K 永久默认值通过 IPC 同步给活动播放会话。"""
    try:
        from app.playback.mpv_ipc import send_mpv_script_message
        from app.playback.service import get_playback_manager
        from app.playback.models import PlaybackSession

        manager = get_playback_manager()
        session = getattr(manager, "_current_session", None)
        if not isinstance(session, PlaybackSession) or not session.ipc_server:
            return
        send_mpv_script_message(
            session.ipc_server,
            "kumiplayer_anime4k",
            "set-default",
            (config.mpv_anime4k_mode or "off", config.mpv_anime4k_quality or "balanced"),
        )
    except Exception:
        # 配置保存不应因 IPC 失败而报错：当前视频保持不变，下一视频仍用新默认值
        pass


def _inspect_mpv(mpv_path: str = "", *, require_file: bool = False) -> dict:
    """返回 KumiPlayer 内置 MPV 的检查结果。

    播放默认使用内置干净 MPV；mpv_path 参数仅保留兼容接口，不再作为播放依据。
    require_file 兼容旧接口语义，但内置运行时缺失时同样返回失败。
    """
    status = check_mpv_runtime(verify_files=True)
    integration = get_kumiplayer_mpv_integration()
    if not status["available"]:
        return {
            "ok": False,
            "message": "KumiPlayer 内置 MPV 未找到或无法启动",
            **integration,
        }
    if status.get("files_valid") is False and status.get("manifest_valid"):
        return {
            "ok": False,
            "message": status["message"] or "内置 MPV 运行文件校验失败",
            **integration,
        }
    version = status["version"] or "unknown"
    return {
        "ok": True,
        "message": f"内置播放器已就绪: {version}",
        "version": version,
        **integration,
    }


def get_kumiplayer_mpv_integration() -> dict:
    """返回非敏感的 KumiPlayer 内置 MPV 功能插件状态。"""
    from app.playback.mpv import get_kumiplayer_mpv_integration as _integration

    return _integration()


@router.get("/setup/status")
def get_setup_status():
    config = load_config()
    runtime_status = check_mpv_runtime(verify_files=False)
    return {
        "setup_completed": config.setup_completed,
        "setup_version": config.setup_version,
        "mpv_configured": runtime_status["available"],
        "mirror_configured": bool(config.mirror_dir),
        "source_configured": any((
            config.pan115_root,
            config.baidu_root,
            config.local_root,
            config.openlist_server_url and config.openlist_mount_root,
        )),
    }


@router.post("/test/mpv-path")
def test_mpv_path(req: MpvPathRequest):
    """验证尚未保存的 MPV 路径，供首次启动引导使用。"""
    return _inspect_mpv(req.mpv_path.strip(), require_file=True)


@router.post("/setup/complete")
def complete_setup(req: SetupCompleteRequest):
    # 首次引导不再要求用户选择外部 MPV；播放默认使用内置干净 MPV，缺失时明确报错。
    runtime_status = check_mpv_runtime(verify_files=True)
    if not runtime_status["available"]:
        raise HTTPException(status_code=400, detail="KumiPlayer 内置播放器未就绪，请修复后重试")
    if not runtime_status["manifest_valid"]:
        raise HTTPException(status_code=400, detail="KumiPlayer 内置 MPV 运行时清单缺失或非法，无法完成设置")
    if not runtime_status["files_valid"]:
        raise HTTPException(status_code=400, detail="KumiPlayer 内置 MPV 运行文件校验失败，无法完成设置")
    if not runtime_status["configuration_available"]:
        raise HTTPException(status_code=400, detail="KumiPlayer 播放配置不完整，无法完成设置")

    mirror_dir = Path(req.mirror_dir).expanduser()

    source_roots = {
        "115 网盘": req.pan115_root.strip(),
        "百度网盘": req.baidu_root.strip(),
        "本地媒体": req.local_root.strip(),
    }
    configured_sources = {name: value for name, value in source_roots.items() if value}
    if not configured_sources:
        raise HTTPException(status_code=400, detail="请至少配置一个可访问的媒体来源")
    with ThreadPoolExecutor(max_workers=len(configured_sources)) as executor:
        source_states = list(executor.map(
            lambda item: (item[0], _probe_media_path_states(Path(item[1]).expanduser(), [])[0][0]),
            configured_sources.items(),
        ))
    invalid_sources = [name for name, available in source_states if not available]
    if invalid_sources:
        raise HTTPException(status_code=400, detail=f"媒体来源不可访问: {', '.join(invalid_sources)}")

    directory_tree_dir = req.directory_tree_dir.strip()
    if (
        directory_tree_dir
        and not _probe_media_path_states(Path(directory_tree_dir).expanduser(), [])[0][0]
    ):
        raise HTTPException(status_code=400, detail="目录树文件目录不存在或不可访问")

    config = load_config()
    # mpv_path 仅保留数据兼容；旧值不删除，但播放不再以它为依据。
    requested_mpv_path = req.mpv_path.strip()
    if requested_mpv_path:
        config.mpv_path = requested_mpv_path
    config.mirror_dir = str(mirror_dir)
    config.pan115_root = req.pan115_root.strip()
    config.baidu_root = req.baidu_root.strip()
    config.local_root = req.local_root.strip()
    config.directory_tree_dir = directory_tree_dir
    tmdb_bearer_token = req.tmdb_bearer_token.strip()
    if tmdb_bearer_token:
        if len(tmdb_bearer_token) == 32 and all(char in "0123456789abcdefABCDEF" for char in tmdb_bearer_token):
            raise HTTPException(
                status_code=400,
                detail="你粘贴的是 TMDB API 密钥；KumiPlayer 需要页面上方较长的 API 读取访问令牌。",
            )
        try:
            with TMDBClient(bearer_token=tmdb_bearer_token) as client:
                token_ok, token_message = client.test_authentication()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"TMDB 验证异常: {exc}") from exc
        if not token_ok:
            raise HTTPException(status_code=400, detail=f"TMDB Token 验证失败: {token_message}")
        config.tmdb_bearer_token = tmdb_bearer_token
    bangumi_access_token = req.bangumi_access_token.strip()
    if bangumi_access_token:
        try:
            BangumiClient(access_token=bangumi_access_token, timeout=12.0).get_me()
        except BangumiError as exc:
            raise HTTPException(status_code=400, detail=f"Bangumi 个人访问令牌验证失败: {exc}") from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Bangumi 验证异常: {exc}") from exc
        config.bangumi_access_token = bangumi_access_token
    _ensure_writable_directory(mirror_dir)
    config.setup_completed = True
    config.setup_version = 1
    save_config(config)
    return config.to_public_dict()


# ============================================================
# 测试端点
# ============================================================

@router.get("/mpv-runtime")
def get_mpv_runtime_status():
    """返回 KumiPlayer 内置 MPV 的统一健康检查结果，供首次引导与设置页共用。

    使用完整校验（verify_files=True）确保首次引导和设置页能获得准确的运行文件状态。
    """
    status = check_mpv_runtime(verify_files=True)
    status["runtime_dir"] = str(get_mpv_runtime_dir())
    status["config_dir"] = str(get_mpv_config_dir())
    return status


@router.post("/mpv-runtime/open-config")
def open_mpv_config_dir():
    """在资源管理器中打开 KumiPlayer 内置 MPV 的配置目录（快捷设置入口）。

    只打开 KumiPlayer 自有的 mpv 配置目录（portable_config），不接受前端传入的
    任意路径，避免把"打开文件夹"能力暴露成任意路径打开接口。
    """
    config_dir = get_mpv_config_dir()
    if not config_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"配置目录不存在: {config_dir}")
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", str(config_dir)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(config_dir)])
        else:
            subprocess.Popen(["xdg-open", str(config_dir)])
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"打开配置目录失败: {e}") from e
    return {"ok": True, "config_dir": str(config_dir)}


@router.post("/test/mpv")
def test_mpv():
    """测试 KumiPlayer 内置 MPV 是否可用"""
    return _inspect_mpv()


@router.post("/test/tmdb")
def test_tmdb():
    """测试 TMDB API 连通性"""
    config = load_config()
    token = config.tmdb_bearer_token

    if not token:
        return {"ok": False, "message": "未配置 tmdb_bearer_token"}

    try:
        with TMDBClient(bearer_token=token) as client:
            ok, message = client.test_authentication()
        return {"ok": ok, "message": message}
    except Exception as e:
        return {"ok": False, "message": f"TMDB 测试异常: {e}"}


@router.post("/test/deepseek")
def test_deepseek():
    """测试 DeepSeek API 连通性"""
    config = load_config()
    api_key = config.deepseek_api_key

    if not api_key:
        return {"ok": False, "message": "未配置 deepseek_api_key"}

    try:
        import httpx
        # 轻量测试：调用 models 列表接口
        resp = httpx.get(
            "https://api.deepseek.com/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "accept": "application/json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return {"ok": True, "message": "DeepSeek API 连通成功"}
        elif resp.status_code == 401:
            return {"ok": False, "message": "DeepSeek 认证失败: API key 无效"}
        else:
            return {"ok": False, "message": f"DeepSeek 返回 {resp.status_code}: {resp.text[:200]}"}
    except httpx.TimeoutException:
        return {"ok": False, "message": "DeepSeek 请求超时（10秒）"}
    except Exception as e:
        return {"ok": False, "message": f"DeepSeek 测试异常: {e}"}


@router.post("/test/media-paths")
def test_media_paths():
    """轻量验证 115、百度与 OpenList 挂载路径，不打开或读取视频内容。"""
    config = load_config()
    roots = {
        "pan115": config.pan115_root,
        "baidu": config.baidu_root,
        "openlist": config.openlist_mount_root,
    }
    presets = list_presets()

    def validate_source(source: str, configured_root: str) -> dict:
        if not configured_root:
            return {
                "source": source,
                "ok": False,
                "status": "unavailable",
                "configured_root": "",
                "resolved_root": "",
                "scope_name": "",
                "checked_count": 0,
                "existing_count": 0,
                "example_path": "",
                "message": "尚未配置挂载目录",
            }
        snapshots = [
            snapshot
            for preset in presets
            if preset.source == source and preset.current_snapshot_id
            for snapshot in [load_raw_snapshot(preset.current_snapshot_id)]
            if snapshot is not None
        ]
        if not snapshots:
            latest = load_latest_raw_snapshot(source)
            snapshots = [latest] if latest is not None else []

        root = Path(configured_root).expanduser()
        sample_paths = []
        resolved_roots = []
        for snapshot in snapshots:
            resolved_roots.append(snapshot.source_root)
            sample = next((item for item in snapshot.files if item.resource_hint == "video"), None)
            if sample is not None:
                sample_paths.append(Path(sample.real_path))
        states, probe_timed_out = _probe_media_path_states(root, sample_paths)
        root_ok = states[0]
        existing_count = sum(states[1:])
        samples_ok = not sample_paths or existing_count > 0
        ok = root_ok and samples_ok
        return {
                "source": source,
                "ok": ok,
                "status": "verified" if ok else ("unavailable" if not root_ok else "mismatch"),
                "configured_root": configured_root,
                "resolved_root": resolved_roots[0] if resolved_roots else configured_root,
                "scope_name": "",
                "checked_count": len(sample_paths),
                "existing_count": existing_count,
                "example_path": str(sample_paths[0]) if sample_paths else "",
                "message": (
                    f"挂载路径检测超过 {_MEDIA_PATH_PROBE_TIMEOUT_SECONDS} 秒，已停止等待"
                    if probe_timed_out
                    else
                    f"挂载正常，当前媒体库命中 {existing_count}/{len(sample_paths)} 组代表视频"
                    if ok and sample_paths
                    else "挂载目录可访问，导入目录树后会继续验证视频路径" if ok
                    else "挂载目录不可访问" if not root_ok
                    else "当前媒体库的视频样本均未命中，请检查挂载是否在线或路径是否变更"
                ),
            }

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda item: validate_source(*item), roots.items()))
    return {"ok": all(item["ok"] for item in results), "sources": results}
