"""作品边界身份规则（季标记 / 年份 / 空格）真实路径回归。

用户反馈（2026-09-24）：真实媒体库里"很多作品在不同季度被分成了不同的作品，
而且很多同一个名字的作品重复出现了好几遍"。对 confirmed revision
`rev-9e6b51f0-c52c-44c8-836d-dd8ca005c93c`（root `root_c6827e6590d9537b0bc03318`，
177 个 active work、3472 条 evidence）做只读重建，定位到四个机械成因：

- **R1** 季目录解析出的年份是本季播出年，不得进入身份键；
- **R2** 季标记不得留在身份标题（首部 `S3 ` / `第2季 `，尾部 `第二季` / `S2` / `Ⅱ`）；
- **R3** 缺年份补齐的作用域放宽到 ``root + 边界标题 + 媒体类型``；
- **R4** 边界标题忽略中日韩文本内部空格（`天元突破 红莲螺岩` == `天元突破红莲螺岩`）。

本文件用**真实相对路径**（只读提取，无凭据、无绝对根路径）锁定上述四条规则，
并守住两条反例：季标记未落到本地季号时不得合并、两侧都有年份且不同时不得合并。
"""

from __future__ import annotations

import pytest

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.resolution import resolver as R
from app.media_v4.resolution.resolver import MediaResolver

ROOT = "root-boundary-identity"
SCAN = "scan-boundary-identity"
VERSION = V4Parser.VERSION

#: 真实路径 → 作品边界身份键。左列取自 `rev-9e6b51f0` 的实际 evidence。
REAL_BOUNDARY_CASES = [
    # M1 + R2：季标记留在容器名里（`暗杀教室第一季` / `第二季`）
    (
        "A 4k 暗杀教室/暗杀教室第一季/[TUDO] Ansatsu Kyoushitsu [23][Ma10p_2160p][x265_flac_ass].mkv",
        "title:暗杀教室::tv",
    ),
    (
        "A 4k 暗杀教室/暗杀教室第二季/[TUDO] Ansatsu Kyoushitsu S2 [22][Ma10p_2160p][x265_flac_ass].mkv",
        "title:暗杀教室::tv",
    ),
    # M1 + R2：尾部中文季标记
    (
        "S 4k 赛马娘/赛马娘 第二季/[Ygm] Uma Musume Pretty Derby S2 [02][Ma10p_2160p][x265_DTS5.1_ass].mkv",
        "title:赛马娘::tv",
    ),
    (
        "S 4k 赛马娘/赛马娘 第三季/[Ygm] Uma Musume Pretty Derby S3 [10][Ma10p_2160p][x265_DTS5.1_ass].mkv",
        "title:赛马娘::tv",
    ),
    # M1 + R2：首部 `S1/S2/S3 ` 标记 + 罗马数字季号（`Ⅱ` / `Ⅲ`）
    (
        "灵能百分百/S1 灵能百分百 路人超能100（2016）/灵能百分百 路人超能100 [S01E01][Ma10p_2160p][x265_flac_ass].mkv",
        "title:灵能百分百路人超能100::tv",
    ),
    (
        "灵能百分百/S2 灵能百分百 路人超能100 Ⅱ（2019）/灵能百分百 路人超能100 Ⅱ [S02E07][Ma10p_2160p][x265_flac_ass].mkv",
        "title:灵能百分百路人超能100::tv",
    ),
    (
        "灵能百分百/S3 灵能百分百 路人超能100 Ⅲ（2022）/灵能百分百 路人超能100 Ⅲ [S03E09][Ma10p_2160p][x265_flac_ass].mkv",
        "title:灵能百分百路人超能100::tv",
    ),
    # M2 + R1：季标记在容器**下面**的季目录上，容器名解析不出年份，
    # 年份来自 `S3 …（2022）`；各季播出年不能进身份键。
    (
        "辉夜大小姐想让我告白/辉夜大小姐想让我告白/S3 辉夜大小姐想让我告白-超级浪漫-（2022）"
        "/辉夜大小姐想让我告白-超级浪漫- [S03E03][Ma10p_2160p][x265_flac_ass].mkv",
        "title:辉夜大小姐想让我告白::tv",
    ),
    (
        "辉夜大小姐想让我告白/辉夜大小姐想让我告白/S1 辉夜大小姐想让我告白～天才们的恋爱头脑战～（2019）"
        "/辉夜大小姐想让我告白～天才们的恋爱头脑战～ [S01E07][Ma10p_2160p][x265_flac_ass].mkv",
        "title:辉夜大小姐想让我告白::tv",
    ),
    (
        "辉夜大小姐想让我告白/辉夜大小姐想让我告白/S2 辉夜大小姐想让我告白？～天才们的恋爱头脑战～（2020）"
        "/辉夜大小姐想让我告白？～天才们的恋爱头脑战～ [S02E12][Ma10p_2160p][x265_flac_ass].mkv",
        "title:辉夜大小姐想让我告白::tv",
    ),
    # M4 + R4：空格差异（`天元突破 红莲螺岩` / `天元突破红莲螺岩`）与 2007 年
    (
        "T 4k 天元突破 红莲螺岩/[TUDO&Ygm] Tengen Toppa Gurren Lagann [27][Ma10p_2160p][x265_flac_ass].mkv",
        "title:天元突破红莲螺岩::tv",
    ),
    (
        "天元突破红莲螺岩.2007/[TUDO&MAI] Tengen Toppa Gurren Lagann [17][Ma10p_2160p][x265_flac_ass].mkv",
        "title:天元突破红莲螺岩:2007:tv",
    ),
    # M3 + R1：一侧无年份、一侧年份来自 `S1 …（2011）` 季目录
    (
        "命运石之门 [KissSub&MAI] [Ma10p_2160p]/1.命运石之门 [KissSub&MAI] [Ma10p_2160p]"
        "/[MAI] Steins;Gate [16][Ma10p_2160p][x265_flac_ass].mkv",
        "title:命运石之门::tv",
    ),
    (
        "命运石之门/命运石之门/S1 命运石之门（2011）/命运石之门 [S01E02][Ma10p_2160p][x265_flac_ass].mkv",
        "title:命运石之门::tv",
    ),
]

