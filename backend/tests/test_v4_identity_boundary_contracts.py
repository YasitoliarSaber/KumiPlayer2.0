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


def test_differently_named_works_are_separate_works():
    """真实目录解析后的独立条目，即使共享在线系列资料也不能重新合并。

    合并门槛的历史教训（不要重复走）：

    - “标题互为前缀”（在 can_merge_provider_identity 里加）与“要求双方年份齐全”
      都已被实测证伪，会破坏合法合并，不要重新引入。
    - 逐对要求标题证据是可行方向，但必须同时接入显式译名证据：单条候选自己
      覆盖两个本地名，或已核验译名表
      （recognition/verified_titles.titles_share_verified_alias）。
      只按“共享在线 ID”拒绝合并会打掉跨语言同一作品的既有契约
      （test_v4_p001_rework.py::test_pre_confirm_candidate_resolution_merges_cross_language_titles）。
    - 反向边界同样锁死：“共享 ID + 双方各自命中自身标题”不得当成译名证据
      （test_v4_provider_merge_boundaries.py::test_matching_each_local_name_to_shared_id_is_not_translation_evidence）。
    """

    from app.media_v4.resolution.candidates import WorkCandidate, merge_graph, merge_map_from_candidates

    graph = _resolve_paths(MONOGATARI_PATHS, MOUNT_ROOT)
    keys = {work.work_key for work in graph.works}

    assert len(keys) >= 2, "名字不同的条目必须得到不同的 Work 键"
    assert len(graph.works) == len(keys), "每个 Work 键对应一个独立作品卡"
    candidates = {work.work_key: [WorkCandidate(
        work.work_key, "tmdb", "46195", "tv", "物语系列", None, "search", "high",
        status="confirmed",
    )] for work in graph.works}
    merged = merge_graph(graph, merge_map_from_candidates(graph, candidates))
    assert {work.work_key for work in merged.works} == keys
    assert merged.episodes == graph.episodes


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


def test_punctuation_only_difference_is_identity_equal_by_project_contract():
    """⚠️ 规格 §4.2 与项目既有契约**冲突**（已实测；勿按规格直接收紧）。

    规格第 2 步要求"检索命中不得升级为身份等值"，即 `K-ON!` 与 `K-ON!!` 在身份层应不同。
    但项目现有 4 条契约明确要求**相反**行为（标点/符号差异必须视为同一标题）：
      - `test_v4_title_normalization_unity.py::test_ranker_and_draft_scorer_agree_on_a_punctuation_only_difference`
      - `test_v4_candidate_ranker.py::test_cjk_punctuation_difference_still_counts_as_the_same_title`
      - `test_scrape_alias_matching.py::test_every_language_variant_of_the_same_work_is_adoptable`
      - `test_scrape_alias_matching.py::test_punctuation_and_symbols_do_not_break_equality`

    实测：把 `ranker._title_identity_level` 的身份等值改为严格归一化后，上述 4 条
    全部失败。因此第 2 步的这项收紧**未实施**，需规格作者先裁定这 4 条契约是否应改。
    本用例锁定当前行为，并留下冲突记录。
    """

    from app.media_v4.resolution.ranker import _title_identity_level

    assert _title_identity_level(["K-ON!!"], {"title": "K-ON!"})[0] >= 2, (
        "当前契约：标点差异视为同一标题"
    )
    assert _title_identity_level(["Yuru Camp"], {"title": "《Ｙｕｒｕ Ｃａｍｐ》"})[0] >= 2, (
        "无害差异（全半角/空格/书名号）必须等值"
    )


