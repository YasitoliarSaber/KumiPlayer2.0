from __future__ import annotations

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.resolution.resolver import MediaResolver


def _evidence(index: int, relative_path: str, *, provider: str = "baidu") -> SourceEvidence:
    return SourceEvidence(
        evidence_id=f"ev-hierarchy-{index}",
        scan_id="scan-hierarchy",
        root_id="root-hierarchy",
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider=provider,
        ingest_method="directory_tree",
    )


def _resolve(paths: list[str], *, root_container: str = ""):
    parser = V4Parser()
    evidence = [_evidence(index, path) for index, path in enumerate(paths)]
    entries = [
        (item, parser.parse(item, root_container=root_container))
        for item in evidence
    ]
    return MediaResolver().resolve(entries)


def test_explicit_series_container_groups_seasons_and_specials_into_one_work():
    graph = _resolve(
        [
            "动画/CLANNAD.S1-S2+SP+OVA/1.CLANNAD.[S1].2007/CLANNAD [01].mkv",
            "动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.After.Story.[S2].2008/CLANNAD After Story [01].mkv",
            "动画/CLANNAD.S1-S2+SP+OVA/SP/CLANNAD [SP01].mkv",
        ]
    )

    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "CLANNAD"
    assert {episode.local_season_number for episode in graph.episodes} == {0, 1, 2}


def test_standalone_movie_inside_series_container_remains_independent_work():
    graph = _resolve(
        [
            "动画/刀剑神域.S1-S3+剧场版/1.刀剑神域.[S1].2012/刀剑神域 [01].mkv",
            "动画/刀剑神域.S1-S3+剧场版/剧场版：序列之争.2017/刀剑神域 剧场版：序列之争.mkv",
        ]
    )

    assert len(graph.works) == 2
    assert {(work.media_type, work.card_type) for work in graph.works} == {
        ("tv", "main_series"),
        ("movie", "standalone"),
    }


def test_export_duplicate_suffix_does_not_become_part_of_work_title():
    graph = _resolve(
        [
            "描绘直至生命尽头 (2026) {tmdbid-287028} [4K](1)/Season 1/描绘直至生命尽头.2026.S01E01.2160p.NF.WEBRip.HEVC.10bit.AAC.SRTx2-NoxiaAI.mkv",
            "描绘直至生命尽头 (2026) {tmdbid-287028} [4K]/Season 1/描绘直至生命尽头.2026.S01E01.2160p.NF.WEBRip.HEVC.10bit.AAC.SRTx2-NoxiaAI.mkv",
        ]
    )

    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "描绘直至生命尽头"
    assert len(graph.episodes) == 1
    assert len(graph.episodes[0].asset_evidence_ids) == 2


def test_source_scope_name_cannot_merge_unrelated_works():
    graph = _resolve(
        [
            "动画/AIR.2005/AIR [01].mkv",
            "动画/CLANNAD.2007/CLANNAD [01].mkv",
        ],
        root_container="根目录",
    )

    assert {work.preferred_title for work in graph.works} == {"AIR", "CLANNAD"}


def test_single_work_root_can_supply_identity_when_path_starts_with_season():
    graph = _resolve(
        [
            "Season 1/Yuru Camp S01E01.mkv",
            "Season 2/Yuru Camp S02E01.mkv",
        ],
        root_container="Yuru Camp",
    )

    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "Yuru Camp"
    assert {episode.local_season_number for episode in graph.episodes} == {1, 2}


def test_plain_series_collection_merges_regular_seasons_but_keeps_spinoff_and_movie_separate():
    graph = _resolve(
        [
            "[VCB-Studio] Yuru Camp/Yuru Camp/Yuru Camp [01].mkv",
            "[VCB-Studio] Yuru Camp/Yuru Camp Season 2/Yuru Camp Season 2 [01].mkv",
            "[VCB-Studio] Yuru Camp/Yuru Camp Season 3/Yuru Camp Season 3 [01].mkv",
            "[VCB-Studio] Yuru Camp/Heya Camp/Heya Camp [01].mkv",
            "[VCB-Studio] Yuru Camp/Yuru Camp Movie (2022)/Yuru Camp Movie.mkv",
            "[VCB-Studio] Yuru Camp/Specials/[VCB-Studio] Yuru Camp [SP08][Making Documentary][Ma10p_1080p].mkv",
        ]
    )

    assert {work.preferred_title for work in graph.works} == {
        "Yuru Camp",
        "Heya Camp",
        "Yuru Camp Movie",
    }
    main_work = next(work for work in graph.works if work.preferred_title == "Yuru Camp")
    assert {
        episode.local_season_number
        for episode in graph.episodes
        if episode.work_key == main_work.work_key
    } == {0, 1, 2, 3}


