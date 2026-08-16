"""配置模型与敏感字段脱敏"""

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.core.atomic_json import write_json_atomic
from app.core.credential_store import SECURE_CREDENTIAL_STORE, CredentialStoreError
from app.core.data_lock import DATA_WRITE_LOCK
from app.core.paths import get_data_dir

_logger = logging.getLogger(__name__)

# 测试和诊断工具可临时覆盖；生产环境始终跟随统一数据目录。
CONFIG_FILE: Path | None = None
DEFAULT_BANGUMI_USER_AGENT = "KumiPlayer/2.0 (+https://github.com/kumiplayer/kumiplayer)"
# 需要脱敏的字段
_SENSITIVE_FIELDS = {"tmdb_bearer_token", "deepseek_api_key", "bangumi_access_token", "openlist_password"}
# 需要存入 Windows Credential Manager 的字段（含不脱敏但同样不落配置文件的用户名）
_CREDENTIAL_FIELDS = _SENSITIVE_FIELDS | {"openlist_username"}


@dataclass
class AppConfig:
    """应用配置"""

    # 首次启动。配置文件不存在时为未完成；旧版本配置会在读取时自动迁移。
    setup_completed: bool = False
    setup_version: int = 0

    # 播放器
    mpv_path: str = ""

    # 本地后端服务
    server_port: int = 37821

    # 镜像目录
    mirror_dir: str = ""

    # 来源挂载根目录
    pan115_root: str = ""
    baidu_root: str = ""
    local_root: str = ""
    directory_tree_dir: str = ""

    # OpenList 目录连接（单实例，不按提供商重复配置）
    # openlist_remote_root: OpenList 服务侧远端总根（如 /）
    # openlist_mount_root: 对应的本地总挂载根（如 K:\）
    # openlist_username / openlist_password 存 Windows Credential Manager
    # openlist_cache_ttl_minutes: KumiPlayer 本地浏览缓存时长（分钟）
    # openlist_prefetch_limit: 单层目录浏览的有界预取直接子目录数量上限
    # openlist_routes: 提供商路由表（远端前缀 -> provider），见 providers.OpenListRouteConfig
    openlist_server_url: str = ""
    openlist_remote_root: str = ""
    openlist_mount_root: str = ""
    openlist_username: str = ""
    openlist_password: str = ""
    openlist_cache_ttl_minutes: int = 1440
    openlist_prefetch_limit: int = 12
    openlist_routes: list = field(default_factory=list)

    # TMDB
    tmdb_bearer_token: str = ""
    tmdb_language: str = "zh-CN"
    tmdb_certification_regions: str = "CN,TW,HK,US,JP"
    anilist_enabled: bool = True
    anilist_rate_limit: float = 1.0
    anilist_timeout: int = 10
    scrape_search_timeout: int = 35
    # remote: 刮削只保存图片 URL；local: 同步下载 poster/fanart；auto: local 来源下载，其它来源用 URL。
    artwork_storage_mode: str = "auto"

    # DeepSeek
    deepseek_api_key: str = ""
    tmdb_rate_limit: float = 0.12
    tmdb_max_retries: int = 2
    tmdb_timeout: int = 10

    # 代理（用于访问 TMDB 等外部服务）
    proxy_url: str = ""

    # Bangumi
    bangumi_access_token: str = ""
    bangumi_user_agent: str = DEFAULT_BANGUMI_USER_AGENT

    # 播放
    auto_play_next_episode: bool = True

    # MPV 播放器调节（Anime4K 永久默认值）
    # mode: off|a|b|c|a+a|b+b|c+a ; quality: light|balanced|high
    mpv_anime4k_mode: str = "off"
    mpv_anime4k_quality: str = "balanced"

    # 前端展示偏好
    # poster: 系列作品卡片默认使用竖版封面；fanart: 使用横向海报/横幅图。
    series_card_image_mode: str = "poster"
    # 分类页卡片尺寸，前端滑块读取并持久化。
    poster_size: int = 180

    # 心跳
    heartbeat_enabled: bool = True
    heartbeat_timeout: int = 30
    auto_shutdown_on_heartbeat_timeout: bool = False

    def to_public_dict(self) -> dict:
        """返回脱敏后的配置字典，用于 API 响应"""
        d = asdict(self)
        for key in _SENSITIVE_FIELDS:
            val = d.get(key, "")
            if val:
                if len(val) > 8:
                    d[key] = val[:8] + "..."
                else:
                    d[key] = val[:4] + "..."
        # OpenList 凭据零泄露：用户名与密码绝不进入 API 响应（掩码也不保留，
        # 密码前缀仍属敏感信息），只向客户端暴露是否已配置。
        username = d.pop("openlist_username", "")
        password = d.pop("openlist_password", "")
        d["openlist_configured"] = bool(username and password)
        return d


_cached_config: AppConfig | None = None


def _credential_storage_enabled() -> bool:
    """测试覆盖配置文件时保持隔离；正式 Windows 配置使用凭据管理器。"""
    return CONFIG_FILE is None and SECURE_CREDENTIAL_STORE.available


