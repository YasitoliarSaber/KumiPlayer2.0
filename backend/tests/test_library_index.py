# -*- coding: utf-8 -*-
"""M09 LibraryIndex 构建测试"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_TEST_MIRROR = Path(__file__).parent.parent / "data" / "test_mirror"


def _cleanup():
    if _TEST_MIRROR.exists():
        shutil.rmtree(_TEST_MIRROR)


def _make_item(item_id, work_id="w1", work_title="CLANNAD", year=2007,
               group_type="season", season_number=1, episode_number=1,
               series_group="CLANNAD", card_type="main_series",
               target_strm_path=None, source="pan115", relative_path=None, real_path=None,
               **kwargs):
    from app.import_plan.models import ImportPlanItem
    if target_strm_path is None:
        sn = season_number or 0
        target_strm_path = f"mirror/{source}/{work_title}/S{sn}/{item_id}.strm"
    if relative_path is None:
        relative_path = f"动画/{work_title}/{item_id}.mkv"
    if real_path is None:
        real_path = f"H:\\test\\{item_id}.mkv"
    return ImportPlanItem(
        id=item_id, plan_id="p1", raw_file_id=f"r-{item_id}", source=source,
        relative_path=relative_path, real_path=real_path,
        resource_type="video", action="generate_strm",
        work_id=work_id, work_title=work_title, year=year,
        group_type=group_type, season_number=season_number, episode_number=episode_number,
        series_group=series_group, card_type=card_type,
        target_strm_path=target_strm_path,
        **kwargs,
    )


def _make_plan(items, source="pan115"):
    from app.import_plan.models import ImportPlan
    return ImportPlan(plan_id="p1", source=source, status="confirmed", items=items)


def test_build_work_index():
    """从 ImportPlan 生成 WorkIndex"""
    from app.library.index import build_library_index, rebuild_related_works_for_plan

    items = [
        _make_item("v1", season_number=1, episode_number=1),
        _make_item("v2", season_number=1, episode_number=2),
    ]
    index = build_library_index(_make_plan(items))
    assert len(index.works) == 1
    assert index.works[0].title == "CLANNAD"
    assert index.works[0].year == 2007


def test_same_tmdb_series_copies_remain_separate_cards():
    """同一来源的两个目录副本即使 TMDB 相同，也必须保留两张卡。"""
    from app.library.index import build_library_index, rebuild_related_works_for_plan
    from app.library.scanner import MirrorFile, MirrorScanResult

    copied = _make_item(
        "copy-episode", work_id="raw-copy", work_title="测试新番 (2026) (1)",
        series_group="测试新番 (2026) (1)", tmdb_hint_id=12345,
        tmdb_hint_type="tv", relative_path="动画/测试新番 (1)/copy-episode.mkv",
    )
    original = _make_item(
        "original-episode", work_id="raw-original", work_title="测试新番",
        series_group="测试新番", tmdb_hint_id=12345, tmdb_hint_type="tv",
        relative_path="动画/测试新番/original-episode.mkv",
    )

    scan = MirrorScanResult(strm_files=[
        MirrorFile(
            source="pan115",
            strm_path=str(Path(copied.target_strm_path).resolve()),
            real_path=copied.real_path,
        ),
        MirrorFile(
            source="pan115",
            strm_path=str(Path(original.target_strm_path).resolve()),
            real_path=original.real_path,
        ),
    ])
    index = build_library_index(_make_plan([copied, original]), scan_result=scan)

    assert len(index.works) == 2
    assert sorted(len(work.episodes) for work in index.works) == [1, 1]


def test_same_confirmed_tmdb_tv_id_keeps_alias_directories_separate(tmp_path):
    """不同目录即使确认到同一 TMDB TV，也不跨目录合并卡片。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorFile, MirrorScanResult
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    items = []
    files = []
    for season, work_id, title, group in (
        (1, "jujutsu-cn", "咒术回战", "咒术回战"),
        (2, "jujutsu-en", "Jujutsu_Kaisen", "Jujutsu_Kaisen"),
    ):
        strm = tmp_path / group / f"Season {season}" / f"S{season:02d}E01.strm"
        strm.parent.mkdir(parents=True, exist_ok=True)
        strm.write_text(f"H:\\test\\s{season}e1.mkv", encoding="utf-8")
        items.append(_make_item(
            f"s{season}e1", work_id=work_id, work_title=title,
            series_group=group, season_number=season, episode_number=1,
            target_strm_path=str(strm), relative_path=f"动画/{group}/S{season:02d}E01.mkv",
        ))
        files.append(MirrorFile(
            source="pan115", strm_path=str(strm.resolve()),
            real_path=f"H:\\test\\s{season}e1.mkv",
        ))

    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="jujutsu-s1", work_id="jujutsu-cn", source="pan115",
            import_plan_id="p1", card_type="main_series", media_type="tv",
            series_group="咒术回战", local_title="咒术回战", local_season_number=1,
            tmdb_id=95479, tmdb_type="tv", tmdb_season_number=1, selected_by="auto",
        ),
        ScrapeMapItem(
            scrape_target_id="jujutsu-s2", work_id="jujutsu-en", source="pan115",
            import_plan_id="p1", card_type="main_series", media_type="tv",
            series_group="Jujutsu_Kaisen", local_title="Jujutsu_Kaisen", local_season_number=2,
            tmdb_id=95479, tmdb_type="tv", tmdb_season_number=2, selected_by="auto",
        ),
    ])

    index = build_library_index(
        _make_plan(items), scrape_map=scrape_map,
        scan_result=MirrorScanResult(strm_files=files),
    )

    assert len(index.works) == 2
    assert sorted(
        tuple(season.season_number for season in work.seasons)
        for work in index.works
    ) == [(1,), (2,)]


