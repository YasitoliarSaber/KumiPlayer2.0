"""桌面本地 API 的会话身份校验。"""

import os
import secrets
from collections.abc import Awaitable, Callable

from starlette.datastructures import Headers, QueryParams
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

API_TOKEN_HEADER = "X-KumiPlayer-Token"
API_TOKEN_QUERY = "api_token"
_PUBLIC_HTTP_PATHS = frozenset({"/api/health"})
_QUERY_TOKEN_PATHS = frozenset(
    {
        "/api/assets",
        "/api/assets/remote",
        "/api/assets/thumbnail",
        "/api/integrations/bangumi/avatar",
        "/api/integrations/bangumi/subject-image",
    }
)


def get_expected_api_token() -> str:
    """返回桌面壳为当前进程注入的短期令牌。"""
    return os.environ.get("KUMIPLAYER_API_TOKEN", "").strip()


def token_matches(candidate: str | None) -> bool:
    expected = get_expected_api_token()
    if not expected:
        return True
    if not candidate:
        return False
    return secrets.compare_digest(candidate, expected)


def websocket_token_is_valid(query_string: bytes) -> bool:
    params = QueryParams(query_string.decode("utf-8", errors="ignore"))
    return token_matches(params.get(API_TOKEN_QUERY))


class ApiSessionMiddleware:
    """保护 `/api`；仅图片 GET 可通过查询参数携带令牌。"""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not get_expected_api_token():
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if not path.startswith("/api/") or path in _PUBLIC_HTTP_PATHS:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        if method == "OPTIONS":
            # 浏览器预检不携带实际会话令牌，由内层 CORS 中间件校验来源与请求头。
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        candidate = headers.get(API_TOKEN_HEADER)
        if method == "GET" and path in _QUERY_TOKEN_PATHS and not candidate:
            params = QueryParams(scope.get("query_string", b"").decode("utf-8", errors="ignore"))
            candidate = params.get(API_TOKEN_QUERY)

        if token_matches(candidate):
            await self.app(scope, receive, send)
            return

        response = JSONResponse(
            status_code=401,
            content={"detail": "KumiPlayer 桌面会话验证失败"},
        )
        await response(scope, receive, send)
