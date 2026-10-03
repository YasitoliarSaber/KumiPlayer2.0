"""标题仲裁必须解释最终事实，并保留成熟识别器的作品边界。"""

import pytest

from app.media_v4.parsing.parser import V4Parser
from app.media_v4.sources.adapters import SourceEntry, to_source_evidence


@pytest.mark.parametrize(
    ("path", "title", "series", "content_class", "discarded_title"),
    [
        (
            "刮削好的动画/石纪元 (2019) {tmdbid-86031} [4K]/Season 1/"
            "石纪元 - S01E15 - 200万年的结晶.mkv",
            "石纪元", "石纪元", "regular", "石纪元 (2019) {} [4K]",
        ),
        (
            "刮削好的动画/【我推的孩子】 (2023) {tmdbid-203737} [4K]/Season 1/"
            "【我推的孩子】.S01E01.母与子.mkv",
            "【我推的孩子】", "【我推的孩子】", "regular", "我推的孩子",
        ),
        (
            "动画/莉可丽丝 (2022)/Season 1/Lycoris Recoil S01E01-[1080p].mkv",
            "莉可丽丝", "莉可丽丝", "regular", "莉可丽丝 (2022)",
        ),
        (
            "Show/Movie/Show The Movie.mkv",
            "Show The Movie", "Show The Movie", "movie", "Movie",
        ),
        (
            "刮削好的动画/石纪元 (2019) {tmdbid-86031} [4K]/Specials/"
            "石纪元 - S00E03 - 龙水.mkv",
            "石纪元", "石纪元", "attached_special", "石纪元 (2019) {} [4K]",
        ),
    ],
)
def test_title_trace_matches_final_title_without_changing_work_boundaries(
    path: str, title: str, series: str, content_class: str, discarded_title: str,
) -> None:
    evidence = to_source_evidence(SourceEntry(
        root_id="offline-title", scan_id="offline-title", provider="baidu",
        ingest_method="directory_tree", relative_path=path, playback_locator=f"fixture://{path}",
    ))

    facts = V4Parser().parse(evidence)

    assert facts.work_title == title
    assert facts.series_group == series
    assert facts.content_class == content_class
    title_traces = [trace for trace in facts.decision_trace if trace.field == "work_title"]
    assert len(title_traces) == 1
    assert title_traces[0].value == facts.work_title
    assert discarded_title in title_traces[0].alternatives
    assert facts.work_title not in title_traces[0].alternatives
    assert title_traces[0].rule_id == "recognized_work_title"
    candidate_traces = [trace for trace in facts.decision_trace if trace.field == "work_title_candidate"]
    assert len(candidate_traces) == 1
    assert candidate_traces[0].value == discarded_title
    assert candidate_traces[0].rule_id in {"explicit_file_title", "nearest_semantic_directory"}
    assert candidate_traces[0].origin == title_traces[0].origin
    assert candidate_traces[0].scope == title_traces[0].scope
    if content_class == "movie":
        assert facts.card_type == "standalone"
    if "Lycoris Recoil" in path:
        assert "Lycoris Recoil" in facts.title_candidates


@pytest.mark.parametrize("filename", ["Show.S02E01.mkv", "Show OVA01.mkv", "S02E01.mkv"])
def test_existing_consistent_title_trace_preserves_season_and_special_decisions(filename: str) -> None:
    evidence = to_source_evidence(SourceEntry(
        root_id="offline-title", scan_id="offline-title", provider="baidu",
        ingest_method="directory_tree", relative_path=f"Show/Season 2/{filename}",
    ))

    facts = V4Parser().parse(evidence)

    assert facts.work_title == facts.series_group == "Show"
    assert facts.season_candidate == 2
    assert all(trace.value == "Show" for trace in facts.decision_trace if trace.field == "work_title")
    assert facts.content_class == ("playable_special" if "OVA" in filename else "regular")


def test_season_subtitle_keeps_parent_series_identity() -> None:
    path = "星空的炼金师/星空的炼金师 第3季 银月篇/星空的炼金师 第3季 银月篇 [01].mkv"
    evidence = to_source_evidence(SourceEntry(
        root_id="offline-title", scan_id="offline-title", provider="baidu",
        ingest_method="directory_tree", relative_path=path,
    ))

    facts = V4Parser().parse(evidence)

    assert facts.work_title == "星空的炼金师 第3季 银月篇"
    assert facts.series_group == "星空的炼金师"
    assert facts.season_candidate == 3
    assert facts.card_type == "main_series"
    assert facts.content_class == "regular"
    assert all(trace.value == facts.work_title for trace in facts.decision_trace if trace.field == "work_title")


def test_movie_identity_stays_independent_while_selected_special_belongs_to_series() -> None:
    from app.media_v4.resolution.resolver import MediaResolver

    paths = [
        "Show/Season 1/Show.S01E01.mkv",
        "Show/Season 1/Show OVA01.mkv",
        "Show/Movie/Show The Movie.mkv",
    ]
    entries = []
    for path in paths:
        evidence = to_source_evidence(SourceEntry(
            root_id="offline-title", scan_id="offline-title", provider="baidu",
            ingest_method="directory_tree", relative_path=path,
        ))
        entries.append((evidence, V4Parser().parse(evidence)))

    graph = MediaResolver().resolve(entries)

    assert len(graph.works) == 2
    series = next(work for work in graph.works if work.media_type == "tv")
    movie = next(work for work in graph.works if work.media_type == "movie")
    assert series.preferred_title == "Show"
    assert movie.preferred_title == "Show The Movie"
    assert set(series.source_evidence_ids) == {entries[0][0].evidence_id, entries[1][0].evidence_id}
    assert set(movie.source_evidence_ids) == {entries[2][0].evidence_id}
    special = next(episode for episode in graph.episodes if episode.season_kind == "special")
    assert special.work_key == series.work_key