def openlist_credential_state() -> str:
    """OpenList 凭据三态：``found`` / ``missing`` / ``unavailable``。

    - ``found``：用户名与密码都存在；
    - ``missing``：尚未配置（凭据存储可读但字段为空）；
    - ``unavailable``：凭据存储本身暂时不可读（读取抛错）——调用方
      绝不能把该状态当作「没有凭据」进而触发删除。
    """
    if not _credential_storage_enabled():
        cfg = load_config()
        return "found" if (cfg.openlist_username and cfg.openlist_password) else "missing"
    try:
        username = SECURE_CREDENTIAL_STORE.read("openlist_username")
        password = SECURE_CREDENTIAL_STORE.read("openlist_password")
    except CredentialStoreError:
        return "unavailable"
    return "found" if (username and password) else "missing"

def resolve_openlist_credentials() -> tuple[str, str, str]:
    """解析真实已保存的 OpenList 凭据，返回 ``(username, password, state)``。

    ``state`` 固定为 ``found`` / ``missing`` / ``unavailable``。

    语义（REWORK：Credential Store 恢复后 KEEP SAVED 必须可用）：
    - cached config 已有可靠值（username 与 password 都非空）→ 直接使用；
    - cached config 为空（例如启动时 hydrate 中途 read failure 留下 stale
      blank cache）但 Credential Store 已恢复 → **重新直接读取 secure store**，
      取回真实凭据，不依赖缓存重新 hydrate；
    - 任何 read failure → ``("", "", "unavailable")``——绝不猜 missing、
      绝不触发任何 mutation。
    """
    if not _credential_storage_enabled():
        cfg = load_config()
        if cfg.openlist_username and cfg.openlist_password:
            return cfg.openlist_username, cfg.openlist_password, "found"
        return "", "", "missing"
    try:
        cached = load_config()
        if cached.openlist_username and cached.openlist_password:
            return cached.openlist_username, cached.openlist_password, "found"
        # stale/partial cache：直接回源读取 secure store（恢复后无需重启）
        username = SECURE_CREDENTIAL_STORE.read("openlist_username")
        password = SECURE_CREDENTIAL_STORE.read("openlist_password")
    except CredentialStoreError:
        return "", "", "unavailable"
    if username and password:
        return username, password, "found"
    return "", "", "missing"
def _persist_config_payload_guarded(config: AppConfig) -> None:
    """load_config 迁移路径的持久化保护：store 暂时不可读时跳过迁移。

    迁移（legacy 升级 / UA 清理 / 明文凭据搬移）不是用户主动保存操作；
    store 不可读时直接跳过，保留原文件，不以迁移失败阻断启动或把
    「不可读」当作「清除凭据」。
    """
    try:
        _persist_config_payload(config)
    except CredentialStoreError:
        _logger.warning("凭据存储暂时不可读，跳过配置迁移（不影响启动）")


def _persist_config_payload(config: AppConfig, *, cleared_keys: set[str] | None = None) -> None:
    """持久化配置；凭据写入带补偿回滚（ROOT-7 / OL-3 / REWORK）。

    - 凭据先于 JSON 写入；任一凭据写入失败 → 恢复已写入的旧凭据后抛出；
    - JSON 原子写失败 → 恢复全部本次写入的凭据后抛出；
    - 凭据读取失败（``CredentialStoreError``）→ 直接中止本次保存（0 mutation），
      绝不允许把「存储暂时不可读」当作「清除凭据」；
    - **空值语义 = KEEP**：只有 ``cleared_keys`` 中显式列出的字段才执行
      DELETE；其余空值字段既不写也不删（防止 stale blank cache 误删真实凭据）。
    """
    with DATA_WRITE_LOCK:
        payload = asdict(config)
        written: list[tuple[str, str]] = []  # (key, 旧值)
        cleared = cleared_keys or set()
        if _credential_storage_enabled():
            try:
                for key in _CREDENTIAL_FIELDS:
                    value = str(payload.get(key, "") or "")
                    try:
                        old = SECURE_CREDENTIAL_STORE.read(key)
                    except CredentialStoreError:
                        # 存储暂时不可读：绝不能继续保存——否则要么把「不可读」
                        # 当作「清除凭据」，要么把未脱敏明文写进 config.json。
                        # 直接中止本次保存（0 mutation：store / JSON / 内存全保持）。
                        raise
                    written.append((key, old or ""))
                    if value:
                        if value == old:
                            # 值未变化：跳过（幂等保存不产生多余秘密操作；
                            # 同时保证「只 SET 目标字段」的跨字段隔离）
                            payload[key] = ""
                            continue
                        # SET：非空新值
                        SECURE_CREDENTIAL_STORE.write(key, value)
                    elif key in cleared:
                        # 显式 CLEAR intent：只有调用方明确要求清除才 delete
                        SECURE_CREDENTIAL_STORE.delete(key)
                    else:
                        # KEEP：空值且无显式 clear intent → 不写不删
                        # （防止 hydrate 故障留下的 stale blank cache 误删真实凭据）
                        pass
                    payload[key] = ""
            except Exception:
                _rollback_credentials(written)
                raise
        try:
            write_json_atomic(get_config_file(), payload)
        except Exception:
            if _credential_storage_enabled():
                _rollback_credentials(written)
            raise


