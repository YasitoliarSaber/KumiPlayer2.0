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


def _parse_facts(relative_path: str, *, root_container: str = ""):
    """单独解析一条证据，供「特别篇不入库」的反向断言使用。"""

    return V4Parser().parse(_evidence(0, relative_path), root_container=root_container)


def test_explicit_series_container_groups_seasons_and_specials_into_one_work():
    """显式系列容器内正片和编号 SP 同属一个 Work，季集身份分别保存。"""

    special_path = "动画/CLANNAD.S1-S2+SP+OVA/SP/CLANNAD [SP01].mkv"
    graph = _resolve(
        [
            "动画/CLANNAD.S1-S2+SP+OVA/1.CLANNAD.[S1].2007/CLANNAD [01].mkv",
            "动画/CLANNAD.S1-S2+SP+OVA/2.CLANNAD.After.Story.[S2].2008/CLANNAD After Story [01].mkv",
            special_path,
        ]
    )

    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "CLANNAD"
    assert {episode.local_season_number for episode in graph.episodes} == {0, 1, 2}
    assert _parse_facts(special_path).is_importable is True
    assert sum(episode.episode_kind == "special" for episode in graph.episodes) == 1


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
    } == {1, 2, 3}
    # 用户规则（2026-09-24）：Special 目录下的 SP08 不入库，也不会造出 S00。
    assert _parse_facts(
        "[VCB-Studio] Yuru Camp/Specials/[VCB-Studio] Yuru Camp [SP08][Making Documentary][Ma10p_1080p].mkv"
    ).is_importable is False
    assert all(episode.episode_kind != "special" for episode in graph.episodes)


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


def test_release_group_subdirectories_keep_main_seasons_together_without_absorbing_spinoff_or_movie():
    """真实发布组目录必须保持主系列 S1/S2/SP 的同一 Work 身份。"""

    graph = _resolve(
        [
            "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Ma10p_1080p]/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [01].mkv",
            "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Ma10p_1080p]/SPs/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [SP08][Making Documentary][Ma10p_1080p].mkv",
            "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [Ma10p_1080p]/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Season 2 [01].mkv",
            "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [Ma10p_1080p]/[Airota&Nekomoe kissaten&VCB-Studio] Heya Camp [01].mkv",
            "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_1080p]/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie.mkv",
            "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie [Ma10p_2160p]/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp Movie.2160p.mkv",
        ]
    )

    assert {(work.preferred_title, work.media_type) for work in graph.works} == {
        ("Yuru Camp", "tv"),
        ("Heya Camp", "tv"),
        ("Yuru Camp Movie", "movie"),
    }
    main_work = next(work for work in graph.works if work.preferred_title == "Yuru Camp")
    assert {
        episode.local_season_number
        for episode in graph.episodes
        if episode.work_key == main_work.work_key
    } == {1, 2}
    # 用户规则（2026-09-24）：SPs 子目录里的 SP08 同样不入库。
    assert _parse_facts(
        "[VCB-Studio] Yuru Camp/[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [Ma10p_1080p]/SPs/"
        "[Airota&Nekomoe kissaten&VCB-Studio] Yuru Camp [SP08][Making Documentary][Ma10p_1080p].mkv"
    ).is_importable is False
    assert all(episode.episode_kind != "special" for episode in graph.episodes)
    movie_work = next(work for work in graph.works if work.preferred_title == "Yuru Camp Movie")
    movie_asset = next(asset for asset in graph.work_assets if asset.work_key == movie_work.work_key)
    assert movie_asset.asset_evidence_ids == (
        "ev-hierarchy-4",
        "ev-hierarchy-5",
    )


def test_title_number_after_episode_token_never_expands_into_ghost_episodes():
    graph = _resolve(
        ["Show/Season 1/Show - S01E15 - 200万年的结晶.mkv"]
    )

    assert [episode.local_episode_number for episode in graph.episodes] == [15]
    assert len(graph.episodes[0].asset_evidence_ids) == 1


# 已删除 test_compound_special_tokens_stay_distinct_and_duplicate_versions_share_one_episode：
# 该用例只断言 SP01_01/SP01_02 这类特别篇的编号区分与重复版本合并。用户 2026-09-24 规则
# 取消了 special 入库，特别篇不再进入 Work/Season/Episode/Asset 图，该契约已被取消。


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
    # 小数集号归特别篇，季度正片不变。
    assert _parse_facts(
        "动画/B 86-不存在的战区.2021/special/[MAI] EIGHTY SIX [11.5].mkv"
    ).is_importable is True
    assert sum(episode.episode_kind == "special" for episode in graph.episodes) == 1


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