#: C-003：目录里的明确季标记（`第二季`/`Season 2`/`[S2]`）会补到本地季号上，
#: 因此标记一致的条目正常收口到主系列键；只有季号无法确定时才保留原键。
SEASON_MARKER_WITHOUT_LOCAL_SEASON = [
    (
        "P 4k 排球少年/排球少年第一季/[Ygm] Haikyuu!! [13][Ma10p_2160p][x265_flac_ass].mkv",
        "title:排球少年::tv",
        1,
    ),
    (
        "P 4k 排球少年/排球少年第二季/[Ygm] Haikyuu!! 2nd Season [20][Ma10p_2160p][x265_flac_ass].mkv",
        "title:排球少年::tv",
        2,
    ),
]


def _evidence(index: int, relative_path: str, *, provider: str = "quark", root_id: str = ROOT) -> SourceEvidence:
    return SourceEvidence(
        evidence_id=f"ev-boundary-{index:03d}",
        scan_id=SCAN,
        root_id=root_id,
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider=provider,
        ingest_method="openlist_scan",
    )


def _parse(relative_paths: list[str], *, provider: str = "quark", root_id: str = ROOT):
    parser = V4Parser()
    entries = []
    for index, relative_path in enumerate(relative_paths):
        evidence = _evidence(index, relative_path, provider=provider, root_id=root_id)
        entries.append((evidence, parser.parse(evidence)))
    return entries


def _facts(evidence: SourceEvidence, **overrides) -> ParsedFacts:
    """最小 ParsedFacts（用于 R3/R4 的边界反例，不经过识别器）。"""

    fields = {
        "parsed_fact_id": f"facts-{evidence.evidence_id}",
        "evidence_id": evidence.evidence_id,
        "parser_version": VERSION,
        "media_type": "tv",
        "group_type": "season",
        "work_title": "Show",
        "title_candidates": ("Show",),
        "season_candidate": 1,
        "episode_candidate": 1,
    }
    fields.update(overrides)
    return ParsedFacts(**fields)


