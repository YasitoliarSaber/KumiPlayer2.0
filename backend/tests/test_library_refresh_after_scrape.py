# -*- coding: utf-8 -*-
"""刮削后的媒体库局部刷新测试。"""


def _patch_data_dirs(monkeypatch, tmp_path):
    from app.library.store import invalidate_library_index_cache

    data_dir = tmp_path / "data"
    mirror_root = tmp_path / "mirror"
    monkeypatch.setattr("app.core.paths.get_data_dir", lambda: data_dir)
    monkeypatch.setattr("app.core.paths.get_cache_dir", lambda: data_dir / "cache")
    monkeypatch.setattr("app.core.paths.get_mirror_root", lambda: mirror_root)
    invalidate_library_index_cache()
    return data_dir, mirror_root


def test_refresh_library_for_scrape_target_rebuilds_only_affected_work(tmp_path, monkeypatch):
    """刮削后只重建当前作品，不把其他作品从索引里清掉。"""
    _data_dir, mirror_root = _patch_data_dirs(monkeypatch, tmp_path)

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.index import _library_work_id
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import refresh_library_for_scrape_target
    from app.library.store import load_library_index, save_library_index
    from app.scrape.models import ScrapeTarget

    show_dir = mirror_root / "115" / "Show A" / "Season 1"
    show_dir.mkdir(parents=True)
    strm_path = show_dir / "S01E01.strm"
    strm_path.write_text("H:/media/show-a-01.mkv", encoding="utf-8")

    other_dir = mirror_root / "115" / "Show B" / "Season 1"
    other_dir.mkdir(parents=True)
    other_strm = other_dir / "S01E01.strm"
    other_strm.write_text("H:/media/show-b-01.mkv", encoding="utf-8")

    plan = ImportPlan(
        plan_id="p1",
        source="pan115",
        status="confirmed",
        items=[
            ImportPlanItem(
                id="i1",
                plan_id="p1",
                source="pan115",
                relative_path="动画/Show A/01.mkv",
                real_path="H:/media/show-a-01.mkv",
                resource_type="video",
                action="generate_strm",
                work_id="work-a",
                work_title="Show A",
                media_type="tv",
                card_type="standalone",
                group_type="season",
                season_number=1,
                episode_number=1,
                target_dir=str(show_dir),
                target_strm_path=str(strm_path),
            ),
            ImportPlanItem(
                id="i2",
                plan_id="p1",
                source="pan115",
                relative_path="动画/Show B/01.mkv",
                real_path="H:/media/show-b-01.mkv",
                resource_type="video",
                action="generate_strm",
                work_id="work-b",
                work_title="Show B",
                media_type="tv",
                card_type="standalone",
                group_type="season",
                season_number=1,
                episode_number=1,
                target_dir=str(other_dir),
                target_strm_path=str(other_strm),
            ),
        ],
    )
    save_import_plan(plan)
    save_library_index(LibraryIndex(
        works=[
            WorkIndex(work_id="work-a", source="pan115", title="旧 Show A"),
            WorkIndex(
                work_id="work-b",
                source="pan115",
                title="Show B",
                episodes=[EpisodeIndex(episode_id="old-b", work_id="work-b")],
            ),
        ],
        source_summary={"pan115": {"work_count": 2, "episode_count": 1, "strm_count": 2}},
    ))

    result = refresh_library_for_scrape_target(ScrapeTarget(
        scrape_target_id="t1",
        source="pan115",
        import_plan_id="p1",
        work_id="work-a",
        card_type="standalone",
        media_type="tv",
        group_type="season",
        local_season_number=1,
        target_dir=str(show_dir),
        item_ids=["i1"],
    ))

    assert result["mode"] == "partial"
    index = load_library_index()
    works = {work.work_id: work for work in index.works}
    show_a_id = _library_work_id(plan.items[0])
    assert "work-a" not in works
    assert works[show_a_id].title == "Show A"
    assert len(works[show_a_id].episodes) == 1
    assert works["work-b"].title == "Show B"
    assert len(works["work-b"].episodes) == 1