def test_ordinary_episode_title_has_no_bracket_debris():
    """第 4 步：普通剧集标题不得是方括号残片（实测为 `]`）或纯发布参数。

    证据（探针 `.context/openlist-debug/episode_title_probe.py`）：
    `冰海战记 第1季 [S01E01][Ma10p_2160p][x265_flac_ass].mkv`、
    `辉夜大小姐想让我告白-超级浪漫- [S03E01][Ma10p_2160p][x265_flac_ass].mkv`、
    `灵能百分百 路人超能100 [S01E01][Ma10p_2160p][x265_flac_ass].mkv`
    三者的 `episode_title` 都是 `]`，在媒体库里直接显示为 `]`。
    注意：与作品名相同**不是**无效标题（规格 §4.3），只清理纯符号与纯发布参数。
    """

    parser = V4Parser()
    cases = {
        "冰海战记 第1季 [S01E01][Ma10p_2160p][x265_flac_ass].mkv": (1, 1),
        "辉夜大小姐想让我告白-超级浪漫- [S03E01][Ma10p_2160p][x265_flac_ass].mkv": (3, 1),
        "[TUDO&Ygm] Vindland Saga [06][Ma10p_2160p][x265_flac_ass].mkv": (1, 6),
    }
    for index, (name, (season, episode)) in enumerate(cases.items()):
        rel = f"夸克网盘/动画/某作品/{name}"
        facts = parser.parse(_evidence(index, rel), root_container="/夸克网盘")
        assert facts.season_candidate == season, f"{name} 的季号解析错误"
        assert facts.episode_candidate == episode, f"{name} 的集号解析错误"
        assert (facts.episode_title or "").strip() == "", (
            f"{name} 的集标题应是空（由界面回退为未命名），实际为 {facts.episode_title!r}"
        )


def test_library_card_title_keeps_local_identity_over_online_title():
    """第 3 步：在线标题只作**兜底**，不得覆盖本地作品身份；手动覆盖仍最高优先。

    实测（`api/library_v4.py:79`）：`override → metadata.title → card.title`，
    于是多个名字不同的本地作品被刮削到同一条系列条目（物语系列）时，卡片标题
    全部变成在线系列名，媒体库出现多张同名同图的卡片。
    """

    from app.api.library_v4 import _card_payload

    card = {
        "work_id": "w1",
        "title": "化物语",
        "media_type": "tv",
        "year": 2009,
        "episode_count": 15,
        "asset_count": 15,
        "card_type": "main_series",
        "show_type": "anime_series",
        "metadata": {"title": "物语系列", "provider": "tmdb", "original_title": "化物語"},
    }
    assert _card_payload(card)["title"] == "化物语", "本地标题必须优先于在线标题"
    assert _card_payload(card, {"title": "我的标题"})["title"] == "我的标题", "手动覆盖优先级最高"
    assert _card_payload(dict(card, title=""))["title"] == "物语系列", "本地无标题时才用在线标题"


def test_cjk_episode_numbering_is_recognized():
    """中文命名（OpenList 中文库）必须识别集号，否则整部作品只剩 1 集。

    实测（用户截图）：宝可梦国语三的四部子作品各 144/191/275/189 个文件，
    却都显示为「电影 · 1 集 · N 个文件」，因为文件名是 `第002集.mp4`、
    `146栎树林，寻找大葱鸭.MP4` 这类**中文集号**，既有拉丁集号规则不认。
    同时必须保证项目保护的真标题（86-不存在的战区- / 91Days / 22/7 / 3月的狮子）
    不被误判为集号。
    """

    parser = V4Parser()
    cases = {
        "夸克网盘/动画/宝可梦国语三/无印篇/146栎树林，寻找大葱鸭.MP4": 146,
        "夸克网盘/动画/宝可梦国语三/超级愿望/第002集.mp4": 2,
        "夸克网盘/动画/宝可梦国语三/超世代/155盆才怪与忍者学园!!.mp4": 155,
        "夸克网盘/动画/宝可梦国语三/钻石与珍珠/123樱花宝！让人心疼的对战.mp4": 123,
        # 用户第二次扫描里**未被识别**的残余文件形态：
        "夸克网盘/动画/宝可梦国语三/无印篇/114 再见了，拉普拉斯！.MP4": 114,
        "夸克网盘/动画/宝可梦国语三/钻石与珍珠/181 开幕！神奥联盟‧铃兰大会.mp4": 181,
        "夸克网盘/动画/宝可梦国语三/超级愿望/第062第.mp4": 62,
    }
    for index, (rel, expected) in enumerate(cases.items()):
        facts = parser.parse(_evidence(index, rel), root_container="/夸克网盘")
        assert facts.episode_candidate == expected, f"{rel} 的集号应为 {expected}"
        assert (facts.media_type or "").casefold() == "tv", f"{rel} 应判为剧集而非电影"

    for index, title in enumerate(("86-不存在的战区-", "91Days", "22／7", "3月的狮子"), start=90):
        facts = parser.parse(
            _evidence(index, f"夸克网盘/动画/{title}/[Group] {title} [Ma10p_2160p].mkv"),
            root_container="/夸克网盘",
        )
        assert facts.episode_candidate is None, f"{title} 不得被误判为集号"


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
