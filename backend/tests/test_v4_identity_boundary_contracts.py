"""第 0 步契约：锁定"作品边界"的正确行为，并复现当前的错误路径。

依据：docs/OpenList导入识别与详情页修复施工规格-2026-09-21.md §5 第 0 步。

本文件**只加测试，不改生产代码**。其中标注「RED」的用例在修复前**故意失败**，
用来证明错误路径确实存在；标注「GREEN」的用例锁定现在就已经正确的行为，
修复过程中不得被破坏。

不变量（来自规格 §2）：
- 错误猜测的最大代价是多一条记录；不得因系列名/共享 Provider ID/相似标题合并不同作品；
- 本地身份与在线资料分层：在线 ID 只是资料引用，不能成为作品唯一键。
"""

from __future__ import annotations

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.resolution.resolver import MediaResolver
from app.media_v4.resolution.title_norm import normalize_identity_title, normalize_match_title

# --------------------------------------------------------------------------
# 夹具：真实目录形态（脱敏，仅相对路径与文件名）
# --------------------------------------------------------------------------

# 同一系列下**名字不同**的条目（物语系列）：必须各自成 Work（规格 §4.1 / §11.7）
# 使用**真实形态**：扫描根是网盘挂载名（`/夸克网盘`），作品目录在挂载名之下。
# 实测（探针）：修复前 parser 会把挂载名当成 work_title/series_group，整根塌缩成
# 1 个 Work（key 全为 `title:夸克网盘:tv`）；修复后各自成 Work。
MOUNT_ROOT = "/夸克网盘"
MONOGATARI_PATHS = [
    "夸克网盘/动画/物语系列/化物语/[Group] Bakemonogatari [01][Ma10p_2160p][x265_flac].mkv",
    "夸克网盘/动画/物语系列/化物语/[Group] Bakemonogatari [02][Ma10p_2160p][x265_flac].mkv",
    "夸克网盘/动画/物语系列/终物语/[Group] Owarimonogatari [01][Ma10p_2160p][x265_flac].mkv",
    "夸克网盘/动画/物语系列/续终物语/[Group] Zoku Owarimonogatari [01][Ma10p_2160p][x265_flac].mkv",
]

# 同名作品的多季：必须仍是同一个 Work（GREEN，锁定现状）
# 注意：必须使用**真实发布目录层级**（作品目录/发布目录/文件），与
# test_v4_work_identity_stability.py 的真实形态一致；只用一层扁平路径
# 无法让解析器形成结构化的同系列证据。
SAME_NAME_SEASONS_PATHS = [
    "[Group] Yuru Camp/[Group] Yuru Camp [Ma10p_1080p]/[Group] Yuru Camp [01][Ma10p_1080p][x265_flac].mkv",
    "[Group] Yuru Camp/[Group] Yuru Camp Season 2 [Ma10p_1080p]/[Group] Yuru Camp Season 2 [01][Ma10p_1080p][x265_flac].mkv",
]

# 主篇 + 剧场版：必须各自成 Work（standalone 隔离，GREEN）
MAIN_AND_MOVIE_PATHS = [
    "动画/某作品/[Group] Show [01][Ma10p_2160p][x265_flac].mkv",
    "动画/某作品 剧场版/[Group] Show Movie [Ma10p_2160p][x265_flac].mkv",
]


def _evidence(index: int, relative_path: str) -> SourceEvidence:
    return SourceEvidence(
        evidence_id=f"ev-contract-{index:03d}",
        scan_id="scan-contract",
        root_id="root-contract",
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider="local",
        ingest_method="local_scan",
        source_locator=f"K:\\媒体库\\{relative_path}",
        playback_locator=f"K:\\媒体库\\{relative_path}",
    )


def _resolve_paths(paths: list[str], container: str):
    parser = V4Parser()
    evidence = [_evidence(index, path) for index, path in enumerate(paths)]
    parsed = normalize_batch_parsed_facts(
        [(item, parser.parse(item, root_container=container)) for item in evidence]
    )
    return MediaResolver().resolve(parsed)


def _facts(*, work_title: str, series_group: str, tmdb_hint_id: int | None = None) -> ParsedFacts:
    return ParsedFacts(
        parsed_fact_id=f"facts-{work_title}",
        evidence_id=f"ev-{work_title}",
        parser_version="contract",
        work_title=work_title,
        title_candidates=(work_title,),
        media_type="tv",
        group_type="season",
        series_group=series_group,
        confidence="high",
        needs_review=False,
        tmdb_hint_id=tmdb_hint_id,
        tmdb_hint_type="tv" if tmdb_hint_id else None,
    )