def test_library_index_marks_work_ready_only_after_all_scrape_outputs_exist(tmp_path, monkeypatch):
    """作品级身份、NFO、图片和分集 NFO 未全部完成前必须保持 waiting_metadata。"""
    _data_dir, mirror_root = _patch_data_dirs(monkeypatch, tmp_path)

    from pathlib import Path

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.index import _library_work_id, build_library_index
    from app.library.scanner import MirrorFile, MirrorScanResult
    from app.scrape.models import ScrapeMap, ScrapeMapItem
    from app.scrape.target_builder import build_scrape_targets

    ready_dir = mirror_root / "local" / "Ready" / "Season 1"
    pending_dir = mirror_root / "local" / "Pending" / "Season 1"
    ready_dir.mkdir(parents=True)
    pending_dir.mkdir(parents=True)
    ready_strm = ready_dir / "S01E01.strm"
    pending_strm = pending_dir / "S01E01.strm"
    ready_strm.write_text("D:/media/ready.mkv", encoding="utf-8")
    pending_strm.write_text("D:/media/pending.mkv", encoding="utf-8")

    items = [
        ImportPlanItem(
            id="ready-item", plan_id="visibility-plan", source="local",
            resource_type="video", action="generate_strm", work_id="ready-raw",
            work_title="Ready", media_type="tv", card_type="standalone",
            group_type="season", season_number=1, episode_number=1,
            target_dir=str(ready_dir), target_strm_path=str(ready_strm),
        ),
        ImportPlanItem(
            id="pending-item", plan_id="visibility-plan", source="local",
            resource_type="video", action="generate_strm", work_id="pending-raw",
            work_title="Pending", media_type="tv", card_type="standalone",
            group_type="season", season_number=1, episode_number=1,
            target_dir=str(pending_dir), target_strm_path=str(pending_strm),
        ),
    ]
    plan = ImportPlan(
        plan_id="visibility-plan", source="local", status="executed", items=items,
    )
    save_import_plan(plan)
    targets = {target.work_id: target for target in build_scrape_targets(plan)}

    ready_target = targets["ready-raw"]
    ready_nfo = Path(ready_target.target_nfo_path)
    ready_nfo.write_text("<tvshow><title>完成作品</title></tvshow>", encoding="utf-8")
    (ready_dir / "S01E01.nfo").write_text("<episodedetails />", encoding="utf-8")
    Path(ready_target.target_poster_path).write_bytes(b"poster")
    Path(ready_target.target_fanart_path).write_bytes(b"fanart")

    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id=ready_target.scrape_target_id,
        work_id="ready-raw",
        source="local",
        import_plan_id=plan.plan_id,
        tmdb_id=100,
        tmdb_type="tv",
        nfo_path=str(ready_nfo),
        poster_path=ready_target.target_poster_path,
        fanart_path=ready_target.target_fanart_path,
    )])
    scan_result = MirrorScanResult(source="local", strm_files=[
        MirrorFile(source="local", strm_path=str(ready_strm), exists=True),
        MirrorFile(source="local", strm_path=str(pending_strm), exists=True),
    ])

    index = build_library_index(plan, scrape_map, scan_result)
    states = {work.work_id: work.metadata_state for work in index.works}

    assert states[_library_work_id(items[0])] == "ready"
    assert states[_library_work_id(items[1])] == "waiting_metadata"
    assert index.version == 2


def test_refresh_library_rejects_empty_replacement_and_preserves_existing_card(tmp_path, monkeypatch):
    """镜像路径失效导致构建不到卡片时，局部刷新必须失败且不能删掉旧卡。"""
    _patch_data_dirs(monkeypatch, tmp_path)

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.models import LibraryIndex, WorkIndex
    from app.library.service import refresh_library_for_scrape_target
    from app.library.store import load_library_index, save_library_index
    from app.scrape.models import ScrapeTarget

    plan = ImportPlan(
        plan_id="broken-path-plan",
        source="baidu",
        status="confirmed",
        items=[ImportPlanItem(
            id="broken-item",
            plan_id="broken-path-plan",
            source="baidu",
            relative_path="作品/S01E01.mkv",
            real_path=str(tmp_path / "不存在" / "S01E01.mkv"),
            resource_type="video",
            action="generate_strm",
            work_id="broken-work",
            work_title="路径待修复作品",
            media_type="tv",
            card_type="standalone",
            group_type="season",
            season_number=1,
            episode_number=1,
            target_dir=str(tmp_path / "mirror" / "baidu" / "路径待修复作品"),
            target_strm_path=str(tmp_path / "mirror" / "baidu" / "路径待修复作品" / "S01E01.strm"),
        )],
    )
    save_import_plan(plan)
    save_library_index(LibraryIndex(works=[
        WorkIndex(work_id="broken-work", source="baidu", title="旧卡片仍需保留"),
    ]))

    target = ScrapeTarget(
        scrape_target_id="broken-target",
        source="baidu",
        import_plan_id=plan.plan_id,
        work_id="broken-work",
        card_type="standalone",
        media_type="tv",
        group_type="season",
        local_season_number=1,
        item_ids=["broken-item"],
    )

    import pytest
    with pytest.raises(RuntimeError, match="没有生成任何可展示作品"):
        refresh_library_for_scrape_target(target)

    assert [work.title for work in load_library_index().works] == ["旧卡片仍需保留"]