def test_plain_series_regular_seasons_do_not_require_a_specials_folder_to_merge():
    graph = _resolve(
        [
            "[VCB-Studio] Yuru Camp/Yuru Camp/Yuru Camp [01].mkv",
            "[VCB-Studio] Yuru Camp/Yuru Camp Season 2/Yuru Camp Season 2 [01].mkv",
            "[VCB-Studio] Yuru Camp/Yuru Camp Season 3/Yuru Camp Season 3 [01].mkv",
            "[VCB-Studio] Yuru Camp/Heya Camp/Heya Camp [01].mkv",
        ]
    )

    assert {work.preferred_title for work in graph.works} == {"Yuru Camp", "Heya Camp"}
    main_work = next(work for work in graph.works if work.preferred_title == "Yuru Camp")
    assert {
        episode.local_season_number
        for episode in graph.episodes
        if episode.work_key == main_work.work_key
    } == {1, 2, 3}


def test_category_prefix_preserves_work_container_year_and_special_membership():
    graph = _resolve(
        [
            "动画/B 86-不存在的战区.2021/86-不存在的战区-.S01E01.送葬者.mkv",
            "动画/B 86-不存在的战区.2021/special/[MAI] EIGHTY SIX [11.5].mkv",
        ]
    )

    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "86-不存在的战区"
    assert graph.works[0].year == 2021
    assert {episode.local_season_number for episode in graph.episodes} == {0, 1}


def test_series_named_collection_is_a_shared_work_boundary():
    graph = _resolve(
        [
            "动画/鬼灭之刃系列/1.立志篇.[S1].2019/Kimetsu no Yaiba [01].mkv",
            "动画/鬼灭之刃系列/3.游郭篇.[S2].2021/Kimetsu no Yaiba [34].mkv",
        ]
    )

    assert len(graph.works) == 1
    assert {episode.local_season_number for episode in graph.episodes} == {1, 2}
    assert graph.issues == ()


def test_plain_parent_with_explicit_season_children_is_a_series_boundary():
    graph = _resolve(
        [
            "动画/我推的孩子/我推的孩子.[S1].2023/我推的孩子.S01E01.mkv",
            "动画/我推的孩子/我推的孩子.[S2].2024/我推的孩子.S02E01.mkv",
        ]
    )

    assert len(graph.works) == 1
    assert {episode.local_season_number for episode in graph.episodes} == {1, 2}


def test_plain_parent_groups_regular_season_and_specials_consistently():
    graph = _resolve(
        [
            "动画/少女歌剧 (2018)/Season 1/少女歌剧.S01E01.mkv",
            "动画/少女歌剧 (2018)/Specials/少女歌剧.S00E01.mkv",
        ]
    )

    assert len(graph.works) == 1
    assert {episode.local_season_number for episode in graph.episodes} == {0, 1}


def test_special_subtitle_folder_uses_resolved_main_series_boundary():
    graph = _resolve(
        [
            "刮削好的动画/作品/Season 1/作品.S01E01.mkv",
            "刮削好的动画/作品 通向大人的阶梯/作品 通向大人的阶梯.S01E01.mkv",
        ]
    )

    assert len(graph.works) == 1
    assert {episode.local_season_number for episode in graph.episodes} == {0, 1}


def test_standalone_subwork_and_direct_recap_keep_their_own_titles():
    collection = _resolve(
        [
            "动画/刀剑神域.S1-S3+剧场版/2.刀剑神域 Extra Edition 总集篇.2013/刀剑神域 Extra Edition 总集篇.mkv",
        ]
    )
    direct = _resolve(
        [
            "动画/AIR.2005/[4K_AS] 青空&AIR 总集篇.mkv",
        ]
    )

    assert collection.works[0].preferred_title == "刀剑神域 Extra Edition 总集篇"
    assert direct.works[0].preferred_title == "青空&AIR 总集篇"