def test_same_confirmed_tmdb_movie_id_keeps_release_copies_separate(tmp_path):
    """同一电影的不同目录版本允许展示成两张独立卡片。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorFile, MirrorScanResult
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    items = []
    files = []
    for suffix, work_id, title in (
        ("cn", "jujutsu-movie-cn", "剧场版：咒术回战0"),
        ("en", "jujutsu-movie-en", "Jujutsu Kaisen 0"),
    ):
        strm = tmp_path / title / f"{suffix}.strm"
        strm.parent.mkdir(parents=True, exist_ok=True)
        strm.write_text(f"H:\\test\\{suffix}.mkv", encoding="utf-8")
        items.append(_make_item(
            f"movie-{suffix}", work_id=work_id, work_title=title,
            title=title, year=2021, group_type="movie", card_type="standalone",
            media_type="movie", season_number=None, episode_number=None,
            series_group="咒术回战", target_strm_path=str(strm),
            relative_path=f"动画电影/{title}/{suffix}.mkv",
        ))
        files.append(MirrorFile(
            source="pan115", strm_path=str(strm.resolve()),
            real_path=f"H:\\test\\{suffix}.mkv",
        ))

    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id=f"movie-{suffix}", work_id=work_id, source="pan115",
            import_plan_id="p1", card_type="standalone", media_type="movie",
            series_group="咒术回战", local_title=title, local_year=2021,
            tmdb_id=810693, tmdb_type="movie", selected_by="auto",
        )
        for suffix, work_id, title in (
            ("cn", "jujutsu-movie-cn", "剧场版：咒术回战0"),
            ("en", "jujutsu-movie-en", "Jujutsu Kaisen 0"),
        )
    ])

    index = build_library_index(
        _make_plan(items), scrape_map=scrape_map,
        scan_result=MirrorScanResult(strm_files=files),
    )

    assert len(index.works) == 2
    assert sorted(len(work.episodes) for work in index.works) == [1, 1]


def test_seasonal_import_scope_is_preserved_on_every_library_work():
    """目录树标记为新番后，所有生成的作品卡片都必须保留该导入范围。"""
    from app.library.index import build_library_index

    plan = _make_plan([
        _make_item("seasonal-1", work_id="w-seasonal-1"),
        _make_item(
            "seasonal-2", work_id="w-seasonal-2", work_title="另一部新番",
            series_group="另一部新番",
        ),
    ])
    plan.import_scope = "seasonal"

    index = build_library_index(plan)

    assert len(index.works) == 2
    assert {work.import_scope for work in index.works} == {"seasonal"}


def test_parse_nfo_normalizes_tmdb_relative_episode_thumbnail(tmp_path):
    """旧 NFO 中的 TMDB 相对剧照路径应恢复为可访问的完整 URL。"""
    from app.library.index import _parse_nfo

    nfo = tmp_path / "S01E01.nfo"
    nfo.write_text(
        "<episodedetails><title>第一集</title><thumb>/abc123.jpg</thumb></episodedetails>",
        encoding="utf-8",
    )

    parsed = _parse_nfo(nfo)

    assert parsed["thumb"] == "https://image.tmdb.org/t/p/original/abc123.jpg"


def test_build_season_index():
    """SeasonIndex 从 episodes 构建（需要 .strm 存在）"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile

    items = [
        _make_item("v1", season_number=1, episode_number=1),
        _make_item("v2", season_number=2, episode_number=1),
    ]
    # 创建 mock scan result（.strm 存在）
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(Path(items[0].target_strm_path).resolve()), real_path="H:\\test.mkv"),
        MirrorFile(source="pan115", strm_path=str(Path(items[1].target_strm_path).resolve()), real_path="H:\\test.mkv"),
    ])
    index = build_library_index(_make_plan(items), scan_result=scan)
    assert len(index.works[0].seasons) == 2
    labels = [s.label for s in index.works[0].seasons]
    assert "第1季" in labels
    assert "第2季" in labels


def test_specials_share_main_series_card_when_mirror_root_is_shared(tmp_path):
    """原始目录不同但已整理到同一镜像作品根的特别篇应与正季合成一张卡。"""
    from dataclasses import replace

    from app.library.index import _library_work_id, build_library_index
    from app.library.scanner import MirrorFile, MirrorScanResult

    series_root = tmp_path / "莉可丽丝"
    season_strm = series_root / "Season 1" / "S01E01.strm"
    special_strm = series_root / "Season 0" / "S00E05.strm"
    season_strm.parent.mkdir(parents=True)
    special_strm.parent.mkdir(parents=True)
    season_strm.write_text("H:\\test\\main.mkv", encoding="utf-8")
    special_strm.write_text("H:\\test\\special.mkv", encoding="utf-8")

    season = _make_item(
        "lycoris-main",
        work_id="lycoris-main-source",
        work_title="莉可丽丝",
        series_group="莉可丽丝",
        relative_path="动画/莉可丽丝 (2022)/Season 1/main.mkv",
        target_dir=str(season_strm.parent),
        target_strm_path=str(season_strm),
    )
    special = _make_item(
        "lycoris-special",
        work_id="lycoris-special-source",
        work_title="莉可丽丝",
        series_group="莉可丽丝",
        relative_path="动画/莉可丽丝：友谊是时间的窃贼/Season 0/special.mkv",
        group_type="special",
        season_number=0,
        episode_number=None,
        special_number=5,
        target_dir=str(special_strm.parent),
        target_strm_path=str(special_strm),
    )
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(season_strm.resolve()), real_path="H:\\test\\main.mkv"),
        MirrorFile(source="pan115", strm_path=str(special_strm.resolve()), real_path="H:\\test\\special.mkv"),
    ])
    expected_main_work_id = _library_work_id(replace(season, target_dir="", target_strm_path=""))

    index = build_library_index(_make_plan([season, special]), scan_result=scan)

    assert len(index.works) == 1
    assert index.works[0].work_id == expected_main_work_id
    assert {(item.group_type, item.season_number) for item in index.works[0].seasons} == {
        ("season", 1),
        ("special", 0),
    }


def test_merged_specials_with_duplicate_numbers_keep_unique_display_positions(tmp_path):
    """旧计划或跨子目录合卡后，特别篇不能在详情页占用同一个集号。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorFile, MirrorScanResult

    series_root = tmp_path / "同一作品" / "Season 0"
    first_strm = series_root / "Season1 SP01.strm"
    second_strm = series_root / "Season2 SP01.strm"
    series_root.mkdir(parents=True)
    first_strm.write_text("H:\\test\\sp1.mkv", encoding="utf-8")
    second_strm.write_text("H:\\test\\sp2.mkv", encoding="utf-8")
    items = [
        _make_item(
            "sp1", work_id="season-1", work_title="同一作品", series_group="同一作品",
            relative_path="动画/同一作品/Season 1/SP01.mkv", group_type="special",
            season_number=0, episode_number=None, special_number=1,
            target_dir=str(series_root), target_strm_path=str(first_strm),
        ),
        _make_item(
            "sp2", work_id="season-2", work_title="同一作品", series_group="同一作品",
            relative_path="动画/同一作品/Season 2/SP01.mkv", group_type="special",
            season_number=0, episode_number=None, special_number=1,
            target_dir=str(series_root), target_strm_path=str(second_strm),
        ),
    ]
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(first_strm.resolve()), real_path="H:\\test\\sp1.mkv"),
        MirrorFile(source="pan115", strm_path=str(second_strm.resolve()), real_path="H:\\test\\sp2.mkv"),
    ])

    index = build_library_index(_make_plan(items), scan_result=scan)

    specials = [episode for episode in index.works[0].episodes if episode.group_type == "special"]
    assert len(specials) == 2
    assert [episode.episode_number for episode in specials] == [1, 2]


def test_clannad_aggregation():
    """CLANNAD 同 series_group 聚合为一个 main_series 卡片"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile

    items = [
        _make_item("v1", work_id="w-clannad-2007", season_number=1, episode_number=1),
        _make_item("v2", work_id="w-clannad-2008", season_number=2, episode_number=1, year=2008),
        _make_item("v3", work_id="w-clannad-2007", group_type="ignored", season_number=0, episode_number=0),
    ]
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(Path(items[0].target_strm_path).resolve()), real_path="H:\\test1.mkv"),
        MirrorFile(source="pan115", strm_path=str(Path(items[1].target_strm_path).resolve()), real_path="H:\\test2.mkv"),
        MirrorFile(source="pan115", strm_path=str(Path(items[2].target_strm_path).resolve()), real_path="H:\\op.mkv"),
    ])
    index = build_library_index(_make_plan(items), scan_result=scan)
    assert len(index.works) == 1
    work = index.works[0]
    assert work.title == "CLANNAD"
    labels = [s.label for s in work.seasons]
    assert "第1季" in labels
    assert "第2季" in labels
    assert "OP/ED" not in labels
    assert all(ep.work_id == work.work_id for ep in work.episodes)


