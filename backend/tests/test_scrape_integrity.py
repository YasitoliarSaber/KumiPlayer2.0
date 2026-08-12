# -*- coding: utf-8 -*-

from pathlib import Path


def _target(tmp_path: Path, title: str = "明日同学的水手服"):
    from app.scrape.models import ScrapeTarget

    target_dir = tmp_path / title / "Season 1"
    return ScrapeTarget(
        scrape_target_id="target-current",
        source="baidu",
        work_id="work-current",
        card_type="main_series",
        media_type="tv",
        group_type="season",
        series_group=title,
        local_title=title,
        scrape_title=title,
        scrape_type="tv",
        local_season_number=1,
        target_dir=str(target_dir),
        target_nfo_path=str(target_dir / "tvshow.nfo"),
    )


def test_integrity_reports_wrong_title_binding_without_deleting_files(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap

    target = _target(tmp_path)
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text(
        "<tvshow><title>明日的与一</title><tmdbid>46033</tmdbid></tvshow>",
        encoding="utf-8",
    )

    result = audit_scrape_integrity([target], ScrapeMap())

    assert result["summary"]["wrong_binding_count"] == 1
    assert result["issues"][0]["code"] == "wrong_binding"
    assert result["issues"][0]["cleanup_preview"][0] == str(nfo)
    assert nfo.exists()


def test_integrity_accepts_matching_nfo_and_map_by_path_when_target_id_drifted(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    target = _target(tmp_path, "CLANNAD")
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text(
        "<tvshow><title>CLANNAD</title><tmdbid>24835</tmdbid></tvshow>",
        encoding="utf-8",
    )
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id="target-old",
        work_id="work-old",
        source="baidu",
        card_type="main_series",
        media_type="tv",
        series_group="CLANNAD",
        local_title="CLANNAD",
        local_season_number=1,
        scrape_title="CLANNAD",
        tmdb_id=24835,
        tmdb_type="tv",
        tmdb_season_number=1,
        nfo_path=str(nfo),
    )])

    result = audit_scrape_integrity([target], scrape_map)

    assert result["summary"]["wrong_binding_count"] == 0
    assert not any(issue["code"] == "stale_map" for issue in result["issues"])


def test_integrity_reports_missing_metadata_for_unmapped_target(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap

    result = audit_scrape_integrity([_target(tmp_path)], ScrapeMap())

    assert result["issues"][0]["code"] == "missing_metadata"


def test_integrity_reports_map_and_nfo_that_are_consistently_bound_to_wrong_work(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    target = _target(tmp_path)
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text(
        "<tvshow><title>明日的与一</title><tmdbid>46033</tmdbid></tvshow>",
        encoding="utf-8",
    )
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id="target-old",
        work_id="work-old",
        source="baidu",
        card_type="main_series",
        media_type="tv",
        series_group="明日同学的水手服",
        local_title="明日同学的水手服",
        scrape_title="明日同学的水手服",
        local_season_number=1,
        tmdb_id=46033,
        tmdb_type="tv",
        tmdb_season_number=1,
        nfo_path=str(nfo),
    )])

    result = audit_scrape_integrity([target], scrape_map)

    assert result["issues"][0]["code"] == "wrong_binding"


def test_integrity_does_not_require_tvshow_nfo_for_movie_owned_specials(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap

    target = _target(tmp_path, "剧场版：Take On Me")
    target.group_type = "special"
    target.media_type = "movie"
    target.local_season_number = 0
    movie_nfo = Path(target.target_dir).parent / "movie.nfo"
    movie_nfo.parent.mkdir(parents=True)
    movie_nfo.write_text("<movie><title>剧场版：Take On Me</title></movie>", encoding="utf-8")

    result = audit_scrape_integrity([target], ScrapeMap())

    assert result["issues"] == []


def test_integrity_reports_orphan_map_not_present_in_current_targets(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    nfo = tmp_path / "orphan" / "tvshow.nfo"
    nfo.parent.mkdir(parents=True)
    nfo.write_text("<tvshow><title>苍蓝钢铁的琶音</title><tmdbid>57406</tmdbid></tvshow>", encoding="utf-8")
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id="orphan-old",
        source="baidu",
        card_type="main_series",
        media_type="tv",
        series_group="Steel Ball Run：JoJo no Kimyou na Bouken",
        local_title="Steel Ball Run：JoJo no Kimyou na Bouken",
        tmdb_id=57406,
        tmdb_type="tv",
        nfo_path=str(nfo),
    )])

    result = audit_scrape_integrity([], scrape_map)

    assert result["issues"][0]["code"] == "orphan_map"


