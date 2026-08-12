# -*- coding: utf-8 -*-
"""Library/ScrapeMap 一致性诊断测试"""

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _series_work_id(source="pan115", series_group="CLANNAD"):
    content = f"{source}:{series_group}"
    return "series_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]


def _scrape_item(
    target_id,
    local_season,
    tmdb_season,
    tmdb_id=24835,
    source="pan115",
    series_group="CLANNAD",
    title="CLANNAD",
    nfo_path="",
):
    from app.scrape.models import ScrapeMapItem

    return ScrapeMapItem(
        scrape_target_id=target_id,
        work_id=f"work-{target_id}",
        source=source,
        card_type="main_series",
        media_type="tv",
        series_group=series_group,
        local_title=series_group,
        local_season_number=local_season,
        scrape_title=title,
        scrape_year=2006 + local_season,
        tmdb_id=tmdb_id,
        tmdb_type="tv",
        tmdb_season_number=tmdb_season,
        nfo_path=nfo_path,
    )


def _library_index(seasons, source="pan115", series_group="CLANNAD"):
    from app.library.models import LibraryIndex, WorkIndex

    return LibraryIndex(
        works=[
            WorkIndex(
                work_id=_series_work_id(source, series_group),
                title=series_group,
                source=source,
                card_type="main_series",
                media_type="tv",
                seasons=seasons,
            )
        ]
    )


def _season(target_id, local_season, tmdb_season, tmdb_id=24835, title="CLANNAD"):
    from app.library.models import SeasonIndex

    return SeasonIndex(
        season_id=f"s{local_season}",
        work_id=_series_work_id(),
        season_number=local_season,
        group_type="season",
        label=f"第{local_season}季",
        episode_count=22,
        scrape_target_id=target_id,
        scrape_title=title,
        tmdb_id=tmdb_id,
        tmdb_type="tv",
        tmdb_season_number=tmdb_season,
        scraped=True,
    )


def test_diagnostics_pass_multi_season_mapping(monkeypatch, tmp_path):
    """同系列多季分别绑定 TMDB Season 1/2 时应通过."""
    from app.library import diagnostics
    from app.scrape.models import ScrapeMap

    nfo = tmp_path / "tvshow.nfo"
    nfo.write_text("<tvshow />", encoding="utf-8")

    monkeypatch.setattr(diagnostics, "load_library_index", lambda: _library_index([
        _season("t1", 1, 1, title="CLANNAD"),
        _season("t2", 2, 2, title="CLANNAD After Story"),
    ]))
    monkeypatch.setattr(diagnostics, "load_scrape_map", lambda: ScrapeMap(items=[
        _scrape_item("t1", 1, 1, title="CLANNAD", nfo_path=str(nfo)),
        _scrape_item("t2", 2, 2, title="CLANNAD After Story", nfo_path=str(nfo)),
    ]))

    result = diagnostics.diagnose_library_consistency("pan115")
    assert result["ok"] is True
    assert result["summary"]["checked_scrape_seasons"] == 2
    assert result["errors"] == []


def test_diagnostics_reports_season_tmdb_mismatch(monkeypatch):
    """LibraryIndex 如果把本地第2季挂到错误 TMDB 季，应报错."""
    from app.library import diagnostics
    from app.scrape.models import ScrapeMap

    monkeypatch.setattr(diagnostics, "load_library_index", lambda: _library_index([
        _season("t2", 2, 1, title="CLANNAD After Story"),
    ]))
    monkeypatch.setattr(diagnostics, "load_scrape_map", lambda: ScrapeMap(items=[
        _scrape_item("t2", 2, 2, title="CLANNAD After Story"),
    ]))

    result = diagnostics.diagnose_library_consistency("pan115")
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "season_tmdb_mismatch"
    assert result["errors"][0]["local_season_number"] == 2
    assert result["errors"][0]["tmdb_season_number"] == 2
    assert result["errors"][0]["library_tmdb_season_number"] == 1


def test_diagnostics_missing_asset_is_warning(monkeypatch):
    """图片/NFO 缺失是可修复状态，先提示 warning，不阻断一致性."""
    from app.library import diagnostics
    from app.scrape.models import ScrapeMap

    monkeypatch.setattr(diagnostics, "load_library_index", lambda: _library_index([
        _season("t1", 1, 1),
    ]))
    monkeypatch.setattr(diagnostics, "load_scrape_map", lambda: ScrapeMap(items=[
        _scrape_item("t1", 1, 1, nfo_path="Z:/not-exists/tvshow.nfo"),
    ]))

    result = diagnostics.diagnose_library_consistency("pan115")
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["warnings"][0]["code"] == "nfo_missing"


