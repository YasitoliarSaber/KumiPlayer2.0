"""稳定的 OpenList 配置身份；旧单连接保留原凭据键与来源身份。"""

import copy
import hashlib
import re
from dataclasses import asdict, dataclass, field, fields

LEGACY_CONNECTION_ID = "legacy"


@dataclass
class OpenListConnectionConfig:
    connection_id: str
    name: str = "OpenList"
    connector_type: str = "openlist"
    openlist_server_url: str = ""
    openlist_remote_root: str = "/"
    openlist_mount_root: str = ""
    openlist_username: str = ""
    openlist_password: str = ""
    openlist_cache_ttl_minutes: int = 1440
    openlist_prefetch_limit: int = 12
    openlist_routes: list = field(default_factory=list)

    def __post_init__(self):
        if self.connector_type != "openlist":
            raise ValueError("此版本仅支持 OpenList 连接")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.connection_id) or self.connection_id == LEGACY_CONNECTION_ID:
            raise ValueError("无效的 OpenList 连接标识")


CONNECTION_FIELDS = tuple(f.name for f in fields(OpenListConnectionConfig) if f.name.startswith("openlist_"))


def connection_id(config) -> str:
    return getattr(config, "_openlist_connection_id", LEGACY_CONNECTION_ID)


def connection_fingerprint(config) -> str:
    from app.core.config import resolve_openlist_credentials
    from app.integrations.openlist.client import normalize_openlist_server_url, normalize_remote_path

    user, _password, state = resolve_openlist_credentials(connection_id(config))
    if state != "found":
        raise ValueError("此连接的登录信息暂时不可用")
    raw = "\x1f".join((normalize_openlist_server_url(config.openlist_server_url), user,
                       normalize_remote_path(config.openlist_remote_root or "/"),
                       config.openlist_mount_root.strip().replace("\\", "/").casefold()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def select_connection(config, identity: str = ""):
    identity = identity or LEGACY_CONNECTION_ID
    selected = copy.deepcopy(config)
    if identity != LEGACY_CONNECTION_ID:
        record = next((c for c in config.openlist_connections if c.connection_id == identity), None)
        if record is None:
            raise ValueError("OpenList 连接不存在，请重新选择连接")
        for key in CONNECTION_FIELDS:
            setattr(selected, key, copy.deepcopy(getattr(record, key)))
    selected._openlist_connection_id = identity
    return selected


def public_connections(config) -> list[dict]:
    from app.core.config import _mask_username

    # 即使旧连接尚未配置，仍提供唯一的旧编辑入口，兼容初次设置。
    records = [dict(connection_id=LEGACY_CONNECTION_ID, name=config.openlist_connection_name, connector_type="openlist",
                    **{key: copy.deepcopy(getattr(config, key)) for key in CONNECTION_FIELDS})]
    records += [asdict(c) for c in config.openlist_connections]
    for record in records:
        username = record.pop("openlist_username", "")
        password = record.pop("openlist_password", "")
        record["openlist_configured"] = bool(username and password)
        record["openlist_username_masked"] = _mask_username(username)
        record["openlist_routes"] = [asdict(r) if hasattr(r, "__dataclass_fields__") else r
                                    for r in record["openlist_routes"]]
    return records


def credential_bindings(config):
    for record in config.openlist_connections:
        for attr in ("openlist_username", "openlist_password"):
            yield record, attr, f"openlist:{record.connection_id}:{attr.removeprefix('openlist_')}"


def save_selected_connection(selected, *, name: str | None = None):
    from app.core.config import AppConfig, load_config, save_config
    from app.core.data_lock import DATA_WRITE_LOCK

    with DATA_WRITE_LOCK:
        # 只合并此连接字段，避免把其它设置的陈旧副本写回。
        current = copy.deepcopy(load_config())
        identity = connection_id(selected)
        target: AppConfig | OpenListConnectionConfig
        if identity == LEGACY_CONNECTION_ID:
            target = current
            if name is not None:
                current.openlist_connection_name = name
        else:
            record = next((c for c in current.openlist_connections if c.connection_id == identity), None)
            if record is None:
                raise ValueError("OpenList 连接不存在")
            if name is not None:
                record.name = name
            target = record
        for key in CONNECTION_FIELDS:
            setattr(target, key, copy.deepcopy(getattr(selected, key)))
        save_config(current)