def test_movie_mv_and_teaser_are_auxiliary_not_a_fake_tv_work():
    graph = _resolve(
        [
            "动画/声之形.2016/[Ma10p_2160p][x265_2flac5.1_ass].mkv",
            "动画/声之形.2016/SPs/[MV01][Ma10p_2072p][x265_flac].mkv",
            "动画/声之形.2016/SPs/[Teaser][Ma10p_2072p][x265_flac].mkv",
        ]
    )

    assert len(graph.works) == 1
    assert graph.works[0].media_type == "movie"
    assert graph.episodes == ()


def test_v4_parser_does_not_consume_post_confirmation_verified_title_rules(monkeypatch):
    from app.media_v4.resolution.candidates import WorkCandidate, plan_work_candidates
    from app.recognition import verified_titles

    def forbidden(*_args, **_kwargs):
        raise AssertionError("V4 首次解析不应读取确认后积累的 verified_titles")

    monkeypatch.setattr(verified_titles, "match_verified_tmdb_binding", forbidden)
    monkeypatch.setattr(verified_titles, "match_verified_series_special", forbidden)
    evidence = [
        _evidence(
            0,
            "动画/[VCB-Studio] Yuru Camp/Heya Camp/Heya Camp [01].mkv",
        ),
        _evidence(
            1,
            "动画/[VCB-Studio] Yuru Camp/Yuru Camp Movie (2022)/Yuru Camp Movie.mkv",
        ),
    ]
    parser = V4Parser()
    entries = [(item, parser.parse(item)) for item in evidence]
    graph = MediaResolver().resolve(entries)

    def search(work_key, queries, _year, media_type):
        rows = []
        if "Heya Camp" in queries:
            rows.append(WorkCandidate(
                work_key=work_key,
                provider="tmdb",
                provider_id="92684",
                media_type="tv",
                title="Heya Camp",
                year=2020,
                evidence="online_search",
                confidence="medium",
            ))
        if "Yuru Camp Movie" in queries:
            rows.append(WorkCandidate(
                work_key=work_key,
                provider="tmdb",
                provider_id="566466",
                media_type="movie",
                title="Yuru Camp Movie",
                year=2022,
                evidence="online_search",
                confidence="medium",
            ))
        # 即使搜索器能返回父系列，它也不能凭关系标题制造第二个可信身份。
        if "Yuru Camp" in queries:
            rows.append(WorkCandidate(
                work_key=work_key,
                provider="tmdb",
                provider_id="76075",
                media_type=media_type,
                title="Yuru Camp",
                year=2018,
                evidence="online_search",
                confidence="medium",
            ))
        return rows

    candidates, _merge_map, issues = plan_work_candidates(graph, entries, search)

    assert {work.preferred_title for work in graph.works} == {"Heya Camp", "Yuru Camp Movie"}
    assert not any(issue.code == "candidate_ambiguous" for issue in issues)
    assert {
        candidate.provider_id
        for rows in candidates.values()
        for candidate in rows
        if candidate.status == "confirmed"
    } == {"92684", "566466"}


def test_batch_parser_rebases_continuous_absolute_numbers_in_later_season():
    from app.media_v4.parsing.parser import normalize_batch_parsed_facts

    parser = V4Parser()
    entries = [
        (
            _evidence(
                index,
                f"动画/Yuru Camp/Season 2/Yuru Camp S02E{number:02d}.mkv",
            ),
            parser.parse(
                _evidence(
                    index,
                    f"动画/Yuru Camp/Season 2/Yuru Camp S02E{number:02d}.mkv",
                )
            ),
        )
        for index, number in enumerate(range(13, 16))
    ]

    normalized = normalize_batch_parsed_facts(entries)

    assert [facts.season_candidate for _evidence, facts in normalized] == [2, 2, 2]
    assert [facts.episode_candidate for _evidence, facts in normalized] == [1, 2, 3]
