from app.import_plan.models import ImportPlan, ImportPlanItem
from app.scrape.models import ScrapeTarget
from app.scrape.service import _tmdb_absolute_episode_offset
from app.scrape.service import _generate_episode_nfos
from app.tasks.models import TaskCancelledError


def _episode(item_id: str, season: int, episode: int) -> ImportPlanItem:
    return ImportPlanItem(
        id=item_id, source="local", resource_type="video", action="generate_strm",
        work_id="work-a", work_title="作品A", series_group="作品A",
        group_type="season", season_number=season, episode_number=episode,
    )


def test_sparse_previous_season_never_creates_heuristic_absolute_offset():
    current = _episode("s2e1", 2, 1)
    plan = ImportPlan(items=[
        _episode("s1e1", 1, 1), _episode("s1e2", 1, 2), _episode("s1e4", 1, 4), current,
    ])
    target = ScrapeTarget(
        work_id="work-a", series_group="作品A", group_type="season",
        local_season_number=2, item_ids=[current.id],
    )

    offset = _tmdb_absolute_episode_offset(
        target=target, plan=plan, tmdb_season_number=1,
        season_episodes={number: {"episode_number": number} for number in range(1, 25)},
        episode_items=[(current, 1)],
    )

    assert offset == 0


def test_remote_metadata_lag_still_writes_local_placeholder_nfo(tmp_path, monkeypatch):
    current = _episode("s1e9", 1, 9)
    current.plan_id = "lag-plan"
    current.target_strm_path = str(tmp_path / "S01E09.strm")
    plan = ImportPlan(plan_id="lag-plan", items=[current])
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class LaggingClient:
        def get_tv_season_detail(self, tmdb_id, season_number):
            return {"episodes": [{"episode_number": number} for number in range(1, 9)]}

        def get_tv_episode_detail(self, tmdb_id, season_number, episode_number):
            raise LookupError("TMDB 尚未收录")

    target = ScrapeTarget(
        import_plan_id="lag-plan", work_id="work-a", series_group="作品A",
        group_type="season", local_season_number=1, item_ids=[current.id],
    )

    results = _generate_episode_nfos(target, 100, 1, str(tmp_path), LaggingClient())

    nfo = tmp_path / "S01E09.nfo"
    assert nfo.exists()
    text = nfo.read_text(encoding="utf-8")
    assert "<title>第 9 集</title>" in text
    assert "<metadatapending>true</metadatapending>" in text
    assert results[0]["status"] == "metadata_pending"


def test_absolute_local_episode_numbers_use_tmdb_season_boundaries(tmp_path, monkeypatch):
    """本地 1-47 连续编号应按 TMDB 的 24+23 边界读取两季分集信息。"""
    items = []
    for episode in (1, 24, 25, 47):
        item = _episode(f"e{episode}", 1, episode)
        item.plan_id = "absolute-prefix-plan"
        item.target_strm_path = str(tmp_path / f"S01E{episode:02d}.strm")
        items.append(item)
    plan = ImportPlan(plan_id="absolute-prefix-plan", items=items)
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class MultiSeasonClient:
        calls = []

        def get_tv_season_episodes(self, tmdb_id, season_number):
            self.calls.append(season_number)
            count = {1: 24, 2: 23}[season_number]
            return {
                "episodes": [
                    {"episode_number": episode, "name": f"S{season_number} 第 {episode} 集"}
                    for episode in range(1, count + 1)
                ],
            }

    client = MultiSeasonClient()
    target = ScrapeTarget(
        import_plan_id=plan.plan_id,
        work_id="work-a",
        series_group="作品A",
        group_type="season",
        local_season_number=1,
        item_ids=[item.id for item in items],
    )
    detail = {
        "seasons": [
            {"season_number": 1, "episode_count": 24},
            {"season_number": 2, "episode_count": 23},
            {"season_number": 3, "episode_count": 12},
        ],
    }

    results = _generate_episode_nfos(
        target,
        95479,
        1,
        str(tmp_path),
        client,
        series_detail=detail,
    )

    assert client.calls == [1, 2]
    assert [item["tmdb_episode"] for item in results] == [
        "S01E01",
        "S01E24",
        "S02E01",
        "S02E23",
    ]
    assert "S2 第 1 集" in (tmp_path / "S01E25.nfo").read_text(encoding="utf-8")


