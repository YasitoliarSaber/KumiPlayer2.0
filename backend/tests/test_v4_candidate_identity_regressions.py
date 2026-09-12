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


def test_verified_heya_camp_path_confirms_the_spinoff_without_online_search():
    """已核验外传路径在第二步即冻结身份，第三步不应落入人工确认。"""

    from app.media_v4.resolution.candidates import plan_work_candidates
    from app.media_v4.resolution.resolver import MediaResolver

    entry = _entry(
        "heya-camp",
        work_title="Heya Camp△",
        title_candidates=("Heya Camp△",),
        series_group="Yuru Camp",
        relation_type="spin_off",
    )
    graph = MediaResolver().resolve([entry])

    candidates, _merge_map, issues = plan_work_candidates(
        graph,
        [entry],
        lambda *_args: [],
    )

    assert issues == []
    work = next(item for item in graph.works if "heya camp" in item.preferred_title.casefold())
    assert [
        (item.provider, item.media_type, item.provider_id, item.status)
        for item in candidates[work.work_key]
    ] == [("tmdb", "tv", "95213", "confirmed")]


def test_bonus_filename_cannot_assign_spinoff_identity_to_parent_work():
    from dataclasses import replace

    from app.media_v4.resolution.candidates import plan_work_candidates
    from app.media_v4.resolution.resolver import MediaResolver

    main = _entry("main", work_title="Yuru Camp", title_candidates=("Yuru Camp",))
    # 旧草稿保存的是解析后截断的文件名，不只是原始路径。
    bonus = _entry("bonus", work_title="Yuru Camp", title_candidates=("Yuru Camp", "Yuru Camp [Heya Camp"))
    bonus = (replace(bonus[0], relative_path="Yuru Camp/SP/Heya Camp EP00.mkv"), replace(bonus[1], group_type="special", special_candidate=True))
    side = _entry("side", work_title="Heya Camp", title_candidates=("Heya Camp",), series_group="Yuru Camp", relation_type="spin_off")
    entries = [main, bonus, side]
    graph = MediaResolver().resolve(entries)
    candidates, merge, issues = plan_work_candidates(graph, entries, lambda *_: [])
    parent = next(work for work in graph.works if work.preferred_title == "Yuru Camp")
    assert not any(c.provider_id == "95213" and c.status == "confirmed" for c in candidates[parent.work_key])
    assert not any(issue.code == "work_identity_conflict" for issue in issues)
    assert len(set(merge.values())) == len(merge)


def test_special_alias_does_not_promote_unrelated_online_candidate():
    from dataclasses import replace

    from app.media_v4.resolution.candidates import WorkCandidate, plan_work_candidates
    from app.media_v4.resolution.resolver import MediaResolver

    entry = _entry("bonus", work_title="Parent Show", title_candidates=("Parent Show", "Other Show"))
    entry = (entry[0], replace(entry[1], group_type="special", special_candidate=True))
    graph = MediaResolver().resolve([entry])
    requested = []

    def search(key, queries, _year, _media_type):
        requested.extend(queries)
        return [WorkCandidate(key, "tmdb", "200", "tv", "Other Show", None, "online_search", "high")]

    candidates, _, _ = plan_work_candidates(graph, [entry], search)
    assert "Parent Show" in requested
    assert "Other Show" not in requested
    assert all(item.status != "confirmed" for item in candidates[graph.works[0].work_key])


def test_work_title_precedes_release_variants_without_losing_regular_episode_alias():
    from dataclasses import replace

    from app.media_v4.resolution.candidates import build_query_inputs
    from app.media_v4.resolution.resolver import MediaResolver

    entry = _entry("regular", work_title="本地作品名", title_candidates=("Original Title",))
    work = replace(MediaResolver().resolve([entry]).works[0], preferred_title="系列作品名")
    facts = [replace(entry[1], original_title=f"Release variant {index}") for index in range(12)]
    queries = build_query_inputs(work, [entry], related_facts=facts)
    assert queries[:3] == ["系列作品名", "本地作品名", "Original Title"]
    assert len(queries) <= 8


