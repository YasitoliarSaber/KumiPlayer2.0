from dataclasses import replace

import pytest

from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.resolution.resolver import MediaResolver


def entry(eid, *, root="root", prefix="", year=None, special=False, container="Show"):
    evidence = SourceEvidence(
        evidence_id=eid, scan_id="scan", root_id=root, source_key=eid,
        relative_path=f"{prefix}{container}/{'Specials' if special else 'Season 1'}/{eid}.mkv",
        entry_kind="video", provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{eid}", evidence_id=eid, parser_version="test",
        work_title="Show", title_candidates=("Show",), media_type="tv",
        year_candidate=year, group_type="special" if special else "season",
        season_candidate=0 if special else 1, episode_candidate=None if special else 1,
        special_candidate=special, special_number=1 if special else None,
    )
    return evidence, facts


@pytest.mark.parametrize("reverse", [False, True])
def test_yearless_special_reuses_unique_work_in_exact_source_directory(reverse):
    entries = [entry("main", year=2024), entry("bonus", special=True)]
    graph = MediaResolver().resolve(entries[::-1] if reverse else entries)
    assert len(graph.works) == 1
    assert graph.works[0].year == 2024
    assert {episode.local_season_number for episode in graph.episodes} == {0, 1}
    assert entries[1][1].year_candidate is None


@pytest.mark.parametrize("reverse", [False, True])
def test_work_year_takes_earliest_known_year_regardless_of_input_order(reverse):
    """同一作品目录给出多个年份时取最早的一年，不能“先出现的赢”。

    条目键来自目录名里的年份（Show 2024），因此两条属性不同的年份属于同一个 Work；
    作品年份必须与输入顺序无关。
    """

    entries = [
        entry("season-1", container="Show 2024", year=2024),
        entry("season-2", container="Show 2024", year=2025),
    ]
    graph = MediaResolver().resolve(entries[::-1] if reverse else entries)

    assert len(graph.works) == 1
    assert graph.works[0].year == 2024


@pytest.mark.parametrize("scope", ["root", "ambiguous", "standalone"])
def test_yearless_entry_does_not_borrow_year_from_other_work_scope(scope):
    main = entry("main", year=2024)
    special = entry("bonus", root="other" if scope == "root" else "root", special=True)
    if scope == "standalone":
        special = (special[0], replace(special[1], card_type="standalone"))
    entries = [main, special]
    if scope == "ambiguous":
        entries.append(entry("remake", year=1998))
    graph = MediaResolver().resolve(entries)
    main_work = next(work for work in graph.works if "main" in work.source_evidence_ids)
    assert "bonus" not in main_work.source_evidence_ids


def test_yearless_entry_borrows_unique_year_across_directories_in_same_root():
    """同一 root、同一边界标题、同一媒体类型下只有唯一已知年份时，缺年份条目并入该年份。

    R3 把 `_coalesce_missing_year_keys` 的作用域从"同一实际作品目录"放宽为
    "root + 边界标题 + 媒体类型"：同一部作品的两份物理目录本来就常一侧写年份、
    一侧不写（实测 `天元突破红莲螺岩.2007` 与 `T 4k 天元突破 红莲螺岩`）。
    与上一条负例的差别**只有目录**，因此这里必须合并；两个都带年份且不同、
    或跨 root、或媒体类型不同的情形仍拆开（上一条负例覆盖前两项）。
    """

    main = entry("main", year=2024)
    special = entry("bonus", prefix="Elsewhere/", special=True)
    graph = MediaResolver().resolve([main, special])

    assert len(graph.works) == 1
    work = graph.works[0]
    assert set(work.source_evidence_ids) == {"main", "bonus"}
    assert work.year == 2024
    # 集号/季号语义不受影响：特别篇仍是 special season，正片仍是 Season 1。
    assert {(episode.local_season_number, episode.season_kind) for episode in graph.episodes} == {
        (0, "special"),
        (1, "regular"),
    }