def test_local_collection_season_groups_merge_for_legacy_plan():
    """旧本地计划中按季写入的 series_group 应在 LibraryIndex 归并成系列卡片。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile

    items = [
        _make_item(
            "local-s1e1",
            source="local",
            work_id="w-yuru-s1",
            work_title="Yuru Camp",
            series_group="Yuru Camp",
            season_number=1,
            episode_number=1,
            target_strm_path="mirror/local/Yuru Camp/Season 1/S01E01.strm",
            relative_path="[VCB-Studio] Yuru Camp/Yuru Camp/Yuru Camp S01E01.mkv",
        ),
        _make_item(
            "local-s2e1",
            source="local",
            work_id="w-yuru-s2",
            work_title="Yuru Camp Season 2",
            series_group="Yuru Camp Season 2",
            season_number=2,
            episode_number=1,
            target_strm_path="mirror/local/Yuru Camp Season 2/Season 2/S02E01.strm",
            relative_path="[VCB-Studio] Yuru Camp/Yuru Camp Season 2/Yuru Camp Season 2 S02E01.mkv",
        ),
        _make_item(
            "local-sp1",
            source="local",
            work_id="w-yuru-sp",
            work_title="Yuru Camp",
            series_group="Yuru Camp",
            group_type="special",
            season_number=None,
            episode_number=1,
            target_strm_path="mirror/local/Yuru Camp/SPs/SP01.strm",
            relative_path="[VCB-Studio] Yuru Camp/Yuru Camp SP/Yuru Camp SP01.mkv",
        ),
    ]
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="local", strm_path=str(Path(items[0].target_strm_path).resolve()), real_path="D:\\test\\s1.mkv"),
        MirrorFile(source="local", strm_path=str(Path(items[1].target_strm_path).resolve()), real_path="D:\\test\\s2.mkv"),
        MirrorFile(source="local", strm_path=str(Path(items[2].target_strm_path).resolve()), real_path="D:\\test\\sp.mkv"),
    ])

    index = build_library_index(_make_plan(items, source="local"), scan_result=scan)

    assert len(index.works) == 1
    work = index.works[0]
    assert work.title == "Yuru Camp"
    labels = [s.label for s in work.seasons]
    assert "第1季" in labels
    assert "第2季" in labels
    assert "特别篇" in labels
    assert len(work.episodes) == 3


def test_local_merged_series_prefers_selected_series_nfo(tmp_path):
    """本地合并卡应读取所选正季 NFO，避免被第一个子条目覆盖标题。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    heya_dir = tmp_path / "Heya Camp" / "Season 1"
    yuru_dir = tmp_path / "Yuru Camp" / "Season 1"
    heya_dir.mkdir(parents=True)
    yuru_dir.mkdir(parents=True)
    (heya_dir / "tvshow.nfo").write_text("<tvshow><title>房间露营△</title></tvshow>", encoding="utf-8")
    yuru_nfo = yuru_dir / "tvshow.nfo"
    yuru_nfo.write_text("<tvshow><title>摇曳露营△</title></tvshow>", encoding="utf-8")

    heya_strm = heya_dir / "S01E01.strm"
    yuru_strm = yuru_dir / "S01E01.strm"
    heya_strm.write_text("D:\\test\\heya.mkv", encoding="utf-8")
    yuru_strm.write_text("D:\\test\\yuru.mkv", encoding="utf-8")

    items = [
        _make_item(
            "heya-1",
            source="local",
            work_id="w-heya",
            work_title="Heya Camp",
            series_group="Heya Camp",
            target_dir=str(heya_dir),
            target_strm_path=str(heya_strm),
            relative_path="[VCB-Studio] Yuru Camp/Heya Camp/Heya Camp S01E01.mkv",
        ),
        _make_item(
            "yuru-1",
            source="local",
            work_id="w-yuru",
            work_title="Yuru Camp",
            series_group="Yuru Camp",
            target_dir=str(yuru_dir),
            target_strm_path=str(yuru_strm),
            relative_path="[VCB-Studio] Yuru Camp/Yuru Camp/Yuru Camp S01E01.mkv",
        ),
    ]
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="local", strm_path=str(heya_strm.resolve()), real_path="D:\\test\\heya.mkv"),
        MirrorFile(source="local", strm_path=str(yuru_strm.resolve()), real_path="D:\\test\\yuru.mkv"),
    ])
    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="target-yuru",
            work_id="w-yuru",
            source="local",
            import_plan_id="p1",
            card_type="main_series",
            media_type="tv",
            series_group="Yuru Camp",
            local_title="Yuru Camp",
            local_season_number=1,
            scrape_title="摇曳露营△",
            nfo_path=str(yuru_nfo),
            tmdb_type="tv",
        )
    ])

    index = build_library_index(_make_plan(items, source="local"), scrape_map=scrape_map, scan_result=scan)

    assert len(index.works) == 1
    assert index.works[0].title == "摇曳露营△"