# --------------------------------------------------------------------------
# RED：当前会失败，用来证明错误路径存在
# --------------------------------------------------------------------------


def test_differently_named_series_entries_are_separate_works():
    """RED：同一系列下名字不同的条目（化物语/终物语/续终物语）必须各自成 Work。

    当前 resolver 在 `series_group != work_title` 时直接返回系列名，
    使三者共用 `series:物语系列:tv`，形成"身份塌缩"（规格 §4.1）。
    """

    graph = _resolve_paths(MONOGATARI_PATHS, MOUNT_ROOT)
    keys = {work.work_key for work in graph.works}
    titles = {work.preferred_title for work in graph.works}

    assert len(graph.works) == 3, f"应各自成 Work，实际 {len(graph.works)} 个：{titles}"
    assert len(keys) == 3, f"三者的工作键必须互不相同：{keys}"


def test_differently_named_works_sharing_online_id_are_not_mergeable():
    """RED→GREEN：名字不同、但共享同一在线身份的两个作品不得被合并。

    规格 §5 第 0 步契约 3 / §4.1：Provider ID 只是资料引用，不能成为作品唯一键；
    "最终 merge 门"（`identity_policy.can_merge_provider_identity`）必须要求双方
    标题互为变体，否则《化物语》与《终物语》会因为同属一个系列目录而被并成一个 Work。
    """

    from app.media_v4.resolution.identity_policy import can_merge_provider_identity

    graph = _resolve_paths(MONOGATARI_PATHS, MOUNT_ROOT)
    keys = {work.work_key for work in graph.works}

    assert len(keys) >= 2, "名字不同的条目必须得到不同的 Work 键"
    assert can_merge_provider_identity(graph, keys) is False, (
        "两个名字不同的作品不得通过 merge 门（否则会共用一条在线身份）"
    )


# --------------------------------------------------------------------------
# GREEN：现在就该通过，用来锁定正确行为（修复中不得破坏）
# --------------------------------------------------------------------------


def test_same_name_seasons_stay_one_work():
    """GREEN：同名作品的第 1/2 季仍是同一个 Work。"""

    graph = _resolve_paths(SAME_NAME_SEASONS_PATHS, "Yuru Camp")
    assert len(graph.works) == 1, "同名多季不应被拆成多个 Work"


def test_main_series_and_movie_are_separate_works():
    """GREEN：主篇与剧场版各自成 Work（standalone 隔离）。"""

    graph = _resolve_paths(MAIN_AND_MOVIE_PATHS, "某作品")
    assert len(graph.works) >= 2, "主篇与剧场版不得合并成一个 Work"


def test_identity_normalization_keeps_internal_punctuation():
    """GREEN：身份归一化必须保留内部标点（`K-ON!` ≠ `K-ON!!`）。

    这条直接锁定规格 §3.2.1：真正宽松的是 `normalize_match_title`，
    身份层不得跟着放宽，否则季/续作会被合并。
    """

    assert normalize_identity_title("K-ON!") != normalize_identity_title("K-ON!!")
    assert normalize_identity_title("Chuunibyou demo Koi ga Shitai!") != normalize_identity_title(
        "Chuunibyou demo Koi ga Shitai! Ren"
    )


def test_match_normalization_still_equates_harmless_variants():
    """GREEN：匹配用的宽松归一化仍应等价处理无害差异（全半角/空格/书名号）。"""

    assert normalize_match_title("Ｙｕｒｕ Ｃａｍｐ") == normalize_match_title("Yuru Camp")
    assert normalize_match_title("《Yuru Camp》") == normalize_match_title("YuruCamp")
    # 宽松是**匹配**语义：它可以把 K-ON! 与 K-ON!! 视为同一检索词，
    # 但这不得回写成身份（见上一条）。
    assert normalize_match_title("K-ON!") == normalize_match_title("K-ON!!")


def test_missing_identity_still_produces_local_work():
    """GREEN：没有身份线索时仍必须生成本地 Work（阶段 1 的既有契约）。"""

    facts = ParsedFacts(
        parsed_fact_id="facts-none",
        evidence_id="ev-none",
        parser_version="contract",
        work_title="",
        title_candidates=(),
        media_type="tv",
        group_type="special",
        series_group="",
        confidence="low",
        needs_review=True,
    )
    graph = MediaResolver().resolve([(_evidence(0, "动画/未知/[Group] SP01.mkv"), facts)])

    # 有目录名可用时用目录名做本地身份；完全没有可用标题时才退化为 local:file:。
    assert len(graph.works) == 1
    assert graph.works[0].work_key.startswith(("local:", "title:"))
