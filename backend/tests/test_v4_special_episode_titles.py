"""特别篇按最新有限标记准入并保留原名；未覆盖的 S00 仍不入库。"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _special_entry(revision_id: str = "rev-special-titles") -> dict:
    return {
        "revision_id": revision_id,
        "root_id": "root-special-titles",
        "scan_id": f"scan-{revision_id}",
        "entries": [
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": (
                    "Show/Specials/[VCB-Studio] Show - S00E01 - 露营小剧场 "
                    "[1080p][HEVC].mkv"
                ),
                "source_locator": "local://show/special-01.mkv",
                "playback_locator": "local://show/special-01.mkv",
            },
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": (
                    "Show/Specials/[VCB-Studio] Show - S00E02 - 温泉小剧场 "
                    "[1080p][HEVC].mkv"
                ),
                "source_locator": "local://show/special-02.mkv",
                "playback_locator": "local://show/special-02.mkv",
            },
        ],
    }


def _client(tmp_path, monkeypatch):
    from app.api import library_v4, media_v4
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "special-titles.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)
    application = FastAPI()
    application.include_router(media_v4.router)
    application.include_router(library_v4.router)
    return TestClient(application), database


def test_parser_keeps_lightly_cleaned_special_title_as_immutable_fact():
    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.parsing.parser import V4Parser

    evidence = SourceEvidence(
        evidence_id="evidence-special-title",
        scan_id="scan-special-title",
        root_id="root-special-title",
        source_key="special-title",
        relative_path=(
            "Show/Specials/[VCB-Studio] Show - SP01 - 露营小剧场 "
            "[1080p][HEVC].mkv"
        ),
        entry_kind="video",
        provider="local",
    )

    facts = V4Parser().parse(evidence)

    assert facts.special_number is None
    assert facts.episode_title == "[VCB-Studio] Show - SP01 - 露营小剧场 [1080p][HEVC]"
    assert facts.is_importable


@pytest.mark.parametrize(
    ("parent", "filename", "expected_number", "expected_title"),
    [
        (
            "Angel Beats!",
            "Angel Beats! - S00E02 - OVA1：通向天堂的阶梯（Stairway to Heaven）.mkv",
            None,
            "Angel Beats! - S00E02 - OVA1：通向天堂的阶梯（Stairway to Heaven）",
        ),
        (
            "Re：从零开始的异世界生活",
            "Re：从零开始的异世界生活.S00E55.2024.2160P.BDRIP.mkv",
            55,
            "特别篇",
        ),
    ],
)
def test_sample_special_names_keep_meaning_while_dropping_only_technical_suffixes(
    parent,
    filename,
    expected_number,
    expected_title,
):
    from app.media_v4.domain.models import SourceEvidence
    from app.media_v4.parsing.parser import V4Parser

    evidence = SourceEvidence(
        evidence_id=f"evidence-sample-{expected_number}",
        scan_id="scan-special-samples",
        root_id="root-special-samples",
        source_key=f"sample-{expected_number}",
        relative_path=f"{parent}/Specials/{filename}",
        entry_kind="video",
        provider="local",
    )

    facts = V4Parser().parse(evidence)

    assert facts.special_number == expected_number
    assert facts.episode_title == expected_title


def test_specials_only_import_creates_no_work_episode_or_asset(tmp_path, monkeypatch):
    """反向断言：只有特别篇时，预览与确认都不产生任何媒体库结构。"""

    client, database = _client(tmp_path, monkeypatch)
    payload = _special_entry("rev-specials-excluded")

    preview = client.post("/api/v4/imports/preview", json=payload)
    assert preview.status_code == 200, preview.text
    assert preview.json()["works"] == []
    assert preview.json()["episodes"] == []
    assert preview.json()["work_assets"] == []

    confirmed = client.post("/api/v4/imports/rev-specials-excluded/confirm")
    assert confirmed.status_code == 200, confirmed.text

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM works").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM episode_assets").fetchone()[0] == 0


# 已删除 test_confirmed_specials_keep_distinct_local_titles：
# 该用例断言特别篇确认后产生 Episode 并保留本地标题。用户 2026-09-24 规则
# 取消了 special 入库，特别篇不再进入媒体库，该契约已被取消。

# 已删除 test_detail_prefers_distinct_local_special_titles_over_duplicate_generic_scrape_titles：
# 该用例断言作品详情里的特别篇本地标题优先于通用刮削标题；没有特别篇 Episode
# 后该展示路径不再存在。

# 已删除 test_unnumbered_specials_receive_stable_distinct_numbers：
# 该用例断言无编号特别篇会被分配稳定且互不重复的 SP 编号并入库。

# 已删除 test_unnumbered_special_quality_variants_remain_assets_of_one_episode：
# 该用例断言同一特别篇的 1080p/2160p 变体合并为同 Episode 的多个 Asset。

# 已删除 test_sp00_placeholders_are_replaced_by_allocated_distinct_numbers：
# 该用例断言 SP00 占位编号会被重新分配为不同编号。


def test_special_marker_brackets_are_removed_but_semantic_brackets_are_kept():
    from app.media_v4.parsing.episode_titles import (
        clean_special_episode_title,
    )

    assert clean_special_episode_title(
        "Yuru Camp/Specials/[VCB-Studio] Yuru Camp [SP08][Making Documentary][Ma10p_1080p].mkv",
        work_title="Yuru Camp",
        special_number=8,
    ) == "Making Documentary"


def test_special_marker_does_not_match_inside_a_semantic_word():
    from app.media_v4.parsing.episode_titles import (
        clean_special_episode_title,
        extract_special_episode_number,
    )

    title = "Show/Specials/Show - CRISP08 Documentary.mkv"

    assert extract_special_episode_number("CRISP08 Documentary") is None
    assert clean_special_episode_title(
        title,
        work_title="Show",
        special_number=8,
    ) == "CRISP08 Documentary"