def test_main_series_does_not_use_standalone_scrape_assets(tmp_path):
    """同一 series_group 下的 standalone 不能覆盖主系列标题和图片。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    yuru_dir = tmp_path / "Yuru Camp" / "Season 1"
    heya_dir = tmp_path / "Heya Camp" / "Season 1"
    yuru_dir.mkdir(parents=True)
    heya_dir.mkdir(parents=True)
    yuru_nfo = yuru_dir / "tvshow.nfo"
    heya_nfo = heya_dir / "tvshow.nfo"
    yuru_poster = tmp_path / "Yuru Camp" / "poster.jpg"
    heya_poster = tmp_path / "Heya Camp" / "poster.jpg"
    yuru_nfo.write_text("<tvshow><title>摇曳露营△</title></tvshow>", encoding="utf-8")
    heya_nfo.write_text("<tvshow><title>房间露营△</title></tvshow>", encoding="utf-8")
    yuru_poster.write_bytes(b"yuru")
    heya_poster.write_bytes(b"heya")

    yuru_strm = yuru_dir / "S01E01.strm"
    heya_strm = heya_dir / "S01E01.strm"
    yuru_strm.write_text("D:\\test\\yuru.mkv", encoding="utf-8")
    heya_strm.write_text("D:\\test\\heya.mkv", encoding="utf-8")

    items = [
        _make_item(
            "yuru-1",
            source="local",
            work_id="w-yuru",
            work_title="Yuru Camp",
            series_group="Yuru Camp",
            card_type="main_series",
            target_dir=str(yuru_dir),
            target_strm_path=str(yuru_strm),
            relative_path="[VCB-Studio] Yuru Camp/Yuru Camp/Yuru Camp S01E01.mkv",
        ),
        _make_item(
            "heya-1",
            source="local",
            work_id="w-heya",
            work_title="Heya Camp",
            series_group="Yuru Camp",
            card_type="standalone",
            relation_type="spin_off",
            target_dir=str(heya_dir),
            target_strm_path=str(heya_strm),
            relative_path="[VCB-Studio] Yuru Camp/Heya Camp/Heya Camp S01E01.mkv",
        ),
    ]
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="local", strm_path=str(yuru_strm.resolve()), real_path="D:\\test\\yuru.mkv"),
        MirrorFile(source="local", strm_path=str(heya_strm.resolve()), real_path="D:\\test\\heya.mkv"),
    ])
    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="target-heya-stale",
            work_id="w-heya",
            source="local",
            import_plan_id="p1",
            card_type="main_series",
            media_type="tv",
            series_group="Yuru Camp",
            local_title="Heya Camp",
            source_subwork_dir="Heya Camp",
            local_season_number=1,
            scrape_title="房间露营△",
            nfo_path=str(heya_nfo),
            poster_path=str(heya_poster),
            tmdb_type="tv",
        ),
        ScrapeMapItem(
            scrape_target_id="target-heya",
            work_id="w-heya",
            source="local",
            import_plan_id="p1",
            card_type="standalone",
            media_type="tv",
            series_group="Yuru Camp",
            local_title="Heya Camp",
            local_season_number=1,
            scrape_title="房间露营△",
            nfo_path=str(heya_nfo),
            poster_path=str(heya_poster),
            tmdb_type="tv",
        ),
        ScrapeMapItem(
            scrape_target_id="target-yuru",
            work_id="w-yuru",
            source="local",
            import_plan_id="p1",
            card_type="main_series",
            media_type="tv",
            series_group="Yuru Camp",
            local_title="Yuru Camp",
            local_season_number=1,
            scrape_title="摇曳露营△",
            nfo_path=str(yuru_nfo),
            poster_path=str(yuru_poster),
            tmdb_type="tv",
        ),
    ])

    index = build_library_index(_make_plan(items, source="local"), scrape_map=scrape_map, scan_result=scan)
    series = next(work for work in index.works if work.card_type == "main_series")
    standalone = next(work for work in index.works if work.card_type == "standalone")
    assert series.title == "摇曳露营△"
    assert series.poster_path == str(yuru_poster)
    assert standalone.title == "房间露营△"
    assert standalone.poster_path == str(heya_poster)


def test_standalone_title_not_overwritten_by_series_group():
    """standalone movie 保留自己的标题，不被 series_group 覆盖"""
    from app.library.index import build_library_index

    items = [
        _make_item("v1", work_id="w-movie", work_title="CLANNAD总集篇：在那苍绿的树下",
                    year=2009, group_type="movie", card_type="standalone",
                    season_number=None, episode_number=None, series_group="CLANNAD"),
    ]
    index = build_library_index(_make_plan(items))
    assert len(index.works) == 1
    assert "苍绿" in index.works[0].title


def test_standalone_movie_does_not_inherit_series_poster(tmp_path):
    """standalone movie 没有自己的海报时，不应回退到主系列 TV 海报"""
    from app.library.index import build_library_index
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    series_poster = tmp_path / "series-poster.jpg"
    series_poster.write_bytes(b"poster")

    movie_item = _make_item(
        "movie1",
        work_id="w-movie",
        work_title="刀剑神域",
        original_title="4.剧场版：序列之争.2017",
        title="4.剧场版：序列之争.2017",
        year=2017,
        group_type="movie",
        card_type="standalone",
        media_type="movie",
        season_number=None,
        episode_number=None,
        series_group="刀剑神域",
    )
    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="series-s1",
            work_id="w-series",
            source="pan115",
            import_plan_id="p1",
            card_type="main_series",
            media_type="tv",
            series_group="刀剑神域",
            local_title="刀剑神域",
            scrape_title="刀剑神域",
            scrape_year=2012,
            tmdb_id=45782,
            tmdb_type="tv",
            local_season_number=1,
            poster_path=str(series_poster),
        ),
    ])

    index = build_library_index(_make_plan([movie_item]), scrape_map=scrape_map)
    work = index.works[0]
    assert work.title == "剧场版：序列之争"
    assert work.poster_path == ""


def test_library_index_keeps_remote_scrape_artwork_urls():
    """远程图片 URL 不应被本地文件存在性检查清空。"""
    from app.library.index import build_library_index
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    item = _make_item(
        "ep1",
        work_id="w-remote-artwork",
        work_title="莉可丽丝",
        title="莉可丽丝",
        group_type="season",
        card_type="main_series",
        media_type="tv",
        season_number=1,
        episode_number=1,
        series_group="莉可丽丝",
    )
    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="remote-s1",
            work_id="w-remote-artwork",
            source="pan115",
            import_plan_id="p1",
            card_type="main_series",
            media_type="tv",
            series_group="莉可丽丝",
            local_title="莉可丽丝",
            scrape_title="莉可丽丝",
            tmdb_id=154494,
            tmdb_type="tv",
            local_season_number=1,
            poster_path="https://image.tmdb.org/t/p/w780/poster.jpg",
            fanart_path="https://image.tmdb.org/t/p/w1280/fanart.jpg",
        ),
    ])

    index = build_library_index(_make_plan([item]), scrape_map=scrape_map)
    work = index.works[0]

    assert work.poster_path == "https://image.tmdb.org/t/p/w780/poster.jpg"
    assert work.fanart_path == "https://image.tmdb.org/t/p/w1280/fanart.jpg"


def test_standalone_movie_title_falls_back_to_bracketed_filename():
    """旧计划缺少 item.title 时，也应从 MOVIE 文件名补具体电影标题。"""
    from app.library.index import build_library_index

    item = _make_item(
        "movie-jujutsu",
        work_id="w-jujutsu-movie",
        work_title="Jujutsu_Kaisen",
        original_title="[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]",
        title="",
        year=None,
        group_type="movie",
        card_type="standalone",
        media_type="movie",
        season_number=None,
        episode_number=None,
        series_group="Jujutsu_Kaisen",
        relative_path=(
            "动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE][CHS][1080P][MP4]/"
            "[BeanSub&FZSD][Jujutsu_Kaisen_0][MOVIE][BDRip][CHS][1080P][AVC_AAC](FA6841CF).mp4"
        ),
    )

    index = build_library_index(_make_plan([item]))

    assert len(index.works) == 1
    assert index.works[0].title == "Jujutsu Kaisen 0"


def test_standalone_movie_title_ignores_generic_movie_folder_name():
    """历史计划中的“剧场版”分类名不能继续显示为电影标题。"""
    from app.library.index import build_library_index

    item = _make_item(
        "movie-violet",
        work_id="w-violet-movie",
        work_title="紫罗兰永恒花园",
        original_title="3.剧场版.2021",
        title="剧场版",
        year=2021,
        group_type="movie",
        card_type="standalone",
        media_type="movie",
        season_number=None,
        episode_number=None,
        series_group="紫罗兰永恒花园",
        relative_path=(
            "动画/紫罗兰永恒花园.TV版+外传+剧场版/3.剧场版.2021/剧场版/"
            "[MAI] Gekijouban Violet Evergarden [Ma10p_2160p][x265_flac_ass].mkv"
        ),
    )

    index = build_library_index(_make_plan([item]))

    assert index.works[0].title == "Gekijouban Violet Evergarden"


def test_standalone_movie_title_cleans_release_folder_tags():
    """旧计划里的电影目录标题带字幕组/压制标签时，卡片标题应清洗。"""
    from app.library.index import build_library_index

    item = _make_item(
        "movie-yuru",
        work_id="w-yuru-movie",
        work_title="Yuru Camp Movie",
        original_title="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]",
        title="[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]",
        year=None,
        group_type="movie",
        card_type="standalone",
        media_type="movie",
        season_number=None,
        episode_number=None,
        series_group="Yuru Camp",
        relative_path=(
            "[VCB-Studio] Yuru Camp/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]/"
            "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p][x265_flac].mkv"
        ),
    )

    index = build_library_index(_make_plan([item], source="local"))

    assert len(index.works) == 1
    assert index.works[0].title == "Yuru Camp Movie"


def test_library_api_asset_fallback_does_not_cross_source_root(tmp_path):
    """API 展示兜底不能让缺图电影捡到同源其他作品的 poster"""
    from app.api.library import _enrich_payload

    source_root = tmp_path / "mirror" / "115"
    movie_dir = source_root / "剧场版：Fine (2020)"
    other_dir = source_root / "千年女优.Millennium.Actress (2001)"
    movie_dir.mkdir(parents=True)
    other_dir.mkdir(parents=True)
    (movie_dir / "剧场版：Fine (2020).strm").write_text("H:\\test.mkv", encoding="utf-8")
    (other_dir / "poster.jpg").write_bytes(b"poster")

    payload = {
        "works": [{
            "title": "剧场版：Fine",
            "media_type": "movie",
            "card_type": "standalone",
            "dir_path": str(movie_dir),
            "poster_path": "",
            "episodes": [{
                "season_number": 0,
                "episode_number": 1,
                "group_type": "movie",
                "strm_path": str(movie_dir / "剧场版：Fine (2020).strm"),
            }],
        }]
    }

    _enrich_payload(payload)
    assert payload["works"][0].get("poster_path", "") == ""


def test_op_ed_in_groups():
    """OP/ED 已废弃：不进入详情分组"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile

    items = [
        _make_item("v1", season_number=1, episode_number=1),
        _make_item("v2", group_type="ignored", season_number=0, episode_number=0),
    ]
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(Path(items[0].target_strm_path).resolve()), real_path="H:\\test.mkv"),
        MirrorFile(source="pan115", strm_path=str(Path(items[1].target_strm_path).resolve()), real_path="H:\\test.mkv"),
    ])
    index = build_library_index(_make_plan(items), scan_result=scan)
    group_types = [s.group_type for s in index.works[0].seasons]
    assert "op_ed" not in group_types
    assert "ignored" not in group_types