def test_partial_refresh_restores_relations_from_the_full_source_plan(tmp_path, monkeypatch):
    """局部刮削只重建一张卡，也必须从完整计划恢复同系列关联。"""
    _data_dir, mirror_root = _patch_data_dirs(monkeypatch, tmp_path)

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.import_plan.store import save_import_plan
    from app.library.index import _library_work_id
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import refresh_library_for_scrape_target
    from app.library.store import load_library_index, save_library_index
    from app.scrape.models import ScrapeTarget

    heya_dir = mirror_root / "local" / "Heya Camp" / "Season 1"
    yuru_dir = mirror_root / "local" / "Yuru Camp" / "Season 1"
    heya_dir.mkdir(parents=True)
    yuru_dir.mkdir(parents=True)
    heya_strm = heya_dir / "S01E01.strm"
    yuru_strm = yuru_dir / "S01E01.strm"
    heya_strm.write_text("D:/media/heya-01.mkv", encoding="utf-8")
    yuru_strm.write_text("D:/media/yuru-01.mkv", encoding="utf-8")

    heya = ImportPlanItem(
        id="heya-01", plan_id="camp-plan", source="local",
        relative_path="[VCB-Studio] Yuru Camp/Heya Camp/01.mkv",
        real_path="D:/media/heya-01.mkv", resource_type="video", action="generate_strm",
        work_id="heya", work_title="Heya Camp", series_group="Yuru Camp",
        belongs_to_series="Yuru Camp", relation_type="spin_off",
        media_type="tv", show_type="anime_series", card_type="standalone",
        group_type="season", season_number=1, episode_number=1,
        target_dir=str(heya_dir), target_strm_path=str(heya_strm),
    )
    yuru = ImportPlanItem(
        id="yuru-01", plan_id="camp-plan", source="local",
        relative_path="[VCB-Studio] Yuru Camp/Yuru Camp/01.mkv",
        real_path="D:/media/yuru-01.mkv", resource_type="video", action="generate_strm",
        work_id="yuru", work_title="Yuru Camp", series_group="Yuru Camp",
        media_type="tv", show_type="anime_series", card_type="main_series",
        group_type="season", season_number=1, episode_number=1,
        target_dir=str(yuru_dir), target_strm_path=str(yuru_strm),
    )
    plan = ImportPlan(plan_id="camp-plan", source="local", status="confirmed", items=[heya, yuru])
    save_import_plan(plan)
    yuru_library_id = _library_work_id(yuru)
    save_library_index(LibraryIndex(works=[
        WorkIndex(work_id="heya", source="local", title="Heya Camp"),
        WorkIndex(
            work_id=yuru_library_id,
            source="local",
            title="摇曳露营△",
            episodes=[EpisodeIndex(episode_id="yuru-old", work_id=yuru_library_id)],
        ),
    ]))

    refresh_library_for_scrape_target(ScrapeTarget(
        scrape_target_id="heya-target", source="local", import_plan_id="camp-plan",
        work_id="heya", card_type="standalone", media_type="tv", group_type="season",
        local_season_number=1, target_dir=str(heya_dir), item_ids=["heya-01"],
    ))

    works = {work.work_id: work for work in load_library_index().works}
    heya_library_id = _library_work_id(heya)
    assert [item.work_id for item in works[heya_library_id].related_works] == [yuru_library_id]
    assert [item.work_id for item in works[yuru_library_id].related_works] == [heya_library_id]


def test_execute_scrape_rescan_after_uses_partial_refresh(tmp_path, monkeypatch):
    """手动刮削完成后应触发局部刷新，不再阻塞式全量重扫。"""
    from app.scrape.models import ScrapeTarget
    from app.scrape.service import execute_scrape

    calls = []
    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)
    monkeypatch.setattr("app.library.service.refresh_library_for_scrape_target", lambda target: calls.append(target) or {"mode": "partial"})
    monkeypatch.setattr("app.library.service.rescan_library", lambda source=None: (_ for _ in ()).throw(AssertionError("should not full rescan")))

    class Client:
        def get_tv_detail(self, tmdb_id):
            return {
                "name": "测试番剧",
                "first_air_date": "2024-01-01",
                "overview": "",
                "vote_average": 8,
                "genres": [],
                "production_companies": [],
                "episode_run_time": [24],
                "images": {},
            }

        def close(self):
            pass

    target = ScrapeTarget(
        scrape_target_id="t1",
        source="pan115",
        import_plan_id="p1",
        work_id="w1",
        card_type="standalone",
        media_type="tv",
        group_type="season",
        local_title="测试番剧",
        local_season_number=1,
        scrape_title="测试番剧",
        scrape_type="tv",
        target_dir=str(tmp_path),
    )

    result = execute_scrape(
        target=target,
        tmdb_id=123,
        tmdb_type="tv",
        tmdb_season_number=1,
        tmdb_client=Client(),
        include_episode=False,
        rescan_after=True,
    )

    assert result["scrape_target_id"] == "t1"
    assert calls == [target]


def test_execute_scrape_reports_failure_when_library_refresh_fails(tmp_path, monkeypatch):
    """元数据写完但作品未进入索引时，任务必须失败，不能显示为刮削完成。"""
    import pytest

    from app.scrape.models import ScrapeTarget
    from app.scrape.service import execute_scrape

    monkeypatch.setattr("app.scrape.service.upsert_scrape_map_item", lambda item: None)
    monkeypatch.setattr("app.scrape.service.save_failed_case", lambda case: None)
    monkeypatch.setattr(
        "app.library.service.refresh_library_for_scrape_target",
        lambda target: (_ for _ in ()).throw(RuntimeError("没有生成任何可展示作品")),
    )

    class Client:
        def get_tv_detail(self, tmdb_id):
            return {
                "name": "索引失败测试",
                "first_air_date": "2026-01-01",
                "overview": "",
                "vote_average": 8,
                "genres": [],
                "production_companies": [],
                "episode_run_time": [24],
                "images": {},
            }

        def close(self):
            pass

    target = ScrapeTarget(
        scrape_target_id="refresh-failed",
        source="baidu",
        import_plan_id="broken-plan",
        work_id="broken-work",
        card_type="standalone",
        media_type="tv",
        group_type="season",
        local_title="索引失败测试",
        local_season_number=1,
        scrape_title="索引失败测试",
        scrape_type="tv",
        target_dir=str(tmp_path),
    )

    with pytest.raises(RuntimeError, match="媒体库索引刷新失败"):
        execute_scrape(
            target=target,
            tmdb_id=123,
            tmdb_type="tv",
            tmdb_season_number=1,
            tmdb_client=Client(),
            include_episode=False,
            rescan_after=True,
        )


