"""扫描对上游瞬时故障的韧性：目录级重试 + 失败时保留进度可续扫。

实测故障：一轮扫描已经走完 763 个目录，被**一次**上游 5xx（夸克驱动偶发）直接
判成 failed；证据与 frontier 都在库里，但界面只能让用户从头再来。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.openlist.models import (
    OpenListRiskControlError,
    OpenListScanLimitExceeded,
    OpenListServerError,
)
from app.media_v4.sources import incremental
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
    monkeypatch.setattr(incremental, "_LIST_RETRY_DELAYS", (0.0,))
    client = _FlakyClient(OpenListServerError())

    entries = _collect(client)

    assert len(entries) == 1, "重试成功后就该正常返回"
    assert client.calls == 2, "瞬时 5xx 必须原地重试一次"


def test_directory_listing_gives_up_after_bounded_retries(monkeypatch):
    monkeypatch.setattr(incremental, "_LIST_RETRY_DELAYS", (0.0, 0.0))
    client = _FlakyClient(OpenListServerError(), fail_times=99)

    with pytest.raises(OpenListServerError):
        _collect(client)

    assert client.calls == 3, "重试次数必须有上限（1 次 + 2 次重试）"


def test_directory_listing_never_retries_risk_control(monkeypatch):
    """反证：远端风控重试只会加剧，必须立即抛出。"""

    monkeypatch.setattr(incremental, "_LIST_RETRY_DELAYS", (0.0,))
    client = _FlakyClient(OpenListRiskControlError())

    with pytest.raises(OpenListRiskControlError):
        _collect(client)

    assert client.calls == 1, "风控不得重试"


def test_scan_limit_still_trips_immediately(monkeypatch):
    """扫描上限与重试无关：列目录成功后仍要按上限终止。"""

    monkeypatch.setattr(incremental, "_LIST_RETRY_DELAYS", (0.0,))
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