def test_standalone_work():
    """standalone movie 进入独立 WorkIndex"""
    from app.library.index import build_library_index

    items = [
        _make_item("v1", work_id="w-movie", work_title="总集篇", year=2009,
                    group_type="movie", card_type="standalone", season_number=None, episode_number=None),
    ]
    index = build_library_index(_make_plan(items))
    assert len(index.works) == 1
    assert index.works[0].card_type == "standalone"
    assert len(index.works[0].seasons) == 0


def test_two_movies_in_same_parent_folder_create_two_related_cards(tmp_path):
    """同一父目录里的多部电影必须独立成卡，并与同系列作品双向关联。"""
    from app.library.index import build_library_index, rebuild_related_works_for_plan
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    main = _make_item(
        "main-01", work_id="bocchi-main", work_title="孤独摇滚！",
        series_group="孤独摇滚！", media_type="tv", show_type="anime_series",
    )
    movie_a_dir = tmp_path / "剧场总集篇 孤独摇滚！Re"
    movie_b_dir = tmp_path / "剧场总集篇 孤独摇滚！Re-Re"
    movie_a = _make_item(
        "movie-a", work_id="shared-parent-id", work_title="剧场总集篇 孤独摇滚！",
        original_title="剧场总集篇 孤独摇滚！Re- (2024)", series_group="孤独摇滚！",
        card_type="standalone", group_type="movie", media_type="movie",
        show_type="anime_movie", season_number=None, episode_number=None,
        belongs_to_series="孤独摇滚！", relation_type="recap",
        target_dir=str(movie_a_dir), target_strm_path=str(movie_a_dir / "movie-a.strm"),
        relative_path="动画/剧场总集篇 孤独摇滚！/剧场总集篇 孤独摇滚！Re- (2024)/movie-a.mkv",
    )
    movie_b = _make_item(
        "movie-b", work_id="shared-parent-id", work_title="剧场总集篇 孤独摇滚！",
        original_title="剧场总集篇 孤独摇滚！Re-Re- (2024)", series_group="孤独摇滚！",
        card_type="standalone", group_type="movie", media_type="movie",
        show_type="anime_movie", season_number=None, episode_number=None,
        belongs_to_series="孤独摇滚！", relation_type="recap",
        target_dir=str(movie_b_dir), target_strm_path=str(movie_b_dir / "movie-b.strm"),
        relative_path="动画/剧场总集篇 孤独摇滚！/剧场总集篇 孤独摇滚！Re-Re- (2024)/movie-b.mkv",
    )
    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="movie-a-target", work_id="shared-parent-id", source="pan115",
            card_type="standalone", media_type="movie", series_group="孤独摇滚！",
            local_title="剧场总集篇 孤独摇滚！Re-", tmdb_id=1001, tmdb_type="movie",
            nfo_path=str(movie_a_dir / "movie.nfo"),
        ),
        ScrapeMapItem(
            scrape_target_id="movie-b-target", work_id="shared-parent-id", source="pan115",
            card_type="standalone", media_type="movie", series_group="孤独摇滚！",
            local_title="剧场总集篇 孤独摇滚！Re-Re-", tmdb_id=1002, tmdb_type="movie",
            nfo_path=str(movie_b_dir / "movie.nfo"),
        ),
    ])

    index = build_library_index(_make_plan([main, movie_a, movie_b]), scrape_map=scrape_map)
    movies = [work for work in index.works if work.media_type == "movie"]

    assert len(movies) == 2
    assert len({work.work_id for work in movies}) == 2
    assert all(len(work.related_works) == 2 for work in movies)
    main_work = next(work for work in index.works if work.media_type == "tv")
    assert {item.work_id for item in main_work.related_works} == {work.work_id for work in movies}

    for work in index.works:
        work.related_works = []
    rebuild_related_works_for_plan(index.works, _make_plan([main, movie_a, movie_b]))

    assert all(len(work.related_works) == 2 for work in index.works)