def test_duplicate_library_cards_keep_the_most_complete_refresh_result():
    """历史局部刷新遗留的同卡片记录必须收敛为最新完整的一张。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.library.service import _deduplicate_library_works

    def work(episode_count: int, source: str = "pan115"):
        return WorkIndex(
            work_id="series-a",
            source=source,
            episodes=[EpisodeIndex(episode_id=str(index)) for index in range(episode_count)],
        )

    normalized = _deduplicate_library_works([work(12), work(25), work(38), work(1, "baidu")])

    assert [(item.source, len(item.episodes)) for item in normalized] == [
        ("pan115", 38),
        ("baidu", 1),
    ]


def test_cached_main_season_and_special_cards_merge_by_mirror_series_root(tmp_path):
    """旧索引中同一镜像作品根的正季与特别篇应收敛到正季卡片。"""
    from app.library.models import EpisodeIndex, SeasonIndex, WorkIndex
    from app.library.service import _deduplicate_library_works

    series_root = tmp_path / "莉可丽丝"
    main = WorkIndex(
        work_id="main-card",
        title="莉可丽丝",
        source="pan115",
        media_type="tv",
        card_type="main_series",
        dir_path=str(series_root / "Season 1"),
        seasons=[SeasonIndex(
            season_id="main-season", work_id="main-card", season_number=1,
            group_type="season", label="第1季", episode_count=1,
        )],
        episodes=[EpisodeIndex(
            episode_id="main-episode", work_id="main-card", source="pan115",
            season_number=1, episode_number=1, group_type="season",
        )],
    )
    special = WorkIndex(
        work_id="special-card",
        title="莉可丽丝",
        source="pan115",
        media_type="tv",
        card_type="main_series",
        dir_path=str(series_root / "Season 0"),
        seasons=[SeasonIndex(
            season_id="special-season", work_id="special-card", season_number=0,
            group_type="special", label="特别篇", episode_count=1,
        )],
        episodes=[EpisodeIndex(
            episode_id="special-episode", work_id="special-card", source="pan115",
            season_number=0, episode_number=5, group_type="special",
        )],
    )
    other = WorkIndex(
        work_id="other-card",
        title="其他作品",
        source="pan115",
        media_type="tv",
        card_type="main_series",
        dir_path=str(tmp_path / "其他作品" / "Season 0"),
    )

    normalized = _deduplicate_library_works([main, special, other])

    assert [work.work_id for work in normalized] == ["main-card", "other-card"]
    merged = normalized[0]
    assert {(season.group_type, season.season_number) for season in merged.seasons} == {
        ("season", 1), ("special", 0),
    }
    assert {episode.work_id for episode in merged.episodes} == {"main-card"}
    assert {season.work_id for season in merged.seasons} == {"main-card"}


def test_merge_library_indexes_merges_matching_seasonal_sources():
    """同一新番分散在多个来源时应合并为一张卡。"""
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.service import _merge_library_indexes

    def make_work(source: str, episode_numbers: list[int]) -> WorkIndex:
        episodes = [
            EpisodeIndex(
                episode_id=f"{source}-{number}",
                work_id=f"{source}-work",
                source=source,
                season_number=1,
                episode_number=number,
                group_type="season",
                strm_path=f"{source}-{number}.strm",
            )
            for number in episode_numbers
        ]
        return WorkIndex(
            work_id=f"{source}-work",
            title="零散存储测试番剧",
            year=2026,
            show_type="anime_series",
            media_type="tv",
            source=source,
            sources=[source],
            import_scope="seasonal",
            seasons=[SeasonIndex(
                season_number=1,
                group_type="season",
                episode_count=len(episodes),
                tmdb_id=123456,
                tmdb_type="tv",
            )],
            episodes=episodes,
        )

    merged = _merge_library_indexes([
        LibraryIndex(works=[make_work("pan115", [1, 2, 3])]),
        LibraryIndex(works=[make_work("local", [4])]),
        LibraryIndex(works=[make_work("baidu", [5])]),
    ])

    assert len(merged.works) == 1
    assert merged.works[0].sources == ["pan115", "baidu", "local"]
    assert [episode.episode_number for episode in merged.works[0].episodes] == [1, 2, 3, 4, 5]


def test_merge_library_indexes_keeps_completed_sources_as_separate_cards():
    """已完结作品即使标题和 TMDB 相同也必须按来源分卡。"""
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.service import _merge_library_indexes

    def make_work(source: str) -> WorkIndex:
        return WorkIndex(
            work_id=f"{source}-completed", title="已完结跨来源副本", year=2025,
            show_type="anime_series", media_type="tv", source=source, sources=[source],
            import_scope="", seasons=[SeasonIndex(season_number=1, tmdb_id=987654, tmdb_type="tv")],
            episodes=[EpisodeIndex(
                episode_id=f"{source}-1", source=source, season_number=1,
                episode_number=1, group_type="season",
            )],
        )

    merged = _merge_library_indexes([
        LibraryIndex(works=[make_work("pan115")]),
        LibraryIndex(works=[make_work("local")]),
    ])

    assert [(work.source, work.work_id) for work in merged.works] == [
        ("pan115", "pan115-completed"),
        ("local", "local-completed"),
    ]


def test_seasonal_duplicate_episode_uses_stable_source_priority():
    """同一集存在多来源副本时只保留固定优先来源。"""
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.service import _merge_library_indexes

    def make_work(source: str) -> WorkIndex:
        return WorkIndex(
            work_id=f"{source}-work",
            title="来源优先级测试番剧",
            year=2026,
            show_type="anime_series",
            media_type="tv",
            source=source,
            sources=[source],
            import_scope="seasonal",
            seasons=[SeasonIndex(
                season_number=1,
                group_type="season",
                episode_count=1,
                tmdb_id=654321,
                tmdb_type="tv",
            )],
            episodes=[EpisodeIndex(
                episode_id=f"{source}-1",
                source=source,
                season_number=1,
                episode_number=1,
                group_type="season",
                strm_path=f"{source}-1.strm",
            )],
        )

    merged = _merge_library_indexes([
        LibraryIndex(works=[make_work("baidu")]),
        LibraryIndex(works=[make_work("pan115")]),
    ])

    assert len(merged.works) == 1
    assert merged.works[0].sources == ["pan115", "baidu"]
    assert merged.works[0].episodes[0].source == "pan115"
    assert merged.works[0].source_locations == {
        "pan115": {"episode_id": "pan115-1", "strm_path": "pan115-1.strm"},
        "baidu": {"episode_id": "baidu-1", "strm_path": "baidu-1.strm"},
    }


def test_seasonal_work_merges_shared_canonical_work_id_without_tmdb():
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import _merge_library_indexes

    def make_work(source: str, episode_number: int) -> WorkIndex:
        return WorkIndex(
            work_id="canonical-seasonal-work",
            title="Canonical seasonal anime",
            source=source,
            sources=[source],
            import_scope="seasonal",
            episodes=[EpisodeIndex(
                episode_id=f"{source}-{episode_number}",
                source=source,
                season_number=1,
                episode_number=episode_number,
                group_type="season",
            )],
        )

    merged = _merge_library_indexes([
        LibraryIndex(works=[make_work("pan115", 1)]),
        LibraryIndex(works=[make_work("local", 2)]),
    ])

    assert len(merged.works) == 1
    assert merged.works[0].sources == ["pan115", "local"]


def test_seasonal_same_title_and_year_without_strong_identity_stays_separate():
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import _merge_library_indexes

    def make_work(source: str, work_id: str) -> WorkIndex:
        return WorkIndex(
            work_id=work_id,
            title="Same title is not identity",
            year=2026,
            media_type="tv",
            source=source,
            sources=[source],
            import_scope="seasonal",
            episodes=[EpisodeIndex(
                episode_id=f"{source}-1",
                source=source,
                season_number=1,
                episode_number=1,
                group_type="season",
            )],
        )

    merged = _merge_library_indexes([
        LibraryIndex(works=[make_work("pan115", "pan-directory")]),
        LibraryIndex(works=[make_work("local", "local-directory")]),
    ])

    assert len(merged.works) == 2


def test_single_card_collects_sources_from_its_episodes():
    """同一作品计划内混入本地和网盘剧集时，仍是一张卡并列出全部来源。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.library.service import _deduplicate_library_works

    work = WorkIndex(
        work_id="mixed-work",
        title="混合来源新番",
        show_type="anime_series",
        media_type="tv",
        source="pan115",
        import_scope="seasonal",
        episodes=[
            EpisodeIndex(episode_id="one", source="pan115", season_number=1, episode_number=1),
            EpisodeIndex(episode_id="two", source="local", season_number=1, episode_number=2),
        ],
    )

    merged = _deduplicate_library_works([work])

    assert len(merged) == 1
    assert merged[0].sources == ["pan115", "local"]


