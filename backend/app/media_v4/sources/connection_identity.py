"""从登记的扫描请求解析来源连接，路径或网盘显示名不能决定账号。"""

import json


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