@pytest.mark.parametrize("relative_path, expected_key", REAL_BOUNDARY_CASES)
def test_real_paths_resolve_to_expected_boundary_keys(relative_path, expected_key):
    """真实目录路径的作品边界身份键（R1/R2/R4）。

    左列覆盖报告里提到的 10 条真实路径（暗杀教室第一/二季、赛马娘 第二/三季、
    辉夜 S1/S2/S3、天元突破 两份目录、命运石之门 两份目录），并补齐
    灵能百分百 S1/S2/S3（罗马数字季号）。
    """

    entries = _parse([relative_path])
    evidence, facts = entries[0]

    assert R._work_key(facts, evidence) == expected_key


@pytest.mark.parametrize("relative_path, expected_key, expected_season", SEASON_MARKER_WITHOUT_LOCAL_SEASON)
def test_season_marker_without_local_season_number_keeps_original_key(
    relative_path, expected_key, expected_season
):
    """季标记未落到本地季号时不得合并（否则同季集号相撞、集数丢失）。"""

    entries = _parse([relative_path])
    evidence, facts = entries[0]

    assert facts.season_candidate == expected_season
    assert R._work_key(facts, evidence) == expected_key


@pytest.mark.parametrize(
    "relative_paths, expected_key, expected_seasons, unrelated_paths",
    [
        (
            [
                "A 4k 暗杀教室/暗杀教室第一季/"
                "[TUDO] Ansatsu Kyoushitsu [23][Ma10p_2160p][x265_flac_ass].mkv",
                "A 4k 暗杀教室/暗杀教室第一季/"
                "[TUDO] Ansatsu Kyoushitsu [01][Ma10p_2160p][x265_flac_ass].mkv",
                "A 4k 暗杀教室/暗杀教室第二季/"
                "[TUDO] Ansatsu Kyoushitsu S2 [22][Ma10p_2160p][x265_flac_ass].mkv",
            ],
            "title:暗杀教室::tv",
            {1, 2},
            [],
        ),
        (
            [
                "S 4k 赛马娘/赛马娘 第一季/[Ygm] Uma Musume Pretty Derby [08][Ma10p_2160p][x265_flac_ass].mkv",
                "S 4k 赛马娘/赛马娘 第二季/"
                "[Ygm] Uma Musume Pretty Derby S2 [02][Ma10p_2160p][x265_DTS5.1_ass].mkv",
                "S 4k 赛马娘/赛马娘 第三季/"
                "[Ygm] Uma Musume Pretty Derby S3 [10][Ma10p_2160p][x265_DTS5.1_ass].mkv",
            ],
            "title:赛马娘::tv",
            {1, 2, 3},
            [],
        ),
        (
            [
                "灵能百分百/S1 灵能百分百 路人超能100（2016）/"
                "灵能百分百 路人超能100 [S01E01][Ma10p_2160p][x265_flac_ass].mkv",
                "灵能百分百/S2 灵能百分百 路人超能100 Ⅱ（2019）/"
                "灵能百分百 路人超能100 Ⅱ [S02E07][Ma10p_2160p][x265_flac_ass].mkv",
                "灵能百分百/S3 灵能百分百 路人超能100 Ⅲ（2022）/"
                "灵能百分百 路人超能100 Ⅲ [S03E09][Ma10p_2160p][x265_flac_ass].mkv",
            ],
            "title:灵能百分百路人超能100::tv",
            {1, 2, 3},
            [],
        ),
        # `物语系列` 是明确命名的合集（子作品基名不只一个），系列归并对它不生效：
        # 季标记剥离后 `终物语` 与 `终物语第二季` 仍是一个 Work，而 `化物语` 另成一部。
        (
            [
                "W 4k 物语系列/11.终物语/[Ygm] Owarimonogatari [11][Ma10p_2160p][x265_2flac_2ass].mkv",
                "W 4k 物语系列/14.终物语第二季/"
                "[Ygm] Owarimonogatari S2 [01][Ma10p_2160p][x265_2flac_2ass].mkv",
            ],
            "title:终物语::tv",
            {1, 2},
            [
                "W 4k 物语系列/1.化物语/[Ygm] Bakemonogatari [09][Ma10p_2160p][x265_2flac_2ass].mkv",
            ],
        ),
        (
            [
                "辉夜大小姐想让我告白/辉夜大小姐想让我告白/S1 辉夜大小姐想让我告白～天才们的恋爱头脑战～（2019）"
                "/辉夜大小姐想让我告白～天才们的恋爱头脑战～ [S01E07][Ma10p_2160p][x265_flac_ass].mkv",
                "辉夜大小姐想让我告白/辉夜大小姐想让我告白/S2 辉夜大小姐想让我告白？～天才们的恋爱头脑战～（2020）"
                "/辉夜大小姐想让我告白？～天才们的恋爱头脑战～ [S02E12][Ma10p_2160p][x265_flac_ass].mkv",
                "辉夜大小姐想让我告白/辉夜大小姐想让我告白/S3 辉夜大小姐想让我告白-超级浪漫-（2022）"
                "/辉夜大小姐想让我告白-超级浪漫- [S03E03][Ma10p_2160p][x265_flac_ass].mkv",
            ],
            "title:辉夜大小姐想让我告白::tv",
            {1, 2, 3},
            [],
        ),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_season_directories_merge_into_one_work_with_distinct_seasons(
    relative_paths, expected_key, expected_seasons, unrelated_paths, reverse
):
    """合并后仍是一个 Work、季号仍是 1/2/3（不得塌缩成单季），且与输入顺序无关。"""

    all_paths = relative_paths + unrelated_paths
    entries = _parse(all_paths)
    graph = MediaResolver().resolve(entries[::-1] if reverse else entries)

    season_evidence_ids = {
        evidence.evidence_id
        for index, (evidence, _facts_item) in enumerate(entries)
        if index < len(relative_paths)
    }
    merged = [
        work
        for work in graph.works
        if set(work.source_evidence_ids) & season_evidence_ids
    ]
    assert [work.work_key for work in merged] == [expected_key]
    assert set(merged[0].source_evidence_ids) == season_evidence_ids
    assert {
        episode.local_season_number
        for episode in graph.episodes
        if episode.work_key == expected_key
    } == expected_seasons
    if unrelated_paths:
        unrelated_evidence_ids = {
            evidence.evidence_id for evidence, _facts_item in entries[len(relative_paths):]
        }
        assert not set(merged[0].source_evidence_ids) & unrelated_evidence_ids


def test_two_releases_of_one_work_merge_into_versions_not_duplicate_work():
    """同一部作品的两份物理目录（`T 4k …` 与 `…2007`）合并为一个 Work。

    两份目录都是同一部作品的两套发布：每集成为同一 Episode 的多版本 Asset，
    因此 Work 数从 2 降到 1、Episode 数不变（这里 3 集各 2 个 Asset）。
    合并由 R4（忽略中日韩内部空格）+ R3（缺年份并入唯一已知年份）完成；
    在 `rev-9e6b51f0` 全量重建里，该目录的 `SPs` 条目带 `relation_type=main`
    的主系列事实，同一份作品也会通过 `series:天元突破红莲螺岩:tv` 收口为同一个 Work。
    """

    first = [
        f"T 4k 天元突破 红莲螺岩/[TUDO&Ygm] Tengen Toppa Gurren Lagann [{episode:02d}]"
        f"[Ma10p_2160p][x265_flac_ass].mkv"
        for episode in range(1, 4)
    ]
    second = [
        f"天元突破红莲螺岩.2007/[TUDO&MAI] Tengen Toppa Gurren Lagann [{episode:02d}]"
        f"[Ma10p_2160p][x265_flac_ass].mkv"
        for episode in range(1, 4)
    ]
    entries = _parse(first + second)
    graph = MediaResolver().resolve(entries)

    assert len(graph.works) == 1
    work = graph.works[0]
    assert work.work_key == "title:天元突破红莲螺岩:2007:tv"
    assert work.year == 2007
    assert len(work.source_evidence_ids) == 6
    assert len(graph.episodes) == 3
    assert {(episode.local_season_number, episode.local_episode_number) for episode in graph.episodes} == {
        (1, 1),
        (1, 2),
        (1, 3),
    }
    assert all(len(episode.asset_evidence_ids) == 2 for episode in graph.episodes)


def test_plain_structural_season_directory_does_not_hide_year():
    """`X/Season 1/…` 里的纯结构季目录不算季标记。

    容器名自带年份的两部不同作品（`AIR.2005` 与 `AIR.2015`）各自有 `Season 1`
    子目录；若把纯结构季目录当成季标记，年份会被抹掉、两个身份键会撞在一起。
    这里直接断言边界键本身，避开系列归并（`series:air:tv`）的既有路径。
    """

    keys = set()
    for index, root in enumerate(("AIR.2005", "AIR.2015")):
        evidence = _evidence(index, f"{root}/Season 1/AIR [{index + 1:02d}].mkv")
        facts = _facts(
            evidence,
            work_title="AIR",
            group_type="season",
            season_candidate=1,
            episode_candidate=index + 1,
        )
        keys.add(R._boundary_work_key(evidence, facts))

    assert keys == {"title:air:2005:tv", "title:air:2015:tv"}


def test_standalone_movie_keeps_separate_boundary():
    """独立 OVA/电影（`card_type=standalone`）不继承主系列边界，仍是独立作品。"""

    entries = _parse(
        [
            "X 4k 小魔女学园/小魔女学园/[Ygm] Little Witch Academia [10][Ma10p_2160p][x265_flac_ass].mkv",
            "X 4k 小魔女学园/OVA 小魔女学园 2013/"
            "[Ygm] Little Witch Academia 2013 [Ma10p_2160p][x265_flac_ass].mkv",
        ]
    )
    graph = MediaResolver().resolve(entries)

    assert {work.work_key for work in graph.works} == {
        "title:小魔女学园::tv",
        "title:小魔女学园:2013:movie",
    }


# ---------------------------------------------------------------------------
# R3：缺年份补齐的作用域 = root + 边界标题 + 媒体类型
# ---------------------------------------------------------------------------


def _yearless_scope_entries(*, dated_root: str = ROOT, yearless_root: str = ROOT):
    dated_evidence = _evidence(1, "Show 2024/Season 1/main.mkv", root_id=dated_root)
    yearless_evidence = _evidence(2, "Elsewhere/Show/Season 1/bonus.mkv", root_id=yearless_root)
    return [
        (dated_evidence, _facts(dated_evidence, work_title="Show", year_candidate=2024)),
        (yearless_evidence, _facts(yearless_evidence, work_title="Show", year_candidate=None)),
    ]


def test_missing_year_borrows_unique_year_across_directories_in_same_root():
    """同一 root/标题/媒体类型下只有唯一已知年份时，缺年份键并入该年份（R3）。"""

    graph = MediaResolver().resolve(_yearless_scope_entries())

    assert [work.work_key for work in graph.works] == ["title:show:2024:tv"]
    assert graph.works[0].year == 2024
    assert len(graph.works[0].source_evidence_ids) == 2


def test_missing_year_does_not_borrow_when_two_years_disagree():
    """两侧都带年份且不同（同一标题的 1999 版与 2011 版）必须保持两条。"""

    entries = _yearless_scope_entries()
    remake_evidence = _evidence(3, "Show 1999/Season 1/remake.mkv")
    entries.append(
        (remake_evidence, _facts(remake_evidence, work_title="Show", year_candidate=1999))
    )

    graph = MediaResolver().resolve(entries)

    assert {work.work_key for work in graph.works} == {
        "title:show:2024:tv",
        "title:show:1999:tv",
        "title:show::tv",
    }


def test_missing_year_does_not_cross_root_or_media_type():
    """跨 root 与跨媒体类型都不能借用年份。"""

    other_root = MediaResolver().resolve(_yearless_scope_entries(yearless_root="root-other"))
    assert {work.work_key for work in other_root.works} == {
        "title:show:2024:tv",
        "title:show::tv",
    }

    entries = _yearless_scope_entries()
    movie_evidence = _evidence(4, "Elsewhere/Show/Season 1/movie.mkv")
    entries.append(
        (
            movie_evidence,
            _facts(movie_evidence, work_title="Show", year_candidate=None, media_type="movie"),
        )
    )
    graph = MediaResolver().resolve(entries)

    assert {work.work_key for work in graph.works} == {
        "title:show:2024:tv",
        "title:show::movie",
    }


# ---------------------------------------------------------------------------
# R4：边界标题忽略中日韩文本内部空格
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "directory_name, expected_index, expected_base",
    [
        # 尾部罗马数字季号（不带 `S3 ` 前缀）；`strip_season_marker` 不处理它。
        ("命定之诗 Ⅲ", 3, "命定之诗"),
        ("玄界之门 II", 2, "玄界之门"),
        ("某作品 S2", 2, "某作品"),
        ("某作品 2nd Season", 2, "某作品"),
        ("某作品 Season 2", 2, "某作品"),
        ("S3 某作品", 3, "某作品"),
        ("第2季 某作品", 2, "某作品"),
        # 反例：真标题不能被当成季标记（`S1m0ne`、`Project X`、`SK8`）。
        ("S1m0ne", None, "S1m0ne"),
        ("Project X", None, "Project X"),
        ("SK8", None, "SK8"),
        # 反例：纯结构季目录没有可剥离的作品基名，只做单向收缩。
        ("Season 1", 1, "Season 1"),
        ("第一季", 1, "第一季"),
    ],
)
def test_season_marker_detection_and_stripping(directory_name, expected_index, expected_base):
    """季标记识别（含罗马数字）与基名剥离的单向收缩边界。

    只有**识别为季标记**的目录名才会被剥离；`S1m0ne` / `Project X` / `SK8` 这类真标题
    必须返回 None，否则会在边界层把 `S1m0ne` 当成季目录并抹掉年份。
    """

    assert R._has_boundary_season_marker(directory_name) is (expected_index is not None)
    assert R._season_index_from_name(directory_name) == expected_index
    if expected_index is not None:
        assert R._strip_boundary_season_marker(directory_name) == expected_base


def test_fallback_work_key_strips_season_marker_without_container():
    """无容器可用时，事实标题自带的季标记同样按基名收口（R2 兜底分支）。

    否则同一个季目录里容器可用与不可用的条目会算成两个作品。
    """

    evidence = _evidence(9, "Season 1/episode.mkv")
    with_marker = _facts(
        evidence, work_title="暗杀教室第二季", year_candidate=2020, title_candidates=("暗杀教室第二季",)
    )
    without_marker = _facts(
        evidence, work_title="暗杀教室", year_candidate=2020, title_candidates=("暗杀教室",)
    )

    # 无 evidence 参数 → 跳过容器分支，走 `_work_key` 的本地事实兜底。
    assert R._work_key(with_marker) == "title:暗杀教室::tv"
    assert R._work_key(without_marker) == "title:暗杀教室:2020:tv"


def test_boundary_title_ignores_cjk_inner_spaces_only():
    """R4 只收敛中日韩文字之间的内部空格，拉丁词之间的空格必须保留。"""

    assert R._boundary_key_title("天元突破 红莲螺岩") == R._boundary_key_title("天元突破红莲螺岩")
    assert R._boundary_key_title("赛马娘 巅峰之路") == "赛马娘巅峰之路"
    # 共享身份语义未被放宽：`normalize_identity_title` 仍保留空格。
    from app.media_v4.resolution.title_norm import normalize_identity_title

    assert normalize_identity_title("天元突破 红莲螺岩") == "天元突破 红莲螺岩"
    # 拉丁标题的空格属于标题本身。
    assert R._boundary_key_title("Love Live！第一季") == normalize_identity_title("Love Live！第一季")
    # 拉丁词与汉日文之间的空格同样保留（`Love Live! 虹咲…` 是两个词，不是一个词）。
    assert R._boundary_key_title("Love Live! 虹咲学园偶像同好会 第二季") == (
        "love live! 虹咲学园偶像同好会第二季"
    )