def test_standalone_tv_uses_confirmed_scrape_type_instead_of_movie_card_type():
    """独立外传可以是 TV；已确认的 TMDB TV 类型必须高于 standalone 卡片形态。"""
    from app.library.index import build_library_index
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    item = _make_item(
        "heya-01",
        work_id="heya-camp",
        work_title="Heya Camp",
        series_group="Yuru Camp",
        card_type="standalone",
        group_type="season",
        media_type="tv",
        show_type="anime_series",
        belongs_to_series="Yuru Camp",
        relation_type="spin_off",
    )
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id="heya-target",
        work_id="heya-camp",
        source="pan115",
        card_type="standalone",
        media_type="tv",
        series_group="Yuru Camp",
        local_title="Heya Camp",
        tmdb_id=95213,
        tmdb_type="tv",
        selected_by="auto",
    )])

    work = build_library_index(_make_plan([item]), scrape_map=scrape_map).works[0]

    assert work.media_type == "tv"
    assert work.show_type == "anime_series"


def test_related_works(tmp_path):
    """related_works 在不同卡片之间双向链接，且从目标卡片读取展示资产"""
    from app.library.index import build_library_index
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    items = [
        _make_item("v1", work_id="w-main"),
        _make_item("v2", work_id="w-movie", work_title="剧场版", year=2017,
                    group_type="movie", card_type="standalone",
                    season_number=None, episode_number=None,
                    belongs_to_series="w-main", relation_type="movie"),
    ]
    poster = tmp_path / "poster.jpg"
    fanart = tmp_path / "fanart.jpg"
    poster.write_bytes(b"poster")
    fanart.write_bytes(b"fanart")
    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="target-movie",
            work_id="w-movie",
            source="pan115",
            import_plan_id="p1",
            card_type="standalone",
            media_type="movie",
            series_group="CLANNAD",
            local_title="剧场版",
            scrape_title="剧场版",
            poster_path=str(poster),
            fanart_path=str(fanart),
        )
    ])

    index = build_library_index(_make_plan(items), scrape_map=scrape_map)
    w1 = next(w for w in index.works if w.card_type == "main_series")
    movie = next(w for w in index.works if w.card_type == "standalone")

    assert len(w1.related_works) == 1
    assert w1.related_works[0].relation_type == "movie"
    assert w1.related_works[0].work_id == movie.work_id
    assert w1.related_works[0].poster_path == str(poster)
    assert w1.related_works[0].fanart_path == str(fanart)
    assert len(movie.related_works) == 1
    assert movie.related_works[0].card_type == "main_series"


def test_related_works_title_fallback():
    """没有显式 belongs_to_series 时，标题兜底也应能连回主系列。"""
    from app.library.index import build_library_index

    items = [
        _make_item("v1", work_id="w-main", work_title="CLANNAD", series_group="CLANNAD"),
        _make_item(
            "v2",
            work_id="w-movie",
            work_title="CLANNAD 剧场版",
            year=2007,
            group_type="movie",
            card_type="standalone",
            season_number=None,
            episode_number=None,
        ),
    ]

    index = build_library_index(_make_plan(items))
    w1 = next(w for w in index.works if w.card_type == "main_series")
    movie = next(w for w in index.works if w.card_type == "standalone")

    assert len(w1.related_works) == 1
    assert w1.related_works[0].card_type == "standalone"
    assert len(movie.related_works) == 1
    assert movie.related_works[0].card_type == "main_series"


def test_rebuild_related_works_scans_plan_once(monkeypatch):
    """局部刷新重建关联时，计划扫描次数不能随作品卡数量成倍增长。"""
    from app.library import index as index_module
    from app.library.models import WorkIndex

    item_count = 40
    items = [
        _make_item(
            f"related-{number}",
            work_id=f"work-{number}",
            work_title=f"作品 {number}",
            series_group=f"系列 {number // 4}",
            card_type="standalone",
            belongs_to_series=f"系列 {number // 4}",
        )
        for number in range(item_count)
    ]
    works = [
        WorkIndex(
            work_id=index_module._library_work_id(item),
            source="pan115",
            title=item.work_title,
        )
        for item in items
    ]
    original = index_module._related_title_keys
    calls = 0

    def counted_related_title_keys(value):
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(index_module, "_related_title_keys", counted_related_title_keys)

    index_module.rebuild_related_works_for_plan(works, _make_plan(items))

    assert calls == item_count * 4


def test_missing_strm():
    """缺失 .strm 的作品不应留下 0 集空卡片"""
    from app.library.index import build_library_index

    items = [
        _make_item("v1", target_strm_path=""),
    ]
    index = build_library_index(_make_plan(items))
    assert index.works == []
    assert index.source_summary.get("pan115", {}).get("missing_strm_count", 0) >= 1


def test_stable_id():
    """同一输入生成稳定索引"""
    from app.library.index import build_library_index

    items = [_make_item("v1")]
    i1 = build_library_index(_make_plan(items))
    i2 = build_library_index(_make_plan(items))
    assert i1.works[0].work_id == i2.works[0].work_id


def test_episode_title_from_episode_nfo(tmp_path):
    """重扫媒体库时应从分集 NFO 读取标题/简介/时长"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile

    season_dir = tmp_path / "CLANNAD" / "Season 2"
    season_dir.mkdir(parents=True)
    strm = season_dir / "CLANNAD - S02E01.strm"
    strm.write_text("H:\\test\\ep01.mkv", encoding="utf-8")
    # 本地 S2 可能映射 TMDB S1，所以这里故意写 S01E01.nfo。
    nfo = season_dir / "S01E01.nfo"
    nfo.write_text(
        """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<episodedetails>
  <title>时间上的跳跃者</title>
  <plot>第一集简介</plot>
  <runtime>24</runtime>
</episodedetails>
""",
        encoding="utf-8",
    )

    item = _make_item(
        "v1",
        work_id="w-clannad-2008",
        work_title="CLANNAD",
        year=2008,
        season_number=2,
        episode_number=1,
        target_strm_path=str(strm),
    )
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(strm.resolve()), real_path="H:\\test\\ep01.mkv"),
    ])
    index = build_library_index(_make_plan([item]), scan_result=scan)

    episode = index.works[0].episodes[0]
    assert episode.title == "时间上的跳跃者"
    assert episode.plot == "第一集简介"
    assert episode.runtime == 24
    assert episode.nfo_path == str(nfo)


def test_special_title_from_episode_nfo(tmp_path):
    """特别篇也应读取 S00Exx NFO，不能只显示原文件占位标题。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorFile, MirrorScanResult

    special_dir = tmp_path / "AIR" / "Season 0"
    special_dir.mkdir(parents=True)
    strm = special_dir / "AIR - S00E01.strm"
    strm.write_text("H:\\test\\special01.mkv", encoding="utf-8")
    nfo = special_dir / "S00E01.nfo"
    nfo.write_text(
        "<episodedetails><title>夏季特别篇</title><plot>特别篇简介</plot></episodedetails>",
        encoding="utf-8",
    )
    item = _make_item(
        "sp1",
        work_id="w-air",
        work_title="AIR",
        group_type="special",
        season_number=0,
        episode_number=None,
        special_number=1,
        title="SP01",
        target_strm_path=str(strm),
    )
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(strm.resolve()), real_path="H:\\test\\special01.mkv"),
    ])

    index = build_library_index(_make_plan([item]), scan_result=scan)

    episode = index.works[0].episodes[0]
    assert episode.title == "夏季特别篇"
    assert episode.plot == "特别篇简介"
    assert episode.nfo_path == str(nfo)


