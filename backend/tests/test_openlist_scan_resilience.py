"""扫描对上游瞬时故障的韧性：目录级重试 + 失败时保留进度可续扫。

实测故障：一轮扫描已经走完 763 个目录，被**一次**上游 5xx（夸克驱动偶发）直接
判成 failed；证据与 frontier 都在库里，但界面只能让用户从头再来。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from app.integrations.openlist.client import OpenListClient
from app.integrations.openlist.governor import OpenListRequestGovernor
from app.integrations.openlist.models import (
    OpenListNotFoundError,
    OpenListRiskControlError,
    OpenListScanLimitExceeded,
    OpenListServerError,
)
from app.media_v4.sources import incremental, scanner
from app.media_v4.sources.source_scan_runner import _resumable_transient_message


class _FlakyClient:
    """第一次抛指定异常，之后返回一页结果。"""

    def __init__(self, error: Exception, *, fail_times: int = 1) -> None:
        self.error = error
        self.fail_times = fail_times
        self.calls = 0

    def list_dir(self, _remote_path, page=1, per_page=100, refresh=False):  # noqa: ANN001
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        return SimpleNamespace(
            entries=[SimpleNamespace(remote_path="/a/b.mkv", is_dir=False, name="b.mkv")],
            total=1,
            skipped_entries=0,
        )


def _collect(client) -> list:
    return incremental._list_all(client, "/a", counter=[0], max_entries=1000)


def test_directory_listing_retries_transient_upstream_error(monkeypatch):
    monkeypatch.setattr(scanner, "LIST_RETRY_DELAYS", (0.0,))
    client = _FlakyClient(OpenListServerError())

    entries = _collect(client)

    assert len(entries) == 1, "重试成功后就该正常返回"
    assert client.calls == 2, "瞬时 5xx 必须原地重试一次"


def test_directory_listing_gives_up_after_bounded_retries(monkeypatch):
    monkeypatch.setattr(scanner, "LIST_RETRY_DELAYS", (0.0, 0.0))
    client = _FlakyClient(OpenListServerError(), fail_times=99)

    with pytest.raises(OpenListServerError):
        _collect(client)

    assert client.calls == 3, "重试次数必须有上限（1 次 + 2 次重试）"


def test_directory_listing_never_retries_risk_control(monkeypatch):
    """反证：远端风控重试只会加剧，必须立即抛出。"""

    monkeypatch.setattr(scanner, "LIST_RETRY_DELAYS", (0.0,))
    client = _FlakyClient(OpenListRiskControlError())

    with pytest.raises(OpenListRiskControlError):
        _collect(client)

    assert client.calls == 1, "风控不得重试"


def test_scan_limit_still_trips_immediately(monkeypatch):
    """扫描上限与重试无关：列目录成功后仍要按上限终止。"""

    monkeypatch.setattr(scanner, "LIST_RETRY_DELAYS", (0.0,))
    client = _FlakyClient(OpenListServerError(), fail_times=0)

    with pytest.raises(OpenListScanLimitExceeded):
        incremental._list_all(client, "/a", counter=[10_000], max_entries=10)

    assert client.calls == 1


@pytest.mark.parametrize(
    "error",
    [OpenListServerError(), OpenListRiskControlError()],
)
def test_resumable_message_only_for_transient_upstream(error):
    message = _resumable_transient_message(error)

    if isinstance(error, OpenListServerError):
        assert message is not None and "继续扫描" in message
    else:
        assert message is None, "风控是可续扫反例：它属于安全事件，按失败处理"


def test_resumable_message_ignores_non_openlist_errors():
    assert _resumable_transient_message(ValueError("boom")) is None


# --- 真因：幽灵目录（父目录仍列出它，上游已不存在） ---------------------------


def _make_client(handler, **kwargs) -> OpenListClient:
    kwargs.setdefault("governor", OpenListRequestGovernor(rate_per_second=1000))
    return OpenListClient(
        "https://ol.example.com",
        "user",
        "secret-pass",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_missing_object_body_code_is_path_not_found_and_not_retried():
    """实测原始响应：HTTP 200 + code=500 + 'failed get dir: object not found'。

    这是**路径级**错误（幽灵条目），不是服务端故障：归 not_found、不重试。
    """

    calls: list[int] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            200,
            json={"code": 500, "message": "failed get objs: failed get dir: object not found"},
        )

    client = _make_client(handler, max_attempts=3)

    with pytest.raises(OpenListNotFoundError):
        client.list_dir("/夸克网盘/动画/4k 京阿尼合集/冰菓")

    assert len(calls) == 1, "路径不存在是确定性结果，重试没有意义"


def test_scan_skips_ghost_directory_and_records_it():
    """扫描遇到幽灵目录：跳过并记录，绝不让一次路径问题打死整轮。"""

    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.sources.incremental import scan_openlist_incremental

    root = "/root"

    class _GhostClient:
        def list_dir(self, path, page=1, per_page=100, refresh=False):  # noqa: ANN001
            if path.rstrip("/").endswith("/ghost"):
                raise OpenListNotFoundError()
            if path.rstrip("/") == root:
                return SimpleNamespace(
                    entries=[
                        SimpleNamespace(
                            name="a.mkv", is_dir=False, size=1, modified=1.0, remote_path=f"{root}/a.mkv"
                        ),
                        SimpleNamespace(
                            name="ghost", is_dir=True, size=None, modified=1.0, remote_path=f"{root}/ghost"
                        ),
                    ],
                    total=2,
                    skipped_entries=0,
                )
            return SimpleNamespace(entries=[], total=0, skipped_entries=0)

    baseline = [
        SourceEvidence(
            evidence_id="ev-1",
            scan_id="scan-old",
            root_id="root-1",
            source_key="a.mkv",
            relative_path="a.mkv",
            entry_kind="video",
            provider="local",
            source_locator="local://a",
            fingerprint="fp-1",
        )
    ]
    state = {
        "version": 1,
        "root_id": "root-1",
        "remote_root": root,
        "remote_verified": True,
        "directories": {
            "": {
                "modified": None,
                "last_verified_at": 0,
                "verification_state": "verified",
                "listing_hash": "",
            }
        },
    }

    _scan_id, files, _next_state, stats = scan_openlist_incremental(
        _GhostClient(),
        baseline=baseline,
        state=state,
        mapping_root="",
        mount_root="",
    )

    assert stats["missing_directory_count"] == 1
    assert stats["missing_directories"] == [f"{root}/ghost"]
    assert [item.relative_path for item in files] == ["a.mkv"], "其余文件照常入库"


# --- 完整扫描（用户实际失败的路径：763 个目录） ------------------------------


class _FullScanGhostClient:
    """根目录里有一个列表可见、但上游已不存在的幽灵目录。"""

    def __init__(self, root: str, *, transient_failures: int = 0) -> None:
        self.root = root
        self.transient_failures = transient_failures
        self.calls = 0

    def list_dir(self, path, page=1, per_page=100, refresh=False):  # noqa: ANN001
        self.calls += 1
        if self.transient_failures > 0:
            self.transient_failures -= 1
            raise OpenListServerError()
        if path.rstrip("/").endswith("/ghost"):
            raise OpenListNotFoundError()
        if path.rstrip("/") == self.root:
            return SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        name="a.mkv", is_dir=False, size=1, modified=1.0,
                        remote_path=f"{self.root}/a.mkv",
                    ),
                    SimpleNamespace(
                        name="ghost", is_dir=True, size=None, modified=1.0,
                        remote_path=f"{self.root}/ghost",
                    ),
                ],
                total=2,
                skipped_entries=0,
            )
        return SimpleNamespace(entries=[], total=0, skipped_entries=0)


def test_full_scan_skips_ghost_directory_and_keeps_going():
    """完整扫描遇到幽灵目录必须跳过并记录，而不是整轮 failed。"""

    from app.media_v4.sources.scanner import scan_openlist_directory

    root = "/夸克网盘/动画"
    stats: dict = {}

    _scan_id, evidence = scan_openlist_directory(
        _FullScanGhostClient(root),
        remote_root=root,
        mapping_root=root,
        mount_root="",
        root_id="root-1",
        scan_stats=stats,
    )

    assert [item.relative_path for item in evidence] == ["a.mkv"], "幽灵目录之外的条目照常入库"
    assert stats["missing_directory_count"] == 1
    assert stats["missing_directories"] == [f"{root}/ghost"]


def test_full_scan_retries_transient_failure_before_failing(monkeypatch):
    """完整扫描同样享受目录级重试（与增量共用同一实现）。"""

    from app.media_v4.sources import scanner

    monkeypatch.setattr(scanner, "LIST_RETRY_DELAYS", (0.0,))
    root = "/夸克网盘/动画"
    stats: dict = {}

    _scan_id, evidence = scanner.scan_openlist_directory(
        _FullScanGhostClient(root, transient_failures=1),
        remote_root=root,
        mapping_root=root,
        mount_root="",
        root_id="root-1",
        scan_stats=stats,
    )

    assert [item.relative_path for item in evidence] == ["a.mkv"]