def test_real_camp_bundle_rechecks_polluted_frozen_candidate():
    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.parsing.parser import V4Parser
    from app.media_v4.resolution.candidates import WorkCandidate, plan_work_candidates
    from app.media_v4.resolution.resolver import MediaResolver

    root = "[VCB-Studio] Yuru Camp"
    release = "[Airota&Nekomoe kissaten&VCB-Studio]"
    paths = [
        f"{root}/{release} Yuru Camp Season 2 [Ma10p_1080p]/{release} Yuru Camp Season 2 [01][Ma10p_1080p].mkv",
        f"{root}/{release} Yuru Camp [Ma10p_1080p]/{release} Yuru Camp [Heya Camp EP00][Ma10p_1080p].mkv",
        f"{root}/{release} Heya Camp [Ma10p_1080p]/{release} Heya Camp [01][Ma10p_1080p].mkv",
        f"{root}/{release} Yuru Camp Movie [Ma10p_1080p]/{release} Yuru Camp Movie [Ma10p_1080p].mkv",
    ]
    entries = []
    for index, path in enumerate(paths):
        evidence = SourceEvidence(str(index), "scan", "root", str(index), path, "video", provider="local")
        entries.append((evidence, V4Parser().parse(evidence)))
    graph = MediaResolver().resolve(entries)
    assert len(graph.works) == 3

    def frozen(key, *_args):
        return [WorkCandidate(key, "tmdb", "95213", "tv", "Heya Camp△", None, "verified_path_binding", "high", "rejected")]

    for preserve in (False, True):
        candidates, merge, issues = plan_work_candidates(graph, entries, frozen, preserve_candidate_status=preserve)
        assert issues == []
        assert not merge
        for work in graph.works:
            confirmed = [item.provider_id for item in candidates[work.work_key] if item.status == "confirmed"]
            if work.preferred_title == "Yuru Camp":
                assert confirmed == []
            elif work.media_type == "movie":
                assert confirmed == ["566466"]
            else:
                assert confirmed == ["95213"]


def test_confirmation_does_not_save_bonus_or_parent_titles_as_work_aliases(tmp_path):
    from dataclasses import replace

    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    main = _entry("main", work_title="Parent Show", title_candidates=("Parent Show", "Original Title"))
    bonus = _entry("bonus", work_title="Parent Show", title_candidates=("Parent Show", "Unrelated Bonus"))
    bonus = (bonus[0], replace(bonus[1], group_type="special", special_candidate=True, special_number=1))
    side = _entry("side", work_title="Side Story", title_candidates=("Side Story", "Parent Show"), series_group="Parent Show", relation_type="spin_off")
    database = V4Database(tmp_path / "aliases.db")
    database.initialize()
    service = V4RevisionService(database)
    service.create_draft("rev-alias", [main, bonus, side])
    service.confirm("rev-alias")
    with database.connect() as conn:
        aliases = {(row[0], row[1]) for row in conn.execute(
            "SELECT w.preferred_title, a.normalized_title FROM works w JOIN work_aliases a ON a.work_id=w.work_id",
        )}
    assert ("Parent Show", "original title") in aliases
    assert ("Parent Show", "unrelated bonus") not in aliases
    assert ("Side Story", "parent show") not in aliases


def test_frozen_historical_candidate_cannot_restore_released_binding():
    from app.media_v4.resolution.candidates import WorkCandidate, plan_work_candidates
    from app.media_v4.resolution.resolver import MediaResolver

    entry = _entry("fresh", work_title="Correct", title_candidates=("Correct",))
    graph = MediaResolver().resolve([entry])
    work = graph.works[0]
    for evidence in ("existing_provider_binding", "verified_path_binding"):
        old = WorkCandidate(work_key=work.work_key, provider="tmdb", media_type="tv", provider_id="100", title="Correct", year=None, evidence=evidence, confidence="high", status="confirmed")
        candidates, _, _ = plan_work_candidates(graph, [entry], lambda *_, candidate=old: [candidate], existing_bindings={}, preserve_candidate_status=True)
        assert not any(item.status == "confirmed" for item in candidates[work.work_key])
