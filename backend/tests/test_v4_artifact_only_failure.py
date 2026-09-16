"""O4-1：混合产物失败不得被当成"只缺图片"。

`artifact_only_failure` 决定一个 failed 的作品是否按"资料已就绪、只差本地图片"
处理：只有**全部**原因都是图片问题时才允许放行为 metadata ready + artifact
degraded。原先在结构化原因列表判定失败后还会退回到对拼接 `reason` 字符串做
子串匹配，于是"海报失败 + 缺 3 个剧集 NFO"这种混合失败会被误判为只缺图片，
缺 NFO 的作品因此进入媒体墙。
"""

from __future__ import annotations

import pytest


def _mixed() -> dict:
    return {
        "metadata_state": "failed",
        "completeness": ["海报下载或发布失败", "缺少或损坏 3 个剧集 NFO"],
        "reason": "海报下载或发布失败；缺少或损坏 3 个剧集 NFO",
    }


def test_mixed_artwork_and_nfo_failure_is_not_artifact_only():
    from app.media_v4.jobs.completeness import artifact_only_failure

    assert artifact_only_failure(_mixed()) is False


def test_artwork_only_failure_is_still_treated_as_artifact_only():
    from app.media_v4.jobs.completeness import artifact_only_failure

    metadata = {
        "metadata_state": "failed",
        "completeness": ["背景图下载或发布失败"],
        "reason": "背景图下载或发布失败",
    }
    assert artifact_only_failure(metadata) is True


def test_nfo_only_failure_is_not_artifact_only():
    from app.media_v4.jobs.completeness import artifact_only_failure

    metadata = {
        "metadata_state": "failed",
        "completeness": ["缺少或损坏 3 个剧集 NFO"],
        "reason": "缺少或损坏 3 个剧集 NFO",
    }
    assert artifact_only_failure(metadata) is False


def test_explicit_artifact_incomplete_reason_code_still_wins():
    from app.media_v4.jobs.completeness import artifact_only_failure

    metadata = {
        "metadata_state": "failed",
        "reason_code": "artifact_incomplete",
        "completeness": ["海报下载或发布失败", "缺少或损坏 1 个剧集 NFO"],
        "reason": "海报下载或发布失败；缺少或损坏 1 个剧集 NFO",
    }
    # 新写入的失败路径会给 artifact_incomplete 明确原因码；它优先于文本推断。
    assert artifact_only_failure(metadata) is True


@pytest.mark.parametrize("reason, expected", [
    ("海报下载或发布失败", True),
    ("缺少或损坏 3 个剧集 NFO", False),
])
def test_legacy_records_without_completeness_keep_text_fallback(reason, expected):
    from app.media_v4.jobs.completeness import artifact_only_failure

    metadata = {"metadata_state": "failed", "reason": reason}
    assert artifact_only_failure(metadata) is expected


def test_non_failed_state_is_never_artifact_only():
    from app.media_v4.jobs.completeness import artifact_only_failure

    assert artifact_only_failure({"metadata_state": "ready", "completeness": ["海报下载或发布失败"]}) is False
