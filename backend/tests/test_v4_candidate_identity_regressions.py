"""候选身份的通用回归：关系标题、显式 Provider 类型和电影资产收口。"""

from __future__ import annotations


def _entry(
    evidence_id: str,
    *,
    work_title: str,
    title_candidates: tuple[str, ...],
    media_type: str = "tv",
    group_type: str = "season",
    tmdb_hint_id: int | None = None,
    tmdb_hint_type: str = "",
    series_group: str = "",
    relation_type: str = "",
):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-candidate-identity",
        root_id="root-candidate-identity",
        source_key=evidence_id,
        relative_path=f"Library/{work_title}/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title=work_title,
        title_candidates=title_candidates,
        media_type=media_type,
        group_type=group_type,
        season_candidate=1,
        episode_candidate=1,
        tmdb_hint_id=tmdb_hint_id,
        tmdb_hint_type=tmdb_hint_type,
        series_group=series_group,
        relation_type=relation_type,
        confidence="high",
    )
    return evidence, facts


def test_spinoff_parent_title_cannot_promote_parent_candidate_to_high_confidence():
    """父系列仅表示关系；它不能成为外传自身的身份检索或评分证据。"""

    from app.media_v4.resolution.candidates import WorkCandidate, plan_work_candidates
    from app.media_v4.resolution.resolver import MediaResolver

    entry = _entry(
        "spinoff",
        work_title="Side Story",
        title_candidates=("Side Story", "Parent Series"),
        series_group="Parent Series",
        relation_type="spin_off",
    )
    graph = MediaResolver().resolve([entry])

    def search(work_key, queries, _year, _media_type):
        candidates = []
        if "Parent Series" in queries:
            candidates.append(WorkCandidate(
                work_key=work_key,
                provider="tmdb",
                provider_id="100",
                media_type="tv",
                title="Parent Series",
                year=None,
                evidence="online_search",
                confidence="medium",
            ))
        if "Side Story" in queries:
            candidates.append(WorkCandidate(
                work_key=work_key,
                provider="tmdb",
                provider_id="200",
                media_type="tv",
                title="Side Story",
                year=None,
                evidence="online_search",
                confidence="medium",
            ))
        return candidates

    candidates, _merge_map, issues = plan_work_candidates(graph, [entry], search)

    assert not any(issue.code == "candidate_ambiguous" for issue in issues)
    result = candidates[graph.works[0].work_key]
    assert [(item.provider_id, item.status) for item in result] == [("200", "confirmed")]


def test_explicit_tmdb_hint_keeps_provider_and_media_type_in_their_correct_fields():
    """显式 TMDB movie/tv 提示必须是一条统一三元身份，不能伪造第二候选。"""

    from app.media_v4.resolution.candidates import plan_work_candidates
    from app.media_v4.resolution.resolver import MediaResolver

    entry = _entry(
        "hinted-movie",
        work_title="A Film",
        title_candidates=("A Film",),
        media_type="movie",
        group_type="movie",
        tmdb_hint_id=42,
        tmdb_hint_type="movie",
    )
    graph = MediaResolver().resolve([entry])
    candidates, _merge_map, issues = plan_work_candidates(graph, [entry], lambda *_args: [])

    assert not any(issue.code == "candidate_ambiguous" for issue in issues)
    result = candidates[graph.works[0].work_key]
    assert [(item.provider, item.media_type, item.provider_id, item.status) for item in result] == [
        ("tmdb", "movie", "42", "confirmed"),
    ]


def test_explicit_movie_identity_turns_special_folder_files_into_movie_assets():
    """明确电影身份优先于路径中的 SP/菜单语义，避免电影被展示成 TV。"""

    from app.media_v4.resolution.resolver import MediaResolver

    entry = _entry(
        "movie-special",
        work_title="A Film",
        title_candidates=("A Film",),
        media_type="tv",
        group_type="special",
        tmdb_hint_id=42,
        tmdb_hint_type="movie",
    )
    graph = MediaResolver().resolve([entry])

    assert graph.works[0].media_type == "movie"
    assert graph.episodes == ()
    assert graph.work_assets[0].asset_evidence_ids == ("movie-special",)
