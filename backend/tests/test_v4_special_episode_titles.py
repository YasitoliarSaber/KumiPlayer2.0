"""V4 特别篇本地标题、编号与详情展示回归。"""

from __future__ import annotations

import json

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
                    "Show/Specials/[VCB-Studio] Show - SP01 - 露营小剧场 "
                    "[1080p][HEVC].mkv"
                ),
                "source_locator": "local://show/special-01.mkv",
                "playback_locator": "local://show/special-01.mkv",
            },
            {
                "provider": "local",
                "ingest_method": "local_scan",
                "relative_path": (
                    "Show/Specials/[VCB-Studio] Show - SP02 - 温泉小剧场 "
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

    assert facts.special_number == 1
    assert facts.episode_title == "露营小剧场"


@pytest.mark.parametrize(
    ("parent", "filename", "expected_number", "expected_title"),
    [
        (
            "Angel Beats!",
            "Angel Beats! - S00E02 - OVA1：通向天堂的阶梯（Stairway to Heaven）.mkv",
            2,
            "OVA1：通向天堂的阶梯（Stairway to Heaven）",
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


def test_confirmed_specials_keep_distinct_local_titles(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    payload = _special_entry()

    preview = client.post("/api/v4/imports/preview", json=payload)
    assert preview.status_code == 200, preview.text
    confirmed = client.post("/api/v4/imports/rev-special-titles/confirm")
    assert confirmed.status_code == 200, confirmed.text

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT special_number, display_title FROM episodes ORDER BY special_number"
        ).fetchall()

    assert [(row["special_number"], row["display_title"]) for row in rows] == [
        (1, "露营小剧场"),
        (2, "温泉小剧场"),
    ]


def test_detail_prefers_distinct_local_special_titles_over_duplicate_generic_scrape_titles(
    tmp_path,
    monkeypatch,
):
    client, database = _client(tmp_path, monkeypatch)
    payload = _special_entry("rev-special-detail")
    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-special-detail/confirm").status_code == 200

    with database.connect() as conn:
        work_id = conn.execute("SELECT work_id FROM works LIMIT 1").fetchone()["work_id"]
        episode_rows = conn.execute(
            "SELECT episode_id, special_number FROM episodes ORDER BY special_number"
        ).fetchall()
        metadata = {
            "provider": "tmdb",
            "provider_id": "42",
            "episode_mappings": [
                {
                    "episode_id": row["episode_id"],
                    "provider_season_number": 0,
                    "provider_episode_number": row["special_number"],
                    "title": "特别篇",
                }
                for row in episode_rows
            ],
        }
        conn.execute(
            """
            INSERT INTO scrape_bindings(
                binding_id, revision_id, work_id, provider, provider_id,
                metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "binding-special-detail",
                "rev-special-detail",
                work_id,
                "tmdb",
                "42",
                json.dumps(metadata, ensure_ascii=False),
                "2026-08-28T00:00:00+00:00",
                "2026-08-28T00:00:00+00:00",
            ),
        )

    response = client.get(f"/api/library/works/{work_id}")
    assert response.status_code == 200, response.text
    specials = sorted(response.json()["episodes"], key=lambda item: item["special_number"])

    assert [item["special_number"] for item in specials] == [1, 2]
    assert [item["title"] for item in specials] == [
        "露营小剧场",
        "温泉小剧场",
    ]
    assert all("VCB-Studio" not in item["title"] for item in specials)
    assert all("1080p" not in item["title"] for item in specials)


def test_unnumbered_specials_receive_stable_distinct_numbers(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    payload = _special_entry("rev-unnumbered-specials")
    payload["entries"] = [
        {
            "provider": "local",
            "ingest_method": "local_scan",
            "relative_path": "Show/Specials/[VCB-Studio] Show - Winter Camp [1080p].mkv",
            "source_locator": "local://show/winter-camp.mkv",
            "playback_locator": "local://show/winter-camp.mkv",
        },
        {
            "provider": "local",
            "ingest_method": "local_scan",
            "relative_path": "Show/Specials/[VCB-Studio] Show - Hot Spring [1080p].mkv",
            "source_locator": "local://show/hot-spring.mkv",
            "playback_locator": "local://show/hot-spring.mkv",
        },
    ]

    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-unnumbered-specials/confirm").status_code == 200

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT special_number, display_title FROM episodes ORDER BY special_number"
        ).fetchall()

    assert [(row["special_number"], row["display_title"]) for row in rows] == [
        (1, "Hot Spring"),
        (2, "Winter Camp"),
    ]


def test_unnumbered_special_quality_variants_remain_assets_of_one_episode(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    payload = _special_entry("rev-special-variants")
    payload["entries"] = [
        {
            "provider": "local",
            "ingest_method": "local_scan",
            "relative_path": "Show/Specials/[VCB-Studio] Show - Winter Camp [1080p].mkv",
            "source_locator": "local://show/winter-camp-1080p.mkv",
            "playback_locator": "local://show/winter-camp-1080p.mkv",
        },
        {
            "provider": "local",
            "ingest_method": "local_scan",
            "relative_path": "Show/Specials/[VCB-Studio] Show - Winter Camp [2160p].mkv",
            "source_locator": "local://show/winter-camp-2160p.mkv",
            "playback_locator": "local://show/winter-camp-2160p.mkv",
        },
    ]

    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-special-variants/confirm").status_code == 200

    with database.connect() as conn:
        episode_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        asset_count = conn.execute("SELECT COUNT(*) FROM episode_assets").fetchone()[0]
        title = conn.execute("SELECT display_title FROM episodes").fetchone()[0]

    assert episode_count == 1
    assert asset_count == 2
    assert title == "Winter Camp"


def test_sp00_placeholders_are_replaced_by_allocated_distinct_numbers(tmp_path, monkeypatch):
    client, database = _client(tmp_path, monkeypatch)
    payload = _special_entry("rev-sp00-specials")
    payload["entries"] = [
        {
            "provider": "local",
            "ingest_method": "local_scan",
            "relative_path": "Show/Specials/Show - SP00 - Winter Camp.mkv",
            "source_locator": "local://show/sp00-winter.mkv",
            "playback_locator": "local://show/sp00-winter.mkv",
        },
        {
            "provider": "local",
            "ingest_method": "local_scan",
            "relative_path": "Show/Specials/Show - SP00 - Hot Spring.mkv",
            "source_locator": "local://show/sp00-spring.mkv",
            "playback_locator": "local://show/sp00-spring.mkv",
        },
    ]

    assert client.post("/api/v4/imports/preview", json=payload).status_code == 200
    assert client.post("/api/v4/imports/rev-sp00-specials/confirm").status_code == 200

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT special_number, display_title FROM episodes ORDER BY special_number"
        ).fetchall()

    assert [(row["special_number"], row["display_title"]) for row in rows] == [
        (1, "Hot Spring"),
        (2, "Winter Camp"),
    ]


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