def test_season_batch_failure_does_not_fan_out_to_episode_requests(tmp_path, monkeypatch):
    items = [_episode(f"e{episode}", 1, episode) for episode in (1, 2)]
    for item in items:
        item.plan_id = "network-failure-plan"
        item.target_strm_path = str(tmp_path / f"S01E{item.episode_number:02d}.strm")
    plan = ImportPlan(plan_id="network-failure-plan", items=items)
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class FailingSeasonClient:
        episode_calls = 0

        def get_tv_season_episodes(self, tmdb_id, season_number):
            raise ConnectionError("SSL EOF")

        def get_tv_episode_detail(self, tmdb_id, season_number, episode_number):
            self.episode_calls += 1
            raise ConnectionError("SSL EOF")

    client = FailingSeasonClient()
    target = ScrapeTarget(
        import_plan_id=plan.plan_id,
        group_type="season",
        local_season_number=1,
        item_ids=[item.id for item in items],
    )

    results = _generate_episode_nfos(target, 100, 1, str(tmp_path), client)

    assert client.episode_calls == 0
    assert [item["status"] for item in results] == ["metadata_pending", "metadata_pending"]


def test_episode_generation_stops_between_image_downloads(tmp_path, monkeypatch):
    items = [_episode(f"e{episode}", 1, episode) for episode in (1, 2)]
    for item in items:
        item.plan_id = "cancel-episode-plan"
        item.target_strm_path = str(tmp_path / f"S01E{item.episode_number:02d}.strm")
    plan = ImportPlan(plan_id="cancel-episode-plan", items=items)
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    cancelled = False

    class SlowImageClient:
        download_calls = 0

        def get_tv_season_episodes(self, tmdb_id, season_number):
            return {
                "episodes": [
                    {"episode_number": episode, "name": f"第 {episode} 集", "still_path": f"/{episode}.jpg"}
                    for episode in (1, 2)
                ],
            }

        def download_image(self, file_path, dest, size=None):
            nonlocal cancelled
            self.download_calls += 1
            cancelled = True
            return True

    client = SlowImageClient()
    target = ScrapeTarget(
        import_plan_id=plan.plan_id,
        group_type="season",
        local_season_number=1,
        item_ids=[item.id for item in items],
    )

    try:
        _generate_episode_nfos(
            target,
            100,
            1,
            str(tmp_path),
            client,
            should_cancel=lambda: cancelled,
        )
        assert False, "收到取消信号后应立即退出分集循环"
    except TaskCancelledError:
        pass

    assert client.download_calls == 1
    assert not (tmp_path / "S01E02.nfo").exists()


def test_remote_artwork_mode_does_not_download_every_episode_image(tmp_path, monkeypatch):
    item = _episode("e1", 1, 1)
    item.plan_id = "remote-artwork-plan"
    item.target_strm_path = str(tmp_path / "S01E01.strm")
    plan = ImportPlan(plan_id="remote-artwork-plan", items=[item])
    monkeypatch.setattr("app.import_plan.store.load_import_plan", lambda plan_id: plan)

    class RemoteArtworkClient:
        download_calls = 0

        def get_tv_season_episodes(self, tmdb_id, season_number):
            return {"episodes": [{"episode_number": 1, "name": "第一集", "still_path": "/still.jpg"}]}

        def download_image(self, file_path, dest, size=None):
            self.download_calls += 1
            return True

        def build_image_url(self, file_path, size=None):
            return f"https://image.tmdb.org/t/p/{size}{file_path}"

    client = RemoteArtworkClient()
    target = ScrapeTarget(
        import_plan_id=plan.plan_id,
        group_type="season",
        local_season_number=1,
        item_ids=[item.id],
    )

    _generate_episode_nfos(
        target,
        100,
        1,
        str(tmp_path),
        client,
        download_artwork=False,
    )

    assert client.download_calls == 0
    assert "https://image.tmdb.org/t/p/w500/still.jpg" in (
        tmp_path / "S01E01.nfo"
    ).read_text(encoding="utf-8")