def test_series_card_keeps_season_level_scrape_metadata(tmp_path):
    """同一系列一张卡片，但每一季必须保留自己的刮削身份"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    root = tmp_path / "CLANNAD"
    season1_dir = root / "Season 1"
    season2_dir = root / "Season 2"
    season1_dir.mkdir(parents=True)
    season2_dir.mkdir(parents=True)
    s1_strm = season1_dir / "CLANNAD - S01E01.strm"
    s2_strm = season2_dir / "CLANNAD - S02E01.strm"
    s1_strm.write_text("H:\\\\test\\\\s1e1.mkv", encoding="utf-8")
    s2_strm.write_text("H:\\\\test\\\\s2e1.mkv", encoding="utf-8")

    s1_nfo = season1_dir / "tvshow.nfo"
    s2_nfo = season2_dir / "tvshow.nfo"
    s1_nfo.write_text(
        "<tvshow><title>CLANNAD</title><year>2007</year><plot>第一季简介</plot><rating>8.1</rating></tvshow>",
        encoding="utf-8",
    )
    s2_nfo.write_text(
        "<tvshow><title>CLANNAD After Story</title><year>2008</year><plot>第二季简介</plot><rating>8.9</rating></tvshow>",
        encoding="utf-8",
    )
    s2_poster = season2_dir / "poster.jpg"
    s2_fanart = season2_dir / "fanart.jpg"
    s2_poster.write_bytes(b"poster")
    s2_fanart.write_bytes(b"fanart")

    items = [
        _make_item(
            "s1e1", work_id="clannad-2007", work_title="CLANNAD", year=2007,
            season_number=1, episode_number=1, target_strm_path=str(s1_strm),
        ),
        _make_item(
            "s2e1", work_id="clannad-2008", work_title="CLANNAD", year=2008,
            season_number=2, episode_number=1, target_strm_path=str(s2_strm),
        ),
    ]
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(s1_strm.resolve()), real_path="H:\\\\test\\\\s1e1.mkv"),
        MirrorFile(source="pan115", strm_path=str(s2_strm.resolve()), real_path="H:\\\\test\\\\s2e1.mkv"),
    ])
    scrape_map = ScrapeMap(items=[
        ScrapeMapItem(
            scrape_target_id="target-s1", work_id="clannad-2007", source="pan115",
            import_plan_id="p1", card_type="main_series", media_type="tv",
            series_group="CLANNAD", local_title="CLANNAD", local_year=2007,
            local_season_number=1, scrape_title="CLANNAD", scrape_year=2007,
            tmdb_id=101, tmdb_type="tv", tmdb_season_number=1,
            selected_by="auto", nfo_path=str(s1_nfo),
        ),
        ScrapeMapItem(
            scrape_target_id="target-s2", work_id="clannad-2008", source="pan115",
            import_plan_id="p1", card_type="main_series", media_type="tv",
            series_group="CLANNAD", local_title="CLANNAD", local_year=2008,
            local_season_number=2, scrape_title="CLANNAD After Story", scrape_year=2008,
            tmdb_id=202, tmdb_type="tv", tmdb_season_number=1,
            selected_by="auto", nfo_path=str(s2_nfo),
            poster_path=str(s2_poster), fanart_path=str(s2_fanart),
        ),
    ])

    index = build_library_index(_make_plan(items), scrape_map=scrape_map, scan_result=scan)
    assert len(index.works) == 1
    work = index.works[0]
    assert len(work.seasons) == 2

    season2 = next(s for s in work.seasons if s.season_number == 2)
    assert season2.scrape_target_id == "target-s2"
    assert season2.scrape_title == "CLANNAD After Story"
    assert season2.scrape_year == 2008
    assert season2.tmdb_id == 202
    assert season2.tmdb_season_number == 1
    assert season2.plot == "第二季简介"
    assert season2.rating == 8.9
    assert season2.poster_path == str(s2_poster)
    assert season2.fanart_path == str(s2_fanart)


def test_episodes_sorted_by_number(tmp_path):
    """导入计划顺序可能乱，LibraryIndex 必须按集号稳定输出。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile

    root = tmp_path / "Show" / "Season 1"
    root.mkdir(parents=True)
    items = []
    files = []
    for ep in (2, 4, 1, 3):
        strm = root / f"Show - S01E{ep:02d}.strm"
        strm.write_text(f"H:\\\\test\\\\ep{ep:02d}.mkv", encoding="utf-8")
        items.append(_make_item(
            f"ep{ep}", work_id="show", work_title="Show", year=2024,
            season_number=1, episode_number=ep, target_strm_path=str(strm),
        ))
        files.append(MirrorFile(source="pan115", strm_path=str(strm.resolve()), real_path=f"H:\\\\test\\\\ep{ep:02d}.mkv"))

    index = build_library_index(_make_plan(items), scan_result=MirrorScanResult(strm_files=files))
    assert [ep.episode_number for ep in index.works[0].episodes] == [1, 2, 3, 4]


