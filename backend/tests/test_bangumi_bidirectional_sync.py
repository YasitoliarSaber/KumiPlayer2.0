# -*- coding: utf-8 -*-
"""Bangumi 双向观看进度同步测试。

这些测试在修改生产代码前编写，确认旧实现在关键场景下失败。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _setup_library(works, tmp_path, monkeypatch):
    """Prepare a minimal library index and path isolation."""
    from app.core import paths as core_paths
    from app.library.models import LibraryIndex
    from app.library.store import invalidate_library_index_cache, save_library_index

    data_dir = tmp_path / "data"
    monkeypatch.setattr(core_paths, "get_data_dir", lambda: data_dir)
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(core_paths, "get_cache_dir", lambda: cache_dir)
    invalidate_library_index_cache()

    from app.integrations import bangumi as bg_module

    monkeypatch.setattr(bg_module, "get_state_path", lambda: tmp_path / "bangumi_state.json")

    save_library_index(LibraryIndex(works=works))


def _make_work(work_id="w-1", title="测试作品", episode_count=10, season_number=1):
    """Create a WorkIndex with the given episode count for a season."""
    from app.library.models import EpisodeIndex, WorkIndex

    episodes = []
    for i in range(1, episode_count + 1):
        episode = EpisodeIndex(
            episode_id=f"{work_id}-ep-{i}",
            work_id=work_id,
            season_number=season_number,
            episode_number=i,
            title=f"第{i}集",
            strm_path=f"/mirror/{work_id}/S{season_number:02d}E{i:02d}.strm",
            group_type="season",
        )
        episodes.append(episode)
    work = WorkIndex(
        work_id=work_id,
        title=title,
        seasons=[],
        episodes=episodes,
    )
    return work


def _setup_match(work_id, season_number, subject_id, tmp_path, monkeypatch):
    """Create a Bangumi match for the given work/season."""
    from app.integrations import bangumi as bg_module

    match = bg_module.BangumiMatch(
        work_id=work_id,
        subject_id=subject_id,
        season_number=season_number,
    )
    bg_module.upsert_match(match)


class FakeBangumiClient:
    """Module-level fake Bangumi client for tests."""

    _get_episode_collection_result = None

    def __init__(self, **kwargs):
        pass

    def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
        items = []
        for ep_id, ep_num in [(1001, 1), (1002, 2), (1003, 3), (1004, 4), (1005, 5)]:
            items.append({"id": ep_id, "ep": ep_num, "name": f"Episode {ep_num}", "type": episode_type})
        return items

    def set_episode_collection(self, episode_id, collection_type=2):
        return {"ok": True}

    def get_episode_collection(self, subject_id, limit=1000, offset=0):
        if self._get_episode_collection_result is not None:
            return self._get_episode_collection_result
        return {
            "data": [{"episode_id": 1001, "type": 2}, {"episode_id": 1002, "type": 2}],
            "total": 2,
        }

    def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
        return {"ok": True, "updated": len(episode_ids)}


# ==============================================================================
# 测试 1：网站已看 1-5 集、本地为空 → 同步后本地 1-5 集完成
# ==============================================================================


def test_pull_remote_progress_when_local_empty(tmp_path, monkeypatch):
    """网站第 1-5 集为 type=2、本地为空，执行同步后本地第 1-5 集完成，第 6 集未完成。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.playback import progress as progress_store

    work = _make_work("w-pull", "测试拉取", 10, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-pull", 1, 5001, tmp_path, monkeypatch)

    # 先确认本地没有进度
    items = progress_store.list_progress("w-pull")
    assert len(items) == 0, "测试前提：本地不应有进度"

    from app.integrations import bangumi as bg_module

    fake = FakeBangumiClient()
    fake._get_episode_collection_result = {
        "data": [
            {"episode_id": 1001, "type": 2},
            {"episode_id": 1002, "type": 2},
            {"episode_id": 1003, "type": 2},
            {"episode_id": 1004, "type": 2},
            {"episode_id": 1005, "type": 2},
        ],
        "total": 5,
    }

    try:
        bg_module.sync_bidirectional_progress("w-pull", 1, client=fake)
    except AttributeError:
        # sync_bidirectional_progress 还不存在 — 这正是我们要修复的问题
        raise AssertionError(
            "FAIL: sync_bidirectional_progress 尚未实现。确认旧代码无法处理拉取场景。"
        )

    items_after = progress_store.list_progress("w-pull")
    completed = {item.episode_id for item in items_after if item.completed}
    assert "w-pull-ep-1" in completed, "第 1 集应已完成"
    assert "w-pull-ep-5" in completed, "第 5 集应已完成"
    assert "w-pull-ep-6" not in completed, "第 6 集不应完成"

    # 验证 bangumi_synced 标记
    for item in items_after:
        if item.completed:
            assert item.bangumi_synced, f"{item.episode_id} 应标记为 bangumi_synced"


def test_pull_uses_official_nested_episode_payload(tmp_path, monkeypatch):
    """Bangumi 官方响应把章节 ID 放在 item.episode.id，而不是 item.episode_id。"""
    from app.integrations import bangumi as bg_module
    from app.playback import progress as progress_store

    work = _make_work("w-official-payload", "官方响应结构", 3, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-official-payload", 1, 5011, tmp_path, monkeypatch)

    class OfficialPayloadClient(FakeBangumiClient):
        def get_episode_collection(self, subject_id, limit=1000, offset=0):
            return {
                "data": [
                    {"episode": {"id": 1001, "type": 0, "ep": 1}, "type": 2, "updated_at": 0},
                    {"episode": {"id": 1002, "type": 0, "ep": 2}, "type": 2, "updated_at": 0},
                ],
                "total": 2,
            }

    result = bg_module.sync_bidirectional_progress(
        "w-official-payload",
        1,
        client=OfficialPayloadClient(),
    )

    assert result["pulled"] == 2
    completed = {
        item.episode_id
        for item in progress_store.list_progress("w-official-payload")
        if item.completed
    }
    assert completed == {"w-official-payload-ep-1", "w-official-payload-ep-2"}


# ==============================================================================
# 测试 2：本地已看更多 → 上传差额到 Bangumi
# ==============================================================================


def test_push_local_progress_when_local_ahead(tmp_path, monkeypatch):
    """本地第 1-7 集完成、网站只有第 1-5 集，上传时只补第 6、7 集。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.playback import progress as progress_store

    work = _make_work("w-push", "测试上传", 10, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-push", 1, 5002, tmp_path, monkeypatch)

    # 先标记本地 1-7 集为完成
    for i in range(1, 8):
        progress_store.save_progress("w-push", f"w-push-ep-{i}", 1000, 1000)

    from app.integrations import bangumi as bg_module

    batch_calls = []

    class TrackingFakeClient(FakeBangumiClient):
        def __init__(self):
            super().__init__()
            self._get_episode_collection_result = {
                "data": [
                    {"episode_id": 1001, "type": 2},
                    {"episode_id": 1002, "type": 2},
                    {"episode_id": 1003, "type": 2},
                    {"episode_id": 1004, "type": 2},
                    {"episode_id": 1005, "type": 2},
                ],
                "total": 5,
            }

        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            items = []
            for ep_id, ep_num in [(1001, 1), (1002, 2), (1003, 3), (1004, 4), (1005, 5),
                                  (1006, 6), (1007, 7), (1008, 8), (1009, 9), (1010, 10)]:
                items.append({"id": ep_id, "ep": ep_num, "name": f"Episode {ep_num}", "type": episode_type})
            return items

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            batch_calls.append(set(episode_ids))
            return {"ok": True, "updated": len(episode_ids)}

    try:
        bg_module.sync_bidirectional_progress("w-push", 1, client=TrackingFakeClient())
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    # 验证只上传了 6、7 集
    assert len(batch_calls) == 1, "应只调用一次 batch_set"
    assert batch_calls[0] == {1006, 1007}, f"应只上传第 6、7 集，实际: {batch_calls[0]}"


# ==============================================================================
# 测试 3：跳集时按集合取并集
# ==============================================================================


def test_set_union_not_max_episode_count(tmp_path, monkeypatch):
    """本地和网站存在跳集状态时按集合取并集，不按最大集数填满。"""
    from app.playback import progress as progress_store

    work = _make_work("w-union", "测试并集", 12, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-union", 1, 5003, tmp_path, monkeypatch)

    # 本地已看：1, 3, 5, 7（跳集）
    for i in [1, 3, 5, 7]:
        progress_store.save_progress("w-union", f"w-union-ep-{i}", 1000, 1000)

    from app.integrations import bangumi as bg_module

    class UnionFakeClient(FakeBangumiClient):
        def __init__(self):
            super().__init__()
            self._get_episode_collection_result = {
                "data": [
                    {"episode_id": 2002, "type": 2},
                    {"episode_id": 2004, "type": 2},
                    {"episode_id": 2006, "type": 2},
                ],
                "total": 3,
            }
            self.batch_calls = []

        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            items = []
            for ep_id, ep_num in [(2001, 1), (2002, 2), (2003, 3), (2004, 4), (2005, 5),
                                  (2006, 6), (2007, 7), (2008, 8), (2009, 9), (2010, 10),
                                  (2011, 11), (2012, 12)]:
                items.append({"id": ep_id, "ep": ep_num, "name": f"Episode {ep_num}", "type": episode_type})
            return items

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            self.batch_calls.append(set(episode_ids))
            return {"ok": True, "updated": len(episode_ids)}

    fake = UnionFakeClient()

    try:
        result = bg_module.sync_bidirectional_progress("w-union", 1, client=fake)
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    items_after = progress_store.list_progress("w-union")
    completed = {item.episode_id for item in items_after if item.completed}

    # 网站拉取：2, 4, 6 应完成
    assert "w-union-ep-2" in completed, "第 2 集应由网站拉取"
    assert "w-union-ep-4" in completed, "第 4 集应由网站拉取"
    assert "w-union-ep-6" in completed, "第 6 集应由网站拉取"
    # 本地原有：1, 3, 5, 7 应保持
    assert "w-union-ep-1" in completed
    assert "w-union-ep-3" in completed
    assert "w-union-ep-5" in completed
    assert "w-union-ep-7" in completed
    # 第 8 集不能自动完成（没有按最大值填满）
    assert "w-union-ep-8" not in completed, "第 8 集不应自动完成"


# ==============================================================================
# 测试 4：Bangumi 离线时本地进度不丢失
# ==============================================================================


def test_local_progress_survives_bangumi_offline(tmp_path, monkeypatch):
    """Bangumi 完全离线时，本地完成状态不丢失，仍为待同步。"""
    from app.playback import progress as progress_store

    work = _make_work("w-off", "测试离线", 10, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-off", 1, 5004, tmp_path, monkeypatch)

    # 本地标记 1-3 集完成
    for i in range(1, 4):
        progress_store.save_progress("w-off", f"w-off-ep-{i}", 1000, 1000)

    items_before = progress_store.list_progress("w-off")
    assert all(item.completed for item in items_before)

    # 模拟远端完全不可达
    class OfflineFakeClient:
        def __init__(self, **kwargs):
            pass

        def get_episode_collection(self, subject_id, limit=1000, offset=0):
            from app.integrations.bangumi import BangumiError
            raise BangumiError("Bangumi 无法连接", status_code=0)

    from app.integrations import bangumi as bg_module

    try:
        result = bg_module.sync_bidirectional_progress("w-off", 1, client=OfflineFakeClient())
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    # 离线时本地进度必须保留
    items_after = progress_store.list_progress("w-off")
    assert all(item.completed for item in items_after), "离线时本地完成状态不能丢失"
    for item in items_after:
        assert item.bangumi_synced is False, f"离线时应为待同步状态: {item.episode_id}"


# ==============================================================================
# 测试 5：远端读取成功但上传失败
# ==============================================================================


def test_remote_read_succeeds_but_upload_fails(tmp_path, monkeypatch):
    """远端读取成功但上传失败时，远端已有进度可以落地，本地独有进度保持待上传。"""
    from app.playback import progress as progress_store

    work = _make_work("w-partial", "测试部分成功", 10, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-partial", 1, 5005, tmp_path, monkeypatch)

    # 本地已看：5, 6（网站没有）
    for i in [5, 6]:
        progress_store.save_progress("w-partial", f"w-partial-ep-{i}", 1000, 1000)

    # 网站已看：1-4，上传 5、6 失败
    class PartialFakeClient:
        def __init__(self, **kwargs):
            pass

        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            items = []
            for ep_id, ep_num in [(3001, 1), (3002, 2), (3003, 3), (3004, 4),
                                  (3005, 5), (3006, 6), (3007, 7), (3008, 8),
                                  (3009, 9), (3010, 10)]:
                items.append({"id": ep_id, "ep": ep_num, "name": f"Episode {ep_num}", "type": episode_type})
            return items

        def get_episode_collection(self, subject_id, limit=1000, offset=0):
            return {
                "data": [
                    {"episode_id": 3001, "type": 2},
                    {"episode_id": 3002, "type": 2},
                    {"episode_id": 3003, "type": 2},
                    {"episode_id": 3004, "type": 2},
                ],
                "total": 4,
            }

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            from app.integrations.bangumi import BangumiError
            raise BangumiError("上传失败", status_code=500)

    from app.integrations import bangumi as bg_module

    try:
        result = bg_module.sync_bidirectional_progress("w-partial", 1, client=PartialFakeClient())
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    items = progress_store.list_progress("w-partial")
    completed_ids = {item.episode_id for item in items if item.completed}
    synced_ids = {item.episode_id for item in items if item.bangumi_synced}

    # 网站 1-4 已拉取到本地
    assert "w-partial-ep-1" in completed_ids, "网站进度应已拉取"
    assert "w-partial-ep-4" in completed_ids, "网站进度应已拉取"

    # 本地 5、6 仍为完成但待同步
    assert "w-partial-ep-5" in completed_ids
    assert "w-partial-ep-6" in completed_ids
    assert "w-partial-ep-5" not in synced_ids, "上传失败应保持待同步"
    assert "w-partial-ep-6" not in synced_ids, "上传失败应保持待同步"


# ==============================================================================
# 测试 6：第二次执行相同同步幂等
# ==============================================================================


def test_sync_is_idempotent(tmp_path, monkeypatch):
    """第二次执行相同同步不重复 PATCH。"""
    from app.playback import progress as progress_store

    work = _make_work("w-idem", "测试幂等", 10, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-idem", 1, 5006, tmp_path, monkeypatch)

    from app.integrations import bangumi as bg_module

    class IdempotentFakeClient(FakeBangumiClient):
        def __init__(self):
            super().__init__()
            self._get_episode_collection_result = {
                "data": [
                    {"episode_id": 4001, "type": 2},
                    {"episode_id": 4002, "type": 2},
                ],
                "total": 2,
            }
            self.batch_calls = 0

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            self.batch_calls += 1
            return {"ok": True, "updated": len(episode_ids)}

    fake = IdempotentFakeClient()

    try:
        # 第一次同步：网站 1、2 已看，本地无进度 → 拉取
        r1 = bg_module.sync_bidirectional_progress("w-idem", 1, client=fake)
        # 第二次同步：两边一致 → 不应有网络请求
        r2 = bg_module.sync_bidirectional_progress("w-idem", 1, client=fake)
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    assert fake.batch_calls == 0, "两次同步都不应调用 batch_set（第一次也没有本地上传）"


# ==============================================================================
# 测试 7：第 1 季同步不得修改第 2、3 季
# ==============================================================================


def test_sync_does_not_affect_other_seasons(tmp_path, monkeypatch):
    """第 1 季同步不得修改第 2、3 季。"""
    from app.playback import progress as progress_store

    s1 = _make_work("w-s1", "跨季测试S1", 5, 1)
    s3 = _make_work("w-s3", "跨季测试S3", 5, 3)
    # s2: 使用单独 work_id 避免 progress 重叠
    from app.library.models import EpisodeIndex, WorkIndex

    s2_episodes = []
    for i in range(1, 6):
        s2_episodes.append(EpisodeIndex(
            episode_id=f"w-s2-ep-{i}",
            work_id="w-s2",
            season_number=2,
            episode_number=i,
            title=f"第{i}集",
            group_type="season",
        ))
    s2_work = WorkIndex(work_id="w-s2", title="跨季测试S2", seasons=[], episodes=s2_episodes)

    _setup_library([s1, s2_work, s3], tmp_path, monkeypatch)

    from app.integrations import bangumi as bg_module

    for wid, s, sid in [("w-s1", 1, 5101), ("w-s2", 2, 5102), ("w-s3", 3, 5103)]:
        bg_module.upsert_match(bg_module.BangumiMatch(
            work_id=wid, subject_id=sid, season_number=s
        ))

    # 第 w-s2 季本地标记 1-3 集完成
    for i in range(1, 4):
        progress_store.save_progress("w-s2", f"w-s2-ep-{i}", 1000, 1000)

    # 第 w-s1 季同步
    class SeasonFakeClient:
        def __init__(self, **kwargs):
            pass

        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            items = []
            for ep_id, ep_num in [(5101, 1), (5102, 2), (5103, 3), (5104, 4), (5105, 5)]:
                items.append({"id": ep_id, "ep": ep_num, "name": f"Episode {ep_num}", "type": episode_type})
            return items

        def get_episode_collection(self, subject_id, limit=1000, offset=0):
            return {"data": [], "total": 0}

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            return {"ok": True}

    try:
        bg_module.sync_bidirectional_progress("w-s1", 1, client=SeasonFakeClient())
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    # 第 w-s2 季本地进度不丢失
    items = progress_store.list_progress("w-s2")
    s2_completed = [item for item in items if item.episode_id in ("w-s2-ep-1", "w-s2-ep-2", "w-s2-ep-3")]
    assert all(item.completed for item in s2_completed), "第 w-s2 季进度不应被第 w-s1 季同步影响"
    assert len(s2_completed) == 3


def test_successful_push_only_marks_current_season_synced(tmp_path, monkeypatch):
    """同一个 work_id 下同步第 1 季时，不得把第 2 季标为已同步。"""
    from app.integrations import bangumi as bg_module
    from app.library.models import EpisodeIndex, WorkIndex
    from app.playback import progress as progress_store
    from app.playback.progress import PlaybackProgressItem

    work_id = "w-real-seasons"
    season_one = EpisodeIndex(
        episode_id="w-real-seasons-s1e1",
        work_id=work_id,
        season_number=1,
        episode_number=1,
        title="第一季第一集",
        group_type="season",
    )
    season_two = EpisodeIndex(
        episode_id="w-real-seasons-s2e1",
        work_id=work_id,
        season_number=2,
        episode_number=1,
        title="第二季第一集",
        group_type="season",
    )
    _setup_library(
        [WorkIndex(work_id=work_id, title="真实多季度", seasons=[], episodes=[season_one, season_two])],
        tmp_path,
        monkeypatch,
    )
    bg_module.upsert_match(bg_module.BangumiMatch(
        work_id=work_id,
        subject_id=5201,
        season_number=1,
        episode_map={season_one.episode_id: 12001},
    ))
    bg_module.upsert_match(bg_module.BangumiMatch(
        work_id=work_id,
        subject_id=5202,
        season_number=2,
        episode_map={season_two.episode_id: 22001},
    ))
    progress_store._write_progress([
        PlaybackProgressItem(
            work_id=work_id,
            episode_id=season_one.episode_id,
            position=1,
            duration=1,
            ratio=1,
            completed=True,
        ),
        PlaybackProgressItem(
            work_id=work_id,
            episode_id=season_two.episode_id,
            position=1,
            duration=1,
            ratio=1,
            completed=True,
        ),
    ])

    class CurrentSeasonClient:
        def get_episode_collection(self, subject_id, limit=1000, offset=0):
            return {"data": [], "total": 0}

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            assert subject_id == 5201
            assert episode_ids == [12001]
            return {"ok": True, "updated": 1}

    bg_module.sync_bidirectional_progress(work_id, 1, client=CurrentSeasonClient())

    progress = {
        item.episode_id: item
        for item in progress_store.list_progress(work_id)
    }
    assert progress[season_one.episode_id].bangumi_synced is True
    assert progress[season_two.episode_id].bangumi_synced is False


# ==============================================================================
# 测试 8：SP 不参与普通集数推断
# ==============================================================================


def test_special_episodes_not_merged_with_regular(tmp_path, monkeypatch):
    """SP 等非本篇剧集不参与普通集数推断。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.playback import progress as progress_store

    sp = EpisodeIndex(
        episode_id="w-sp-sp-1",
        work_id="w-sp",
        season_number=1,
        episode_number=1,
        title="特别篇",
        group_type="special",
    )
    reg = EpisodeIndex(
        episode_id="w-sp-ep-1",
        work_id="w-sp",
        season_number=1,
        episode_number=1,
        title="第一集",
        group_type="season",
    )
    work = WorkIndex(work_id="w-sp", title="SP 测试", seasons=[], episodes=[sp, reg])
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-sp", 1, 5008, tmp_path, monkeypatch)

    from app.integrations import bangumi as bg_module

    class SpFakeClient:
        def __init__(self, **kwargs):
            pass

        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            return [{"id": 8001, "ep": 1, "name": "SP", "type": episode_type}]

        def get_episode_collection(self, subject_id, limit=1000, offset=0):
            return {"data": [{"episode_id": 8001, "type": 2}], "total": 1}

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            return {"ok": True}

    try:
        bg_module.sync_bidirectional_progress("w-sp", 1, client=SpFakeClient())
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    items = progress_store.list_progress("w-sp")
    completed = {item.episode_id for item in items if item.completed}
    assert completed == {"w-sp-ep-1"}
    assert "w-sp-sp-1" not in completed


# ==============================================================================
# 测试 9：manually_unwatched 不被拉取覆盖
# ==============================================================================


def test_manually_unwatched_respected_from_pull(tmp_path, monkeypatch):
    """manually_unwatched=True 不被自动拉取覆盖。"""
    from app.playback import progress as progress_store

    work = _make_work("w-manual", "手动取消测试", 10, 1)
    _setup_library([work], tmp_path, monkeypatch)
    _setup_match("w-manual", 1, 5009, tmp_path, monkeypatch)

    # 用户手动取消第 3 集已看
    progress_store.mark_episode_completed("w-manual", "w-manual-ep-3", False)
    assert progress_store.list_progress("w-manual")[0].manually_unwatched

    from app.integrations import bangumi as bg_module

    fake = FakeBangumiClient()
    fake._get_episode_collection_result = {
        "data": [
            {"episode_id": 9001, "type": 2},
            {"episode_id": 9002, "type": 2},
            {"episode_id": 9003, "type": 2},
            {"episode_id": 9004, "type": 2},
            {"episode_id": 9005, "type": 2},
        ],
        "total": 5,
    }

    try:
        bg_module.sync_bidirectional_progress("w-manual", 1, client=fake)
    except AttributeError:
        raise AssertionError("FAIL: sync_bidirectional_progress 尚未实现")

    items = progress_store.list_progress("w-manual")
    ep3 = next(item for item in items if item.episode_id == "w-manual-ep-3")
    assert not ep3.completed, "manually_unwatched 不应被拉取覆盖"


# ==============================================================================
# 测试 10：普通零进度记录不被误认为手动取消
# ==============================================================================


def test_zero_progress_not_mistaken_for_manual_unwatch(tmp_path, monkeypatch):
    """普通 position=0、duration=0 记录不再被误认为手动取消。"""
    from app.playback import progress as progress_store

    work = _make_work("w-zero", "零进度测试", 5, 1)
    _setup_library([work], tmp_path, monkeypatch)

    # 创建一条零进度记录（从未播放完的）
    from app.playback.progress import PlaybackProgressItem
    now = "2026-07-28T12:00:00+08:00"
    item = PlaybackProgressItem(
        work_id="w-zero",
        episode_id="w-zero-ep-1",
        position=0,
        duration=0,
        ratio=0,
        completed=False,
        manually_unwatched=False,
        updated_at=now,
    )
    items = progress_store.load_progress()
    items.append(item)
    from app.playback.progress import _write_progress
    _write_progress(items)

    # 重新读取
    loaded = progress_store.list_progress("w-zero")
    ep1 = next(item for item in loaded if item.episode_id == "w-zero-ep-1")
    assert not ep1.manually_unwatched, "零进度记录不应被标记为 manually_unwatched"


# ==============================================================================
# 测试 11：confirm_match 后立即拉取
# ==============================================================================


def test_confirm_match_pulls_remote_progress(tmp_path, monkeypatch):
    """刚确认匹配时能同时拉取网站进度，而不只是上传本地进度。"""
    from app.playback import progress as progress_store

    work = _make_work("w-confirm-pull", "匹配后拉取", 10, 1)
    _setup_library([work], tmp_path, monkeypatch)

    # 先不要匹配

    from fastapi.testclient import TestClient
    from app.api import bangumi as bangumi_api
    from app.main import app

    class ConfirmPullClient:
        def __init__(self, **kwargs):
            pass

        def list_subject_episodes(self, subject_id, episode_type=0, limit=1000):
            items = []
            for ep_id, ep_num in [(11001, 1), (11002, 2), (11003, 3), (11004, 4), (11005, 5),
                                  (11006, 6), (11007, 7), (11008, 8), (11009, 9), (11010, 10)]:
                items.append({"id": ep_id, "ep": ep_num, "name": f"Episode {ep_num}", "type": episode_type})
            return items

        def get_episode_collection(self, subject_id, limit=1000, offset=0):
            return {
                "data": [
                    {"episode_id": 11001, "type": 2},
                    {"episode_id": 11002, "type": 2},
                ],
                "total": 2,
            }

        def batch_set_episode_collection(self, subject_id, episode_ids, collection_type=2):
            return {"ok": True}

    monkeypatch.setattr(bangumi_api, "BangumiClient", lambda: ConfirmPullClient())
    from app.integrations import bangumi as bg_module
    monkeypatch.setattr(bg_module, "BangumiClient", lambda: ConfirmPullClient())

    client = TestClient(app)
    response = client.post(
        "/api/integrations/bangumi/matches/w-confirm-pull",
        json={"season_number": 1, "subject_id": 9999},
    )

    # 检查确认匹配后是否有拉取结果
    data = response.json()
    assert response.status_code == 200

    # 验证本地已被写入网站进度
    items = progress_store.list_progress("w-confirm-pull")
    completed_ids = {item.episode_id for item in items if item.completed}
    assert "w-confirm-pull-ep-1" in completed_ids, "匹配后应自动拉取网站已看进度"
    assert "w-confirm-pull-ep-2" in completed_ids
