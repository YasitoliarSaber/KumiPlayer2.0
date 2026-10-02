"""从登记的扫描请求解析来源连接，路径或网盘显示名不能决定账号。"""

import hashlib
import json
from urllib.parse import urlsplit, urlunsplit


def openlist_account_namespace(server_url: str, username: str) -> str:
    """只散列服务器与账户；不包含密码、显示名或本地挂载位置。"""
    from app.integrations.openlist.client import normalize_openlist_server_url

    parts = urlsplit(normalize_openlist_server_url(server_url))
    scheme = parts.scheme.casefold()
    host = (parts.hostname or "").casefold()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and (scheme, port) not in {("https", 443), ("http", 80)}:
        host = f"{host}:{port}"
    endpoint = urlunsplit((scheme, host, parts.path, "", ""))
    # URL 路径与账户名可能区分大小写，不能随主机名一起 casefold。
    raw = "\x1f".join((endpoint, username.strip()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def resolve_openlist_root_id(
    database, server_url: str, username: str, remote_root: str, *,
    connection_id: str, current_fingerprint: str = "",
) -> str:
    """旧连接根仅在账户证据一致时复用；不迁移或改写历史媒体事实。"""
    from app.integrations.openlist.client import normalize_remote_path
    from app.media_v4.sources.scanner import openlist_root_id

    current = openlist_root_id(server_url, username, remote_root, connection_id=connection_id)
    if not connection_id or connection_id == "legacy":
        return current
    old_raw = "\x1f".join((connection_id, normalize_remote_path(remote_root)))
    old_root = "root_" + hashlib.sha256(old_raw.encode("utf-8")).hexdigest()[:24]
    with database.connect() as conn:
        if conn.execute("SELECT 1 FROM source_roots WHERE root_id = ?", (current,)).fetchone():
            return current
        row = conn.execute(
            "SELECT req.request_json FROM source_scans scan "
            "JOIN source_scan_requests req ON req.scan_id = scan.scan_id "
            "WHERE scan.root_id = ? ORDER BY scan.generation DESC, scan.started_at DESC LIMIT 1",
            (old_root,),
        ).fetchone()
    if row is None:
        return current
    try:
        request = json.loads(row["request_json"] or "{}")
    except (TypeError, ValueError):
        return current
    if not isinstance(request, dict) or request.get("connection_id") != connection_id:
        return current
    namespace = request.get("account_namespace")
    if namespace:
        matches = namespace == openlist_account_namespace(server_url, username)
    else:
        # 旧指纹也包含映射根，映射已变时无法证明账户一致，保守建立新基线。
        matches = bool(current_fingerprint) and request.get("connection_fingerprint") == current_fingerprint
    return old_root if matches else current


def root_connection_id(database, root_id: str) -> str:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT req.request_json FROM source_scans scan "
            "JOIN source_scan_requests req ON req.scan_id = scan.scan_id "
            "WHERE scan.root_id = ? ORDER BY scan.generation DESC, scan.started_at DESC",
            (root_id,),
        ).fetchall()
    for row in rows:
        try:
            request = json.loads(row["request_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(request, dict) and request.get("connection_id"):
            return str(request["connection_id"])
    # 新连接的首个任务必写 ID；此前无 ID 的历史任务只能属于旧连接。
    return "legacy"