def test_source_refresh_replaces_only_that_sources_episodes_in_mixed_card(monkeypatch):
    """单来源重扫不能丢掉混合卡片中来自其他来源的剧集。"""
    from app.library import service
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex

    def episode(source: str, number: int) -> EpisodeIndex:
        return EpisodeIndex(
            episode_id=f"{source}-{number}", source=source, season_number=1,
            episode_number=number, group_type="season", strm_path=f"{source}-{number}.strm",
        )

    existing = LibraryIndex(works=[WorkIndex(
        work_id="pan-work", title="混合来源作品", year=2026,
        show_type="anime_series", media_type="tv", source="pan115",
        sources=["pan115", "local"], import_scope="seasonal",
        seasons=[SeasonIndex(season_number=1, group_type="season", tmdb_id=654321, tmdb_type="tv")],
        episodes=[episode("pan115", 1), episode("local", 4), episode("local", 6)],
    )])
    rebuilt_local = LibraryIndex(works=[WorkIndex(
        work_id="local-work", title="混合来源作品", year=2026,
        show_type="anime_series", media_type="tv", source="local", sources=["local"],
        import_scope="seasonal",
        seasons=[SeasonIndex(season_number=1, group_type="season", tmdb_id=654321, tmdb_type="tv")],
        episodes=[episode("local", 4), episode("local", 5)],
    )])
    monkeypatch.setattr(service, "load_library_index", lambda: existing)

    result = service._replace_source_in_existing_index("local", rebuilt_local)

    assert len(result.works) == 1
    assert [(item.episode_number, item.source) for item in result.works[0].episodes] == [
        (1, "pan115"), (4, "local"), (5, "local"),
    ]