def _rollback_credentials(written: list[tuple[str, str]]) -> None:
    """补偿回滚：把本次已写入的凭据恢复为旧值（尽力而为，失败记录高等级错误）。

    注意：只有能读到旧值的字段才会进入 ``written``，因此这里不会误删
    那些「读取失败被跳过」的凭据。
    """
    for key, old in reversed(written):
        try:
            if old:
                SECURE_CREDENTIAL_STORE.write(key, old)
            else:
                SECURE_CREDENTIAL_STORE.delete(key)
        except Exception:
            _logger.critical(
                "凭据补偿回滚失败（key=%s），本机凭据可能处于不一致状态", key
            )


def _hydrate_secure_credentials(config: AppConfig, file_data: dict) -> bool:
    if not _credential_storage_enabled():
        return False
    migrated_plaintext = False
    for key in _CREDENTIAL_FIELDS:
        legacy_value = str(file_data.get(key, "") or "")
        if legacy_value:
            SECURE_CREDENTIAL_STORE.write(key, legacy_value)
            setattr(config, key, legacy_value)
            migrated_plaintext = True
            continue
        stored_value = SECURE_CREDENTIAL_STORE.read(key)
        if stored_value:
            setattr(config, key, stored_value)
    return migrated_plaintext


def get_config_file() -> Path:
    return CONFIG_FILE if CONFIG_FILE is not None else get_data_dir() / "config.json"


def load_config(force_reload: bool = False) -> AppConfig:
    """加载配置文件，支持缓存"""
    global _cached_config
    if not force_reload and _cached_config is not None:
        return _cached_config

    config_file = get_config_file()
    if config_file.exists():
        try:
            with open(config_file, encoding="utf-8") as f:
                data = json.load(f)
            legacy_config = "setup_completed" not in data
            # 只取 AppConfig 中定义的字段
            valid_keys = {f.name for f in AppConfig.__dataclass_fields__.values()}
            filtered = {k: v for k, v in data.items() if k in valid_keys}
            # 嵌套 dataclass 字段反序列化（OpenList 路由表）
            routes = filtered.get("openlist_routes") or []
            if isinstance(routes, list):
                from app.integrations.openlist.providers import OpenListRouteConfig

                filtered["openlist_routes"] = [
                    OpenListRouteConfig(**item) for item in routes if isinstance(item, dict)
                ]
            else:
                filtered["openlist_routes"] = []
            config = AppConfig(**filtered)
            try:
                migrated_credentials = _hydrate_secure_credentials(config, data)
            except CredentialStoreError:
                # 系统凭据服务暂时不可用时保留原文件，不以丢失配置为代价阻断启动。
                migrated_credentials = False
            if legacy_config:
                # 已经在使用 KumiPlayer 的用户不能因为新增引导字段被强制打断。
                config.setup_completed = True
                config.setup_version = 1
                _persist_config_payload_guarded(config)
            if config.bangumi_user_agent != DEFAULT_BANGUMI_USER_AGENT:
                # 旧版允许用户填写该请求头，可能把姓名或昵称写进网络请求。
                # 分发版统一使用应用标识，并在首次读取时清理个人化旧值。
                config.bangumi_user_agent = DEFAULT_BANGUMI_USER_AGENT
                _persist_config_payload_guarded(config)
            elif migrated_credentials:
                _persist_config_payload_guarded(config)
        except (OSError, json.JSONDecodeError):
            config = AppConfig()
    else:
        config = AppConfig()

    if os.environ.get("KUMIPLAYER_AUTO_SHUTDOWN_ON_HEARTBEAT_TIMEOUT") == "1":
        config.auto_shutdown_on_heartbeat_timeout = True
    heartbeat_timeout = os.environ.get("KUMIPLAYER_HEARTBEAT_TIMEOUT")
    if heartbeat_timeout:
        try:
            config.heartbeat_timeout = max(3, int(heartbeat_timeout))
        except ValueError:
            pass

    _cached_config = config
    return config


def save_config(config: AppConfig, *, cleared_keys: set[str] | None = None) -> None:
    """保存配置文件。

    ``cleared_keys``：本次保存中需要显式清除的 secure credential 字段名集合。
    缺省（None）时**空值一律 KEEP**——不写不删，绝不隐式 CLEAR。
    只有调用方明确传入的字段才会执行 DELETE（例如用户主动退出登录）。
    """
    global _cached_config
    _persist_config_payload(config, cleared_keys=cleared_keys)
    _cached_config = config

def invalidate_config_cache() -> None:
    """清除配置缓存"""
    global _cached_config
    _cached_config = None
