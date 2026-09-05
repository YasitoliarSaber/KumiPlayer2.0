"""V4 必须保留 34b4f2f（V4 重构前最后版本）的核心识别语义。

这里不复活旧 ImportPlan 或旧数据库，只把用户可观察的目录树识别结果转换为
当前 ParsedFacts / MediaGraph 合同，防止架构替换再次删掉成熟能力。
"""

from __future__ import annotations

import pytest

from app.media_v4.domain.models import SourceEvidence
from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.resolution.resolver import MediaResolver


def _evidence(index: int, relative_path: str, *, provider: str = "pan115") -> SourceEvidence:
    return SourceEvidence(
        evidence_id=f"pre-v4-{index}",
        scan_id="scan-pre-v4-compatibility",
        root_id="root-pre-v4-compatibility",
        source_key=relative_path,
        relative_path=relative_path,
        entry_kind="video",
        provider=provider,
        ingest_method="directory_tree",
    )


@pytest.mark.parametrize(
    ("relative_path", "season", "episode"),
    (
        (
            "动画/飞跃巅峰 内封中字/S1/"
            "【Top o Nerae! GunBuster】【01】【BDrip】【HEVC 2880x2160p FLAC】.mkv",
            1,
            1,
        ),
        (
            "动画/飞跃巅峰 内封中字/S2/"
            "【Top o Nerae2! DieBuster】【01】【BDrip】【HEVC 3840x2160p FLAC】.mkv",
            2,
            1,
        ),
        (
            "动画/伪恋.S1-S2+OAD/伪恋.NISEKOI.[S2].2014/S2 [01].mkv",
            2,
            1,
        ),
        (
            "刮削好的动画/奇巧计程车/Season 1/"
            "奇巧出租车 - S02E01 - 奇怪的司机.mkv",
            1,
            1,
        ),
    ),
)
def test_pre_v4_explicit_directory_season_remains_authoritative(
    relative_path: str,
    season: int,
    episode: int,
):
    evidence = _evidence(0, relative_path)
    parsed = normalize_batch_parsed_facts([(evidence, V4Parser().parse(evidence))])
    facts = parsed[0][1]

    assert facts.group_type == "season"
    assert facts.season_candidate == season
    assert facts.episode_candidate == episode
    assert facts.needs_review is False


@pytest.mark.parametrize(
    "relative_path",
    (
        "动画/飞跃巅峰 内封中字/S2/"
        "【Top o Nerae2! DieBuster】【NCED】【BDrip】【HEVC 3840x2160p FLAC】.mkv",
        "动画/鬼灭之刃系列/1.立志篇.[S1].2019/"
        "[MAI] Kimetsu no Yaiba [NCOP][Ma10p_2160p][x265_flac].mkv",
        "动画/间谍过家家.S1-S2+剧场版/MV合集/"
        "[MAI] Spy x Family [MV Breeze ~(K)NoW_NAME~][Ma10p_2160p].mkv",
        "动画/Show/PV/Show PV01.mkv",
    ),
)
def test_pre_v4_op_ed_mv_and_pv_are_excluded_from_library(relative_path: str):
    facts = V4Parser().parse(_evidence(0, relative_path))

    assert facts.group_type in {"ignored", "auxiliary"}
    assert facts.is_importable is False
    assert facts.needs_review is False


def test_pre_v4_series_seasons_specials_and_movie_keep_their_boundaries():
    paths = [
        "动画/中二病也要谈恋爱.S1-S2+剧场版/"
        "1.中二病也要谈恋爱.[S1].2012/[MAI] Chuunibyou demo Koi ga Shitai! [01].mkv",
        "动画/中二病也要谈恋爱.S1-S2+剧场版/"
        "3.中二病也要谈恋爱.[S2].2014/[MAI] Chuunibyou demo Koi ga Shitai! Ren [01].mkv",
        "动画/中二病也要谈恋爱.S1-S2+剧场版/"
        "4.剧场版：Take On Me.2018/[MAI] Chuunibyou demo Koi ga Shitai! -Take On Me- [Movie].mkv",
        "动画/中二病也要谈恋爱.S1-S2+剧场版/"
        "4.剧场版：Take On Me.2018/[MAI] Chuunibyou demo Koi ga Shitai! -Take On Me- [SP02].mkv",
    ]
    evidence = [_evidence(index, path) for index, path in enumerate(paths)]
    parser = V4Parser()
    parsed = normalize_batch_parsed_facts([(item, parser.parse(item)) for item in evidence])
    graph = MediaResolver().resolve(parsed)

    tv_work = next(work for work in graph.works if work.media_type == "tv")
    movie_work = next(work for work in graph.works if work.media_type == "movie")
    tv_episodes = [episode for episode in graph.episodes if episode.work_key == tv_work.work_key]

    assert {episode.local_season_number for episode in tv_episodes} == {0, 1, 2}
    assert len([episode for episode in tv_episodes if episode.episode_kind == "special"]) == 1
    assert len(graph.work_assets) == 1
    assert graph.work_assets[0].work_key == movie_work.work_key


def test_pre_v4_continuous_absolute_numbers_are_rebased_inside_explicit_later_season():
    entries = []
    parser = V4Parser()
    for index, number in enumerate(range(13, 16)):
        evidence = _evidence(
            index,
            "动画/Yuru Camp/Yuru Camp Season 2/"
            f"Yuru Camp Season 2 [{number:02d}].mkv",
        )
        entries.append((evidence, parser.parse(evidence)))

    normalized = normalize_batch_parsed_facts(entries)

    assert [facts.season_candidate for _item, facts in normalized] == [2, 2, 2]
    assert [facts.episode_candidate for _item, facts in normalized] == [1, 2, 3]


def test_pre_v4_single_file_anime_movie_is_not_forced_into_a_tv_season():
    relative_path = (
        "动画/佐贺偶像是传奇 梦幻银河乐园/"
        "[FM Zombie Sub]Zombie Land Saga Yumeginga Paradise"
        "[BDRip][1080p][HEVC_Opus][CHS-JPN][Softsub].mkv"
    )
    evidence = _evidence(0, relative_path)
    facts = V4Parser().parse(evidence)
    graph = MediaResolver().resolve([(evidence, facts)])

    assert facts.group_type == "movie"
    assert len(graph.works) == 1
    assert graph.works[0].media_type == "movie"
    assert graph.episodes == ()
    assert len(graph.work_assets) == 1