def test_tracking_work_refresh_removes_stale_source_episodes_from_mixed_card():
    """追更局部刷新必须先剥离该来源旧贡献，再合并当前剧集。"""
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import _replace_library_works

    def episode(source: str, number: int) -> EpisodeIndex:
        return EpisodeIndex(
            episode_id=f"{source}-{number}", source=source, season_number=1,
            episode_number=number, group_type="season",
        )

    current = LibraryIndex(
        works=[WorkIndex(
            work_id="shared-card", title="局部追更作品", source="pan115",
            sources=["pan115", "local"], import_scope="seasonal",
            source_episode_counts={"pan115": 3, "local": 2},
            episodes=[episode("pan115", 1), episode("local", 2), episode("local", 3)],
        )],
        source_summary={"local": {"work_count": 1, "episode_count": 2}},
    )
    replacement = WorkIndex(
        work_id="shared-card", title="局部追更作品", source="local",
        sources=["local"], import_scope="seasonal",
        episodes=[episode("local", 2), episode("local", 4)],
    )

    _replace_library_works(current, "local", {"shared-card"}, [replacement])

    assert len(current.works) == 1
    assert [(item.source, item.episode_number) for item in current.works[0].episodes] == [
        ("pan115", 1), ("local", 2), ("local", 4),
    ]
    assert current.source_summary["local"]["work_count"] == 1
    assert current.source_summary["local"]["episode_count"] == 2


def test_partial_refresh_replaces_both_raw_and_resolved_card_ids(tmp_path, monkeypatch):
    """目录原始 ID 与最终聚合 ID 不同时，也不能留下旧卡片。"""
    _patch_data_dirs(monkeypatch, tmp_path)

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.library import service
    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.store import load_library_index, save_library_index
    from app.scrape.models import ScrapeTarget

    plan = ImportPlan(
        plan_id="p-resolved",
        source="pan115",
        status="confirmed",
        items=[ImportPlanItem(
            id="item-a",
            plan_id="p-resolved",
            source="pan115",
            relative_path="动画/Show A/01.mkv",
            real_path="H:/media/show-a-01.mkv",
            resource_type="video",
            action="generate_strm",
            work_id="raw-work-a",
            work_title="Show A",
            media_type="tv",
            card_type="main_series",
            group_type="season",
            season_number=1,
            episode_number=1,
        )],
    )
    save_library_index(LibraryIndex(works=[WorkIndex(
        work_id="resolved-series-a",
        source="pan115",
        episodes=[EpisodeIndex(episode_id="old")],
    )]))
    monkeypatch.setattr(service, "load_latest_confirmed_import_plan", lambda source: plan)
    monkeypatch.setattr(service, "load_scrape_map", lambda: None)
    monkeypatch.setattr(service, "_scan_plan_items_directly", lambda *_: None)
    monkeypatch.setattr(service, "build_library_index", lambda *_: LibraryIndex(works=[WorkIndex(
        work_id="resolved-series-a",
        source="pan115",
        episodes=[EpisodeIndex(episode_id=str(index)) for index in range(2)],
    )]))

    service.refresh_library_for_scrape_target(ScrapeTarget(
        scrape_target_id="target-a",
        source="pan115",
        import_plan_id="p-resolved",
        work_id="raw-work-a",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        item_ids=["item-a"],
    ))

    works = load_library_index().works
    assert len(works) == 1
    assert works[0].work_id == "resolved-series-a"
    assert len(works[0].episodes) == 2


