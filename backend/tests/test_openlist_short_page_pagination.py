"""分页终止判据：必须按**实际收到的条目数**判断末页，不能按请求的 per_page。

真因（扫描链路审计 F1，高严重度）：驱动把每页截得比请求值短、而 `total` 又是
正确值时时，`page * per_page >= total` 会在**第一页**就成立 → 后续条目被静默
丢弃，且该目录仍被标成 completed。增量路径随后把"没观察到的基线文件"当成
已删除 pop 掉 → 远端文件没动，库内证据却少了，确认后媒体库丢条目。
"""

from __future__ import annotations

from types import SimpleNamespace

from app.media_v4.sources import incremental, scanner


class _ShortPageClient:
    """每页只返回 page_size 条，但 total 是正确总数（模拟驱动截短页）。"""

    def __init__(self, entries: list[SimpleNamespace], page_size: int) -> None:
        self.entries = entries
        self.page_size = page_size
        self.calls: list[int] = []

    def list_dir(self, _path, page=1, per_page=100, refresh=False):  # noqa: ANN001
        self.calls.append(page)
        start = (page - 1) * self.page_size
        window = self.entries[start : start + self.page_size]
        return SimpleNamespace(entries=window, total=len(self.entries), skipped_entries=0)


def _entry(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"F{index}.mkv",
        is_dir=False,
        size=1,
        modified=1.0,
        remote_path=f"/Anime/F{index}.mkv",
    )


def test_full_scan_paginates_past_a_short_page_with_total_present():
    root = "/Anime"
    client = _ShortPageClient([_entry(index) for index in range(5)], page_size=2)

    _scan_id, evidence = scanner.scan_openlist_directory(
        client, remote_root=root, mapping_root=root, mount_root="", root_id="root-1"
    )

    assert len(evidence) == 5, f"短页不该提前收尾，实际只拿到 {len(evidence)} 条"
    assert client.calls == [1, 2, 3], "应翻到条目收满为止"


def test_incremental_listing_paginates_past_a_short_page_with_total_present():
    client = _ShortPageClient([_entry(index) for index in range(5)], page_size=2)

    entries = incremental._list_all(client, "/Anime", counter=[0], max_entries=100)

    assert len(entries) == 5
    assert client.calls == [1, 2, 3]