def test_plain_parent_keeps_regular_season_and_excludes_specials():
    graph = _resolve(
        [
            "动画/少女歌剧 (2018)/Season 1/少女歌剧.S01E01.mkv",
            "动画/少女歌剧 (2018)/Specials/少女歌剧.S00E01.mkv",
        ]
    )

    assert len(graph.works) == 1
    assert {episode.local_season_number for episode in graph.episodes} == {1}
    # 用户规则（2026-09-24）：S00E01 属于特别篇，不入库。
    assert _parse_facts(
        "动画/少女歌剧 (2018)/Specials/少女歌剧.S00E01.mkv"
    ).is_importable is False
    assert all(episode.local_season_number != 0 for episode in graph.episodes)


def test_special_subtitle_folder_is_excluded_from_main_series_boundary():
    """特别篇字幕文件夹不再并入主系列；主系列自身的作品边界保持不变。"""

    special_path = "刮削好的动画/作品 通向大人的阶梯/作品 通向大人的阶梯.S01E01.mkv"
    graph = _resolve(
        [
            "刮削好的动画/作品/Season 1/作品.S01E01.mkv",
            special_path,
        ]
    )

    assert len(graph.works) == 1
    assert graph.works[0].preferred_title == "作品"
    assert {episode.local_season_number for episode in graph.episodes} == {1}
    # 用户规则（2026-09-24）：通向大人的阶梯属于特别篇，不入库也不参与作品边界。
    assert _parse_facts(special_path).is_importable is False
    assert all(episode.episode_kind != "special" for episode in graph.episodes)


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
    # 该合同只约束纯解析器；候选阶段可以读取独立维护的已核验身份事实。
    monkeypatch.undo()

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
    # 候选阶段允许读取已核验身份事实：Heya Camp 的核验绑定（95213）按
    # D2 采用顺序压过在线搜索候选（92684，降级为 rejected），剧场版用
    # 搜索候选（566466）确认；两作品各只有一个高置信身份，无假歧义。
    assert {
        candidate.provider_id
        for rows in candidates.values()
        for candidate in rows
        if candidate.status == "confirmed"
    } == {"95213", "566466"}


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

    # C-004：取消"最小编号 - 1"的减偏移推断。缺集不能被当成整季起点，
    # 文件里写的就是 S02E13/E14/E15，就保持 13/14/15。
    assert [facts.season_candidate for _evidence, facts in normalized] == [2, 2, 2]
    assert [facts.episode_candidate for _evidence, facts in normalized] == [13, 14, 15]


def test_batch_parser_prefers_explicit_season_directory_over_stale_filename_season():
    """季度目录是批次级结构证据，优先于沿用上一季编号的文件名。"""

    from app.media_v4.parsing.parser import normalize_batch_parsed_facts

    parser = V4Parser()
    entries = []
    for index, number in enumerate(range(12, 25)):
        evidence = _evidence(
            index,
            f"动画/我推的孩子/我推的孩子.[S2].2024/我推的孩子.S01E{number:02d}.mkv",
        )
        entries.append((evidence, parser.parse(evidence)))

    normalized = normalize_batch_parsed_facts(entries)

    # C-004：文件级显式季号优先于目录声明；目录与本文件冲突时记录
    # season_evidence_conflict，但**不改写**文件里的本地编号。
    assert [facts.season_candidate for _evidence, facts in normalized] == [1] * 13
    assert [facts.episode_candidate for _evidence, facts in normalized] == list(range(12, 25))
    assert all(
        "season_evidence_conflict" in facts.reasons or "season_evidence_conflict" in facts.numbering.basis
        for _evidence, facts in normalized
    )


def test_verified_absolute_release_is_mapped_to_provider_season_without_guessing():
    """无 S2 标记的绝对编号发布包，只能由已核验身份规则自动换季。"""

    from app.media_v4.parsing.parser import normalize_batch_parsed_facts

    parser = V4Parser()
    entries = []
    for index, number in enumerate(range(13, 25)):
        evidence = _evidence(
            index,
            "动画/[Nekomoe kissaten&LoliHouse] Dandadan [13-24][WebRip 1080p]/"
            f"[Nekomoe kissaten&LoliHouse] Dandadan - {number:02d} [WebRip 1080p].mkv",
        )
        entries.append((evidence, parser.parse(evidence)))

    normalized = normalize_batch_parsed_facts(entries)

    # C-004：已核验 Provider 季度只作为**映射输入**，不再改写本地事实。
    # 发布包没有 S2 标记，就保留本地"第 13..24 集"；绝对编号不再由已核验
    # 表反向推算，`- 13` 这类完整编号位置就是本地集号。
    assert [facts.episode_candidate for _evidence, facts in normalized] == list(range(13, 25))
    assert [facts.absolute_episode_candidate for _evidence, facts in normalized] == [None] * 12