def test_diagnostics_counts_seasonal_works_without_checking_their_update_mapping(monkeypatch):
    """新番只统计作品数量，不把季度/剧集更新产生的旧映射报为风险。"""
    from types import SimpleNamespace
    from app.library import diagnostics
    from app.library.models import LibraryIndex, WorkIndex
    from app.scrape.models import ScrapeMap

    seasonal_item = _scrape_item("old-seasonal-target", 1, 1, title="当前新番")
    seasonal_item.import_plan_id = "seasonal-plan"
    monkeypatch.setattr(diagnostics, "load_library_index", lambda: LibraryIndex(works=[
        WorkIndex(
            work_id="current-seasonal-card", title="当前新番", source="pan115",
            import_scope="seasonal",
        ),
    ]))
    monkeypatch.setattr(diagnostics, "load_scrape_map", lambda: ScrapeMap(items=[seasonal_item]))
    monkeypatch.setattr(
        diagnostics,
        "load_import_plan",
        lambda plan_id=None: SimpleNamespace(import_scope="seasonal"),
        raising=False,
    )

    result = diagnostics.diagnose_library_consistency("pan115")

    assert result["summary"]["seasonal_work_count"] == 1
    assert result["summary"]["checked_scrape_seasons"] == 0
    assert result["errors"] == []
    assert result["warnings"] == []


def test_diagnostics_uses_target_id_for_merged_alias_and_special(monkeypatch):
    """别名合卡与特别篇均应按稳定 target ID 对照，而不是旧标题哈希。"""
    from app.library import diagnostics
    from app.library.models import LibraryIndex, SeasonIndex, WorkIndex
    from app.scrape.models import ScrapeMap

    merged = WorkIndex(
        work_id="series-merged-by-tmdb",
        title="咒术回战",
        source="pan115",
        card_type="main_series",
        media_type="tv",
        seasons=[
            SeasonIndex(
                season_id="s1", work_id="series-merged-by-tmdb", season_number=1,
                group_type="season", label="第1季", episode_count=24,
                scrape_target_id="alias-s1", tmdb_id=95479, tmdb_type="tv",
                tmdb_season_number=1, scraped=True,
            ),
            SeasonIndex(
                season_id="sp0", work_id="series-merged-by-tmdb", season_number=0,
                group_type="special", label="特别篇", episode_count=1,
                scrape_target_id="alias-sp", tmdb_id=95479, tmdb_type="tv",
                tmdb_season_number=0, scraped=True,
            ),
        ],
    )
    monkeypatch.setattr(diagnostics, "load_library_index", lambda: LibraryIndex(works=[merged]))
    monkeypatch.setattr(diagnostics, "load_scrape_map", lambda: ScrapeMap(items=[
        _scrape_item("alias-s1", 1, 1, tmdb_id=95479, series_group="Jujutsu_Kaisen"),
        _scrape_item("alias-sp", 0, 0, tmdb_id=95479, series_group="Jujutsu_Kaisen"),
    ]))

    result = diagnostics.diagnose_library_consistency("pan115")

    assert result["ok"] is True
    assert result["errors"] == []


def test_diagnostics_accepts_merged_alias_variant_with_same_tmdb_season(monkeypatch):
    """同一 TMDB 季的中英文镜像合卡后，未被选中的别名映射不应报错。"""
    from app.library import diagnostics
    from app.library.models import LibraryIndex, WorkIndex
    from app.scrape.models import ScrapeMap

    indexed = _library_index([_season("en-s1", 1, 1, tmdb_id=95479)])
    indexed.works[0].work_id = "series-merged-by-tmdb"
    indexed.works[0].title = "咒术回战"
    indexed.works[0].seasons[0].work_id = indexed.works[0].work_id
    monkeypatch.setattr(diagnostics, "load_library_index", lambda: indexed)
    monkeypatch.setattr(diagnostics, "load_scrape_map", lambda: ScrapeMap(items=[
        _scrape_item("en-s1", 1, 1, tmdb_id=95479, series_group="Jujutsu_Kaisen"),
        _scrape_item("cn-s1", 1, 1, tmdb_id=95479, series_group="咒术回战"),
    ]))

    result = diagnostics.diagnose_library_consistency("pan115")

    assert result["ok"] is True
    assert result["errors"] == []


def test_library_diagnostics_api(monkeypatch):
    """GET /api/library/diagnostics 暴露只读诊断接口."""
    from fastapi.testclient import TestClient
    from app.api import library as library_api
    from app.main import app

    monkeypatch.setattr(
        library_api,
        "diagnose_library_consistency",
        lambda source=None: {"ok": True, "source": source, "errors": [], "warnings": []},
    )

    client = TestClient(app)
    resp = client.get("/api/library/diagnostics?source=pan115")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert resp.json()["source"] == "pan115"