def test_partial_refresh_uses_target_plan_when_same_source_has_multiple_presets(monkeypatch):
    """同一来源同时导入新番与已完结动画时，局部刷新必须使用目标所属计划。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.library import service
    from app.library.models import LibraryIndex, WorkIndex
    from app.scrape.models import ScrapeTarget

    completed_plan = ImportPlan(
        plan_id="completed-plan",
        source="baidu",
        status="executed",
        import_scope="",
        items=[ImportPlanItem(
            id="completed-item",
            plan_id="completed-plan",
            source="baidu",
            resource_type="video",
            action="generate_strm",
            work_id="completed-work",
            work_title="已完结作品",
            media_type="tv",
            card_type="standalone",
            group_type="season",
            season_number=1,
            episode_number=1,
        )],
    )
    seasonal_plan = ImportPlan(
        plan_id="seasonal-plan",
        source="baidu",
        status="executed",
        import_scope="seasonal",
        items=[ImportPlanItem(id="seasonal-item", plan_id="seasonal-plan", source="baidu")],
    )
    loaded_plan_ids = []

    def load_by_id(plan_id=None, source=None):
        loaded_plan_ids.append(plan_id)
        return completed_plan if plan_id == completed_plan.plan_id else None

    monkeypatch.setattr(service, "load_library_index", lambda: LibraryIndex())
    monkeypatch.setattr(service, "load_import_plan", load_by_id)
    monkeypatch.setattr(service, "load_latest_confirmed_import_plan", lambda source: seasonal_plan)
    monkeypatch.setattr(service, "load_scrape_map", lambda: None)
    monkeypatch.setattr(service, "_scan_plan_items_directly", lambda plan, targets: plan)
    monkeypatch.setattr(
        service,
        "build_library_index",
        lambda plan, *_: LibraryIndex(works=[WorkIndex(
            work_id=plan.items[0].work_id,
            source=plan.source,
            title=plan.items[0].work_title,
        )]),
    )
    monkeypatch.setattr(service, "save_library_index", lambda index: "library-index.json")

    result = service.refresh_library_for_scrape_target(ScrapeTarget(
        scrape_target_id="completed-target",
        source="baidu",
        import_plan_id=completed_plan.plan_id,
        work_id="completed-work",
        item_ids=["completed-item"],
    ))

    assert loaded_plan_ids == ["completed-plan"]
    assert result["work_count"] == 1


def test_library_read_repairs_duplicate_cards_in_persisted_cache(tmp_path, monkeypatch):
    """旧缓存即使已含重复卡片，读取媒体库也必须持久化修复。"""
    _patch_data_dirs(monkeypatch, tmp_path)

    from app.library.models import EpisodeIndex, LibraryIndex, WorkIndex
    from app.library.service import get_library
    from app.library.store import load_library_index, save_library_index

    save_library_index(LibraryIndex(version=2, works=[
        WorkIndex(work_id="series-a", source="pan115", episodes=[EpisodeIndex(episode_id="old")]),
        WorkIndex(work_id="series-a", source="pan115", episodes=[EpisodeIndex(episode_id="new-1"), EpisodeIndex(episode_id="new-2")]),
    ]))

    payload = get_library(compact=True)

    assert payload["summary"]["work_count"] == 1
    assert len(load_library_index().works) == 1
    assert len(load_library_index().works[0].episodes) == 2


def test_manual_scrape_rekeys_rebuilt_work_to_current_card_in_place(tmp_path, monkeypatch):
    """详情页手动刮削即使识别身份变化，也只能更新当前卡片，不能追加第二张。"""
    _patch_data_dirs(monkeypatch, tmp_path)

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.library import service
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.store import load_library_index, save_library_index
    from app.scrape.models import ScrapeTarget

    plan = ImportPlan(
        plan_id="manual-replace-plan",
        source="local",
        status="executed",
        items=[ImportPlanItem(
            id="season-2-item",
            plan_id="manual-replace-plan",
            source="local",
            resource_type="video",
            action="generate_strm",
            work_id="wrong-raw-id",
            work_title="旧错误标题",
            series_group="旧错误标题",
            media_type="tv",
            card_type="main_series",
            group_type="season",
            season_number=2,
            episode_number=1,
        )],
    )
    current = WorkIndex(
        work_id="current-card-id",
        source="local",
        title="旧错误标题",
        last_played="2026-07-20T12:00:00+08:00",
        related_works=[],
        episodes=[EpisodeIndex(episode_id="old-episode", work_id="current-card-id")],
    )
    save_library_index(LibraryIndex(works=[current]))
    monkeypatch.setattr(service, "load_import_plan", lambda **_: plan)
    monkeypatch.setattr(service, "load_scrape_map", lambda: None)
    monkeypatch.setattr(service, "_scan_plan_items_directly", lambda *_: None)
    monkeypatch.setattr(service, "rebuild_related_works_for_plan", lambda *_: None)
    monkeypatch.setattr(service, "build_library_index", lambda *_: LibraryIndex(works=[WorkIndex(
        work_id="newly-derived-id",
        source="local",
        title="正确中文标题",
        seasons=[SeasonIndex(
            season_id="new-season-2",
            work_id="newly-derived-id",
            season_number=2,
            group_type="season",
        )],
        episodes=[EpisodeIndex(
            episode_id="new-episode",
            work_id="newly-derived-id",
            season_number=2,
            episode_number=1,
        )],
    )]))

    result = service.refresh_library_for_scrape_target(
        ScrapeTarget(
            scrape_target_id="season-2-target",
            source="local",
            import_plan_id=plan.plan_id,
            work_id="wrong-raw-id",
            item_ids=["season-2-item"],
            local_season_number=2,
            group_type="season",
        ),
        library_work_id="current-card-id",
    )

    assert result["mode"] == "partial"
    works = load_library_index().works
    assert len(works) == 1
    assert works[0].work_id == "current-card-id"
    assert works[0].title == "正确中文标题"
    assert works[0].last_played == "2026-07-20T12:00:00+08:00"
    assert {season.work_id for season in works[0].seasons} == {"current-card-id"}
    assert {episode.work_id for episode in works[0].episodes} == {"current-card-id"}


def test_manual_scrape_anchor_rejects_full_rescan_fallback(monkeypatch):
    """原位模式缺少计划时必须失败关闭，不能退化成可能换卡的全量重扫。"""
    import pytest

    from app.library import service
    from app.library.models import LibraryIndex, WorkIndex
    from app.scrape.models import ScrapeTarget

    monkeypatch.setattr(service, "load_library_index", lambda: LibraryIndex(works=[
        WorkIndex(work_id="current-card", source="local", title="当前卡片"),
    ]))
    monkeypatch.setattr(service, "load_import_plan", lambda **_: None)
    monkeypatch.setattr(service, "load_latest_confirmed_import_plan", lambda *_: None)
    monkeypatch.setattr(
        service,
        "rescan_library",
        lambda *_: (_ for _ in ()).throw(AssertionError("原位模式禁止全量重扫")),
    )

    with pytest.raises(RuntimeError, match="原位手动刮削.*拒绝全量重扫"):
        service.refresh_library_for_scrape_target(
            ScrapeTarget(
                scrape_target_id="missing-plan-target",
                source="local",
                import_plan_id="missing-plan",
                work_id="raw-work",
            ),
            library_work_id="current-card",
        )


def test_without_primary_source_keeps_hidden_duplicate_source_shell():
    """完全重叠的次要来源不能因主来源刷新而丢失标签和文件夹定位。"""
    from app.library.models import EpisodeIndex, WorkIndex
    from app.library.service import _without_source_contribution

    mixed = WorkIndex(
        work_id="mixed-card",
        source="pan115",
        sources=["pan115", "local"],
        import_scope="seasonal",
        episodes=[EpisodeIndex(
            episode_id="pan-episode",
            work_id="mixed-card",
            source="pan115",
            season_number=1,
            episode_number=1,
            strm_path="D:/mirror/115/show/S01E01.strm",
        )],
        source_locations={
            "pan115": {"episode_id": "pan-episode", "strm_path": "D:/mirror/115/show/S01E01.strm"},
            "local": {"episode_id": "local-episode", "strm_path": "D:/mirror/local/show/S01E01.strm"},
        },
    )

    retained = _without_source_contribution(mixed, "pan115")

    assert retained is not None
    assert retained.source == "local"
    assert retained.sources == ["local"]
    assert retained.episodes == []
    assert set(retained.source_locations) == {"local"}


def test_manual_scrape_secondary_source_keeps_new_metadata_authoritative(tmp_path, monkeypatch):
    """刮削次要来源时新元数据必须覆盖旧主来源，同时保留跨来源剧集和定位。"""
    _patch_data_dirs(monkeypatch, tmp_path)

    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.library import service
    from app.library.models import EpisodeIndex, LibraryIndex, SeasonIndex, WorkIndex
    from app.library.store import load_library_index, save_library_index
    from app.scrape.models import ScrapeTarget

    plan = ImportPlan(
        plan_id="secondary-plan",
        source="local",
        status="executed",
        import_scope="seasonal",
        items=[ImportPlanItem(
            id="local-episode",
            plan_id="secondary-plan",
            source="local",
            resource_type="video",
            action="generate_strm",
            work_id="local-raw",
            canonical_work_id="mixed-card",
            media_type="tv",
            group_type="season",
            season_number=1,
            episode_number=1,
        )],
    )
    current = WorkIndex(
        work_id="mixed-card",
        title="旧错误标题",
        plot="旧简介",
        source="pan115",
        sources=["pan115", "local"],
        import_scope="seasonal",
        seasons=[SeasonIndex(season_number=1, group_type="season", scraped=True)],
        episodes=[EpisodeIndex(
            episode_id="pan-episode",
            work_id="mixed-card",
            source="pan115",
            season_number=1,
            episode_number=1,
            strm_path="D:/mirror/115/show/S01E01.strm",
        )],
        source_locations={
            "pan115": {"episode_id": "pan-episode", "strm_path": "D:/mirror/115/show/S01E01.strm"},
            "local": {"episode_id": "local-episode", "strm_path": "D:/mirror/local/show/S01E01.strm"},
        },
    )
    save_library_index(LibraryIndex(works=[current]))
    monkeypatch.setattr(service, "load_import_plan", lambda **_: plan)
    monkeypatch.setattr(service, "load_scrape_map", lambda: None)
    monkeypatch.setattr(service, "_scan_plan_items_directly", lambda *_: None)
    monkeypatch.setattr(service, "rebuild_related_works_for_plan", lambda *_: None)
    monkeypatch.setattr(service, "build_library_index", lambda *_: LibraryIndex(works=[WorkIndex(
        work_id="mixed-card",
        title="正确中文标题",
        plot="新简介",
        source="local",
        sources=["local"],
        import_scope="seasonal",
        seasons=[SeasonIndex(season_number=1, group_type="season", scraped=True)],
        episodes=[EpisodeIndex(
            episode_id="local-episode",
            work_id="mixed-card",
            source="local",
            season_number=1,
            episode_number=1,
            strm_path="D:/mirror/local/show/S01E01.strm",
        )],
        source_locations={
            "local": {"episode_id": "local-episode", "strm_path": "D:/mirror/local/show/S01E01.strm"},
        },
    )]))

    service.refresh_library_for_scrape_target(
        ScrapeTarget(
            scrape_target_id="local-target",
            source="local",
            import_plan_id=plan.plan_id,
            work_id="local-raw",
            item_ids=["local-episode"],
            local_season_number=1,
            group_type="season",
        ),
        library_work_id="mixed-card",
    )

    refreshed = load_library_index().works[0]
    assert refreshed.title == "正确中文标题"
    assert refreshed.plot == "新简介"
    assert refreshed.sources == ["pan115", "local"]
    assert set(refreshed.source_locations) == {"pan115", "local"}
    assert refreshed.episodes[0].source == "pan115"
    assert refreshed.source_episode_counts == {"pan115": 1, "local": 1}
    assert load_library_index().source_summary["local"]["work_count"] == 1
    assert load_library_index().source_summary["local"]["episode_count"] == 1
