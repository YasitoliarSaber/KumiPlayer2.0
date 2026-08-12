"""UI-REMOVE-01 回归测试：备份与恢复/媒体库快照功能已完整移除。

覆盖两个层面：
1. /api/library/snapshots 系列路由已从 FastAPI 应用移除，请求应返回 404。
2. backend/app 全部 Python 源码中不应再出现快照功能标识符。
   （注意只匹配快照功能标识符本身，不匹配 raw_snapshots / RawSnapshot 等
   同名不同职责的导入快照结构。）
"""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

_BACKEND_APP_DIR = Path(__file__).parent.parent / "app"
_FORBIDDEN_IDENTIFIERS = (
    "create_library_snapshot",
    "restore_library_snapshot",
    "delete_library_snapshot",
    "list_library_snapshots",
    "library_snapshots",
)


def test_library_snapshot_routes_are_removed():
    """已删除的 /api/library/snapshots 系列路由应返回 404。

    未设置 KUMIPLAYER_API_TOKEN 时 api_security 中间件放行，可直接用
    TestClient 验证路由不存在（FastAPI 未匹配路由时返回 404）。
    """
    client = TestClient(app)
    cases = [
        ("GET", "/api/library/snapshots"),
        ("POST", "/api/library/snapshots"),
        ("POST", "/api/library/snapshots/restore"),
        ("DELETE", "/api/library/snapshots/any-id"),
    ]
    for method, path in cases:
        response = client.request(method, path)
        assert response.status_code == 404, (
            f"{method} {path} 应返回 404，实际 {response.status_code}: {response.text}"
        )


def test_library_snapshot_identifiers_absent_from_backend_sources():
    """backend/app 全部 Python 源码中不应再出现快照功能标识符。"""
    offenders = []
    for source_path in sorted(_BACKEND_APP_DIR.rglob("*.py")):
        content = source_path.read_text(encoding="utf-8")
        for identifier in _FORBIDDEN_IDENTIFIERS:
            if re.search(rf"\b{re.escape(identifier)}\b", content):
                offenders.append((str(source_path.relative_to(_BACKEND_APP_DIR.parent)), identifier))
    assert not offenders, f"backend/app 仍存在快照功能标识符引用: {offenders}"