def test_integrity_marks_cross_language_legacy_map_as_unverified_not_wrong(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    target = _target(tmp_path, "Dandadan")
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text("<tvshow><title>胆大党</title><tmdbid>240411</tmdbid></tvshow>", encoding="utf-8")
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id=target.scrape_target_id,
        source="baidu",
        card_type="main_series",
        media_type="tv",
        series_group="Dandadan",
        local_title="Dandadan",
        tmdb_id=240411,
        tmdb_type="tv",
        nfo_path=str(nfo),
    )])

    result = audit_scrape_integrity([target], scrape_map)

    assert result["issues"][0]["code"] == "identity_unverified"
    assert result["summary"]["wrong_binding_count"] == 0


def test_integrity_accepts_persisted_provider_alias_evidence(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    target = _target(tmp_path, "Dandadan")
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text("<tvshow><title>胆大党</title><tmdbid>240411</tmdbid></tvshow>", encoding="utf-8")
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id=target.scrape_target_id,
        source="baidu",
        card_type="main_series",
        media_type="tv",
        series_group="Dandadan",
        local_title="Dandadan",
        tmdb_id=240411,
        tmdb_type="tv",
        nfo_path=str(nfo),
        identity_evidence={
            "provider": "anilist",
            "candidate_title": "胆大党",
            "provider_title_aliases": ["Dandadan", "胆大党"],
            "provider_tmdb_link": "direct",
        },
    )])

    result = audit_scrape_integrity([target], scrape_map)

    assert result["issues"] == []


def test_integrity_accepts_explicitly_verified_manual_binding(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    target = _target(tmp_path, "异世界舅舅")
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text("<tvshow><title>异世界归来的舅舅</title><tmdbid>127714</tmdbid></tvshow>", encoding="utf-8")
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id=target.scrape_target_id,
        source="baidu",
        card_type="main_series",
        media_type="tv",
        series_group="异世界舅舅",
        local_title="异世界舅舅",
        tmdb_id=127714,
        tmdb_type="tv",
        nfo_path=str(nfo),
        identity_evidence={"provider": "manual_audit", "provider_tmdb_link": "explicit_verified"},
    )])

    assert audit_scrape_integrity([target], scrape_map)["issues"] == []


def test_integrity_accepts_stable_cjk_subject_inside_mixed_local_title(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    target = _target(tmp_path, "东京教父.Tokyo.Godfathers")
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text("<tvshow><title>东京教父</title><tmdbid>13398</tmdbid></tvshow>", encoding="utf-8")
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id=target.scrape_target_id, source="baidu", card_type="main_series",
        media_type="tv", series_group=target.series_group, local_title=target.local_title,
        tmdb_id=13398, tmdb_type="tv", nfo_path=str(nfo),
    )])

    assert audit_scrape_integrity([target], scrape_map)["issues"] == []


def test_integrity_accepts_verified_tmdb_binding_despite_translated_title(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    target = _target(tmp_path, "红辣椒.Paprika")
    target.scrape_type = "movie"
    target.group_type = "movie"
    target.target_nfo_path = str(Path(target.target_dir) / "movie.nfo")
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text("<movie><title>红辣椒</title><tmdbid>4977</tmdbid></movie>", encoding="utf-8")
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id=target.scrape_target_id,
        source="pan115",
        card_type="standalone",
        media_type="movie",
        series_group=target.series_group,
        local_title=target.local_title,
        tmdb_id=4977,
        tmdb_type="movie",
        nfo_path=str(nfo),
    )])

    assert audit_scrape_integrity([target], scrape_map)["issues"] == []


def test_integrity_rejects_verified_bocchi_upper_half_bound_to_lower_half(tmp_path):
    from app.scrape.integrity import audit_scrape_integrity
    from app.scrape.models import ScrapeMap, ScrapeMapItem

    title = "剧场总集篇 孤独摇滚！Re-"
    target = _target(tmp_path, title)
    target.scrape_type = "movie"
    target.group_type = "movie"
    target.source_subwork_dir = f"{title} (2024)"
    target.target_nfo_path = str(Path(target.target_dir) / "movie.nfo")
    nfo = Path(target.target_nfo_path)
    nfo.parent.mkdir(parents=True)
    nfo.write_text(
        "<movie><title>孤独摇滚 (下)</title><tmdbid>1201387</tmdbid></movie>",
        encoding="utf-8",
    )
    scrape_map = ScrapeMap(items=[ScrapeMapItem(
        scrape_target_id=target.scrape_target_id,
        source="pan115",
        card_type="standalone",
        media_type="movie",
        series_group=target.series_group,
        local_title=target.local_title,
        source_subwork_dir=target.source_subwork_dir,
        tmdb_id=1201387,
        tmdb_type="movie",
        nfo_path=str(nfo),
    )])

    result = audit_scrape_integrity([target], scrape_map)

    assert result["issues"][0]["code"] == "wrong_binding"