def test_local_series_in_separate_roots_remain_separate(tmp_path):
    """本地来源的独立作品根目录即使 TMDB 相同，也必须保留不同卡片。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    files = []
    items = []
    scrape_items = []
    for season in (1, 2):
        season_dir = tmp_path / f"Yuru Camp Season {season}" / "Season 0"
        season_dir.mkdir(parents=True)
        strm = season_dir / f"Yuru Camp - S{season:02d}E01.strm"
        strm.write_text(f"H:\\\\test\\\\s{season}e1.mkv", encoding="utf-8")
        item = _make_item(
            f"s{season}e1",
            source="local",
            work_id=f"yuru-s{season}",
            work_title=f"Yuru Camp Season {season}",
            series_group=f"Yuru Camp Season {season}",
            season_number=season,
            episode_number=1,
            target_strm_path=str(strm),
        )
        items.append(item)
        files.append(MirrorFile(source="local", strm_path=str(strm.resolve()), real_path=f"H:\\\\test\\\\s{season}e1.mkv"))
        scrape_items.append(ScrapeMapItem(
            scrape_target_id=f"target-s{season}",
            work_id=f"yuru-s{season}",
            source="local",
            import_plan_id="p1",
            card_type="main_series",
            media_type="tv",
            series_group=f"Yuru Camp Season {season}",
            local_title=f"Yuru Camp Season {season}",
            local_year=2018,
            local_season_number=season,
            scrape_title="摇曳露营△",
            scrape_year=2018,
            tmdb_id=76075,
            tmdb_type="tv",
            tmdb_season_number=season,
            selected_by="auto",
        ))

    index = build_library_index(
        _make_plan(items, source="local"),
        scrape_map=ScrapeMap(items=scrape_items),
        scan_result=MirrorScanResult(strm_files=files),
    )

    assert len(index.works) == 2
    assert all(work.source == "local" for work in index.works)
    assert sorted(len(work.episodes) for work in index.works) == [1, 1]
    assert sorted(season.label for work in index.works for season in work.seasons) == ["第1季", "第2季"]


def test_cloud_series_title_not_disambiguated_when_aggregated(tmp_path):
    """网盘同系列仍然聚合成一张卡，不把季度写到总标题里。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorScanResult, MirrorFile

    items = []
    files = []
    for season in (1, 2):
        season_dir = tmp_path / "Yuru Camp" / f"Season {season}"
        season_dir.mkdir(parents=True)
        strm = season_dir / f"Yuru Camp - S{season:02d}E01.strm"
        strm.write_text(f"H:\\\\test\\\\s{season}e1.mkv", encoding="utf-8")
        items.append(_make_item(
            f"s{season}e1",
            work_id=f"yuru-s{season}",
            work_title="摇曳露营△",
            series_group="摇曳露营△",
            season_number=season,
            episode_number=1,
            target_strm_path=str(strm),
        ))
        files.append(MirrorFile(source="pan115", strm_path=str(strm.resolve()), real_path=f"H:\\\\test\\\\s{season}e1.mkv"))

    index = build_library_index(_make_plan(items), scan_result=MirrorScanResult(strm_files=files))

    assert len(index.works) == 1
    assert index.works[0].title == "摇曳露营△"


def test_auxiliary_only_legacy_s01e00_does_not_create_tv_card(tmp_path):
    """旧错误计划里的 CM/PV S01E00 不应单独生成番剧卡片。"""
    from app.library.index import build_library_index
    from app.library.scanner import MirrorFile, MirrorScanResult

    strm_path = tmp_path / "115" / "通往夏天的隧道，离别的出口" / "Season 1" / "S01E00.strm"
    strm_path.parent.mkdir(parents=True)
    real_path = r"H:\115open\动画\通往夏天的隧道，离别的出口.2022\SPs\[MAI] Natsu e no Tunnel [CM01].mkv"
    strm_path.write_text(real_path, encoding="utf-8")
    item = _make_item(
        "legacy-cm",
        work_id="bad-tv-work",
        work_title="通往夏天的隧道，离别的出口",
        year=2022,
        group_type="season",
        season_number=1,
        episode_number=0,
        series_group="通往夏天的隧道，离别的出口",
        card_type="main_series",
        target_strm_path=str(strm_path),
        target_filename="S01E00.strm",
        relative_path="动画/通往夏天的隧道，离别的出口.2022/SPs/[MAI] Natsu e no Tunnel [CM01].mkv",
    )
    scan = MirrorScanResult(strm_files=[
        MirrorFile(source="pan115", strm_path=str(strm_path.resolve()), real_path=real_path),
    ])

    index = build_library_index(_make_plan([item]), scan_result=scan)

    assert index.works == []


def test_movie_folder_with_sps_cm_pv_builds_only_movie_card(tmp_path):
    """电影目录下的 SPs/CM/PV 跳过，只保留电影正片卡。"""
    from app.import_plan.models import ImportPlan, ImportPlanItem
    from app.library.index import build_library_index
    from app.library.scanner import scan_mirror
    from app.mirror.generator import generate_mirror
    from app.recognition.plan_recognizer import recognize_import_plan_media

    raw_paths = [
        "[MAI] Natsu e no Tunnel, Sayonara no Deguchi [Ma10p_1608p][x265].mkv",
        "SPs/[MAI] Natsu e no Tunnel, Sayonara no Deguchi [CM01][Ma10p_1080p][x265].mkv",
        "SPs/[MAI] Natsu e no Tunnel, Sayonara no Deguchi [PV01][Ma10p_1080p][x265].mkv",
    ]
    items = []
    for idx, rel in enumerate(raw_paths, start=1):
        relative_path = f"动画/通往夏天的隧道，离别的出口.2022/{rel}"
        items.append(ImportPlanItem(
            id=f"movie-{idx}",
            plan_id="p-movie",
            raw_file_id=f"raw-{idx}",
            source="pan115",
            relative_path=relative_path,
            real_path=rf"H:\115open\{relative_path.replace('/', chr(92))}",
            resource_type="video",
            action="generate_strm",
            confidence="high",
        ))
    plan = ImportPlan(
        plan_id="p-movie",
        source="pan115",
        source_snapshot_id="snap-movie",
        import_family="anime",
        status="draft",
        items=items,
    )
    recognize_import_plan_media(plan)
    plan.status = "confirmed"

    result = generate_mirror(plan, str(tmp_path))
    scan = scan_mirror(source="pan115", mirror_root=str(tmp_path))
    index = build_library_index(plan, scan_result=scan)

    assert result.failed_count == 0
    assert len(index.works) == 1
    work = index.works[0]
    assert work.title == "通往夏天的隧道，离别的出口"
    assert work.media_type == "movie"
    assert work.show_type == "anime_movie"
    assert [ep.group_type for ep in work.episodes].count("movie") == 1
    assert {ep.group_type for ep in work.episodes} == {"movie"}
    assert all(item.action == "ignore" for item in plan.items if "/SPs/" in item.relative_path)


def test_parse_nfo_exposes_cast_for_detail_page(tmp_path):
    from app.library.index import _parse_nfo

    nfo = tmp_path / "tvshow.nfo"
    nfo.write_text(
        """<tvshow><title>虫师</title><actor><name>中村悠一</name><role>银古</role>
        <thumb>https://image.tmdb.org/t/p/w185/cast.jpg</thumb></actor></tvshow>""",
        encoding="utf-8",
    )

    parsed = _parse_nfo(nfo)

    assert parsed["cast"] == [{
        "name": "中村悠一", "role": "银古",
        "profile_path": "https://image.tmdb.org/t/p/w185/cast.jpg",
    }]


def test_normalize_path_does_not_resolve_offline_filesystem(monkeypatch):
    """媒体库读路径只做词法归一化，不能探测可能离线的盘符。"""
    from app.library.index import _normalize_path as normalize_index_path
    from app.library.service import _normalize_path as normalize_service_path

    def fail_resolve(*args, **kwargs):
        raise AssertionError("媒体库路径归一化不得访问文件系统")

    monkeypatch.setattr(Path, "resolve", fail_resolve)

    for normalize_path in (normalize_index_path, normalize_service_path):
        assert normalize_path(r"Z:\offline\Anime\Season 01\..") == "Z:/offline/Anime"


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [
        test_build_work_index, test_build_season_index, test_clannad_aggregation,
        test_standalone_title_not_overwritten_by_series_group,
        test_op_ed_in_groups, test_standalone_work, test_related_works,
        test_related_works_title_fallback,
        test_missing_strm, test_stable_id,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\nResult: {passed} passed, {failed} failed, {len(tests)} total")