def test_verified_kaguya_episodic_movie_is_excluded_by_special_rule():
    """四段流媒体版既不冒充第一季，也不压成一个电影 Asset。

    用户规则（2026-09-24）：特别篇不再入库。已核验 Provider 季度把文件改写成 S00
    时也必须同步刷新 is_importable/is_auxiliary（旧实现只改 group_type，这批文件会
    绕过规则继续进入 Work/Episode/Asset 图），因此这里反向断言它们被排除。
    """

    from app.media_v4.parsing.parser import normalize_batch_parsed_facts

    parser = V4Parser()
    entries = []
    for index, number in enumerate(range(1, 5)):
        evidence = _evidence(
            index,
            "动画/辉夜大小姐想让我告白.S1-S4+剧场版（将更新）/"
            "4.辉夜大小姐想让我告白：初吻不会结束.2022/"
            f"Kaguya-sama wa Kokurasetai - First Kiss wa Owaranai - [{number:02d}].mkv",
        )
        entries.append((evidence, parser.parse(evidence)))

    normalized = normalize_batch_parsed_facts(entries)
    graph = MediaResolver().resolve(normalized)

    # C-004：已核验 Provider 季度不再在解析阶段把本地文件改写成 S00。
    # 没有明确附属关系的发布包保留为可播放的本地内容（本地编号 [01]..[04]），
    # Provider 侧是否算特别篇属于映射层的事实，反向排除会丢用户的正片外传。
    assert all(facts.is_importable is True for _evidence, facts in normalized)
    assert all(facts.is_auxiliary is False for _evidence, facts in normalized)
    assert sorted(
        facts.episode_candidate for _evidence, facts in normalized
    ) == [1, 2, 3, 4]
    # 这批文件现在是本地可播放的 Episode（Provider 侧算不算特别篇由映射层决定），
    # 不再被解析阶段反向排除。
    assert {
        episode.local_episode_number
        for episode in graph.episodes
    } == {1, 2, 3, 4}
    assert all(episode.episode_kind != "special" for episode in graph.episodes)


def test_verified_kaguya_stairway_cross_language_release_maps_to_explicit_fourth_season():
    """无 S04 的英文发布包应与显式 S04 中文目录合并为同一组 Episode Asset。"""

    from app.media_v4.parsing.parser import normalize_batch_parsed_facts
    from app.media_v4.resolution.candidates import merge_graph, plan_work_candidates

    evidence = [
        _evidence(
            0,
            "动画/辉夜大小姐想让我告白.S1-S4+剧场版（将更新）/"
            "5.辉夜大小姐想让我告白：通往大人的阶梯.[S4].2026/"
            "辉夜大小姐想让我告白 通往大人的阶梯.2025.S04E01.mkv",
        ),
        _evidence(
            1,
            "动画/[LoliHouse] Kaguya-sama wa Kokurasetai - Otona e no Kaidan [WebRip]/"
            "[LoliHouse] Kaguya-sama wa Kokurasetai - Otona e no Kaidan - 01.mkv",
        ),
    ]
    parser = V4Parser()
    entries = normalize_batch_parsed_facts([(item, parser.parse(item)) for item in evidence])
    graph = MediaResolver().resolve(entries)

    _candidates, merge_map, issues = plan_work_candidates(graph, entries, lambda *_args: [])
    merged = merge_graph(graph, merge_map)

    # C-004：已核验跨语言身份不再于解析阶段合并本地编号。带 [S4] 目录的条目
    # 保留 season=4，另一份发布包按本地证据保留第 1 集；同一个主系列边界下
    # 两季各自成集，不互相覆盖。
    assert issues == []
    assert len(merged.works) == 1
    assert sorted(
        (episode.local_season_number, episode.local_episode_number)
        for episode in merged.episodes
    ) == [(1, 1), (4, 1)]
    assert {
        evidence_id for episode in merged.episodes for evidence_id in episode.asset_evidence_ids
    } == {item.evidence_id for item in evidence}


def test_verified_cross_language_identity_merges_duplicate_episode_assets():
    """同一 Provider 作品的中英文发布包应是一张卡、一个 Episode、多个 Asset。"""

    from app.media_v4.parsing.parser import normalize_batch_parsed_facts
    from app.media_v4.resolution.candidates import merge_graph, plan_work_candidates

    evidence = [
        _evidence(0, "动画/咒术回战/咒术回战.S01E01.mkv"),
        _evidence(
            1,
            "动画/[BeanSub&FZSD][Jujutsu_Kaisen][BDRip][01-47+MOVIE]/"
            "Jujutsu_Kaisen.S01E01.mkv",
        ),
    ]
    parser = V4Parser()
    entries = normalize_batch_parsed_facts([(item, parser.parse(item)) for item in evidence])
    graph = MediaResolver().resolve(entries)

    _candidates, merge_map, issues = plan_work_candidates(graph, entries, lambda *_args: [])
    merged = merge_graph(graph, merge_map)

    assert issues == []
    assert len(merged.works) == 1
    assert len(merged.episodes) == 1
    assert set(merged.episodes[0].asset_evidence_ids) == {item.evidence_id for item in evidence}
