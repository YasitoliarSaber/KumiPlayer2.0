"""O3：单个非法条目名不得 abort 整次目录列举（客户端层）。

背景：`validate_entry_name` 的报错文案写着"已跳过"，但 `_list_dir_request` 在循环里
直接抛出 → 一个 `Show: Extra 01.mkv`（含 Windows 非法字符，网盘侧完全合法）就让该
目录所有条目连同整轮扫描失败。正确行为是跳过该条、计数、继续。
"""

from __future__ import annotations

import httpx
from app.integrations.openlist.client import OpenListClient, OpenListValidationError
from app.integrations.openlist.governor import OpenListRequestGovernor


def _json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://test"))


def _client(handler) -> OpenListClient:
    return OpenListClient(
        "https://ol.example.com",
        "user",
        "secret-pass",
        transport=httpx.MockTransport(handler),
        governor=OpenListRequestGovernor(rate_per_second=1000),
    )


def _handler(entries: list[dict]):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return _json_response({"code": 200, "data": {"token": "token"}})
        return _json_response({
            "code": 200,
            "message": "success",
            "data": {"content": entries, "total": len(entries)},
        })

    return handle


def test_illegal_entry_name_is_skipped_instead_of_aborting_the_listing():
    entries = [
        {"name": "Show.S01E01.mkv", "is_dir": False, "size": 100, "modified": 1},
        {"name": "Show: Extra 01.mkv", "is_dir": False, "size": 100, "modified": 1},
        {"name": "Show.S01E02.mkv", "is_dir": False, "size": 100, "modified": 1},
    ]

    page = _client(_handler(entries)).list_dir("/Anime/Show")

    assert [entry.name for entry in page.entries] == ["Show.S01E01.mkv", "Show.S01E02.mkv"]
    assert page.skipped_entries == 1, "被跳过的条目数必须可见，不能静默丢弃"


def test_dangerous_entry_names_are_still_rejected_and_counted():
    entries = [
        {"name": "..", "is_dir": True, "size": 0, "modified": 1},
        {"name": "", "is_dir": False, "size": 100, "modified": 1},
        {"name": "正常条目.mkv", "is_dir": False, "size": 100, "modified": 1},
    ]

    page = _client(_handler(entries)).list_dir("/Anime/Show")

    assert [entry.name for entry in page.entries] == ["正常条目.mkv"]
    assert page.skipped_entries == 2


def test_valid_names_are_unaffected():
    entries = [{"name": f"F{index}.mkv", "is_dir": False, "size": 1, "modified": 1} for index in range(3)]

    page = _client(_handler(entries)).list_dir("/Anime/Show")

    assert len(page.entries) == 3
    assert page.skipped_entries == 0


def test_validate_entry_name_still_raises_for_callers_that_want_a_hard_error():
    """单一校验函数保持原语义：只有目录列举路径负责"跳过并计数"。"""

    for bad in ("", "..", "a:b.mkv", "trailing."):
        try:
            from app.integrations.openlist.client import validate_entry_name

            validate_entry_name(bad)
        except OpenListValidationError:
            continue
        raise AssertionError(f"{bad!r} 应当被拒绝")
