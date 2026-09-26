"""CHECK-007A/B/C：Provider 编号映射、诚实状态与失败分类。

断言经过真实入口：``default_metadata_provider``（元数据 Provider）与
``V4ScrapeService`` + 真实临时 SQLite。Provider 客户端全部是离线 stub，
不发起任何网络请求。
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.media_v4.jobs import metadata as metadata_module
from app.media_v4.jobs.scrape import V4ScrapeService
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService
from app.scrape.tmdb_client import TMDBClientError

_READY_STUB = {
    "provider": "tmdb",
    "provider_id": "42",
    "media_type": "tv",
    "title": "Show",
    "metadata_state": "ready",
    "identity_status": "confirmed",
    "work_metadata_status": "ready",
}


def _episode(
    episode_id: str,
    *,
    season: int | None,
    episode: int | None,
    absolute: int | None = None,
    absolute_origin: str = "",
    season_kind: str = "regular",
    episode_kind: str = "regular",
    provider_season: int | None = None,
    provider_episode: int | None = None,
) -> dict:
    return {
        "episode_id": episode_id,
        "season_id": f"season-{season}",
        "local_season_number": season,
        "local_episode_number": episode,
        "absolute_episode_number": absolute,
        "absolute_origin": absolute_origin,
        "season_kind": season_kind,
        "episode_kind": episode_kind,
        "special_number": None,
        "display_title": episode_id,
        "provider_season_number": provider_season,
        "provider_episode_number": provider_episode,
    }


def _target(
    episodes: list[dict],
    *,
    media_type: str = "tv",
    work_type: str = "series",
    verified_offsets: dict | None = None,
) -> dict:
    target = {
        "work_type": work_type,
        "preferred_title": "Show",
        "episodes": episodes,
        "provider_bindings": [
            {"provider": "tmdb", "provider_id": "42", "media_type": media_type}
        ],
    }
    if verified_offsets:
        target["verified_season_offsets"] = verified_offsets
    return target


def _install_client(
    monkeypatch,
    *,
    seasons: dict[int, list[int]] | None = None,
    season_errors: dict[int, Exception] | None = None,
    detail: dict | None = None,
    requested: list[int] | None = None,
) -> None:
    """安装离线 TMDB stub：只按编号返回集条目，不联网。"""

    seasons = seasons or {}
    season_errors = season_errors or {}
    detail = detail or {"name": "Show", "images": {}, "episode_run_time": [24]}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get_tv_detail(self, _provider_id):
            return detail

        def get_tv_season_episodes(self, _provider_id, season_number):
            if requested is not None:
                requested.append(season_number)
            error = season_errors.get(season_number)
            if error is not None:
                raise error
            return {
                "episodes": [
                    {"episode_number": number, "id": 1000 + number, "name": f"E{number}"}
                    for number in seasons.get(season_number, [])
                ]
            }

        @staticmethod
        def select_best_poster(_images):
            return ""

        @staticmethod
        def select_best_backdrop(_images):
            return ""

        @staticmethod
        def select_best_logo(_images):
            return ""

    monkeypatch.setattr(metadata_module, "TMDBClient", Client)
    monkeypatch.setattr(
        metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="fixture")
    )


def _continuous_detail(episode_count: int) -> dict:
    return {
        "name": "Show",
        "images": {},
        "episode_run_time": [24],
        "seasons": [{"season_number": 1, "episode_count": episode_count}],
    }


def _mapped_ids(result: dict) -> list[str]:
    return sorted(str(item["episode_id"]) for item in result.get("episode_mappings") or [])


def _unmapped_reason(result: dict, episode_id: str) -> str:
    for item in result.get("unmapped_reasons") or []:
        if str(item.get("episode_id")) == episode_id:
            return str(item.get("reason"))
    return ""


# --- CHECK-007A：编号证据不足就不猜 -----------------------------------------


def test_only_second_season_is_not_mapped_to_first_episode(monkeypatch):
    """CHECK-007A: 只有 S2、无绝对编号时既不能映射成 S1E1，也不能失败。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(24), seasons={1: list(range(1, 13))}
    )
    target = _target([_episode("s2e1", season=2, episode=1)])
    original = deepcopy(target)

    result = metadata_module.default_metadata_provider(target)

    assert result["metadata_state"] == "ready"
    assert result["episode_mappings"] == []
    assert result["episode_mapping_status"] == "unmapped"
    assert result["mapped_count"] == 0 and result["total_count"] == 1
    assert _unmapped_reason(result, "s2e1") == "insufficient_numbering_evidence"
    assert target == original


@pytest.mark.parametrize(
    "episodes",
    [
        [_episode("s3e1", season=3, episode=1)],
        [
            _episode("s1e12", season=1, episode=12),
            _episode("s2e1", season=2, episode=1),
        ],
        [
            _episode("s2e13", season=2, episode=13),
            _episode("s2e14", season=2, episode=14),
        ],
    ],
)
def test_missing_or_partial_coverage_never_guesses_offset(monkeypatch, episodes):
    """CHECK-007A: 后续季、缺集、缺前季与未知季都不构成偏移依据。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(12), seasons={1: list(range(1, 13))}
    )
    target = _target(episodes)
    original = deepcopy(target)

    result = metadata_module.default_metadata_provider(target)

    assert result["episode_mappings"] == []
    assert result["metadata_state"] in {"ready", "waiting_review"}
    for episode in episodes:
        assert _unmapped_reason(result, episode["episode_id"]) in {
            "insufficient_numbering_evidence",
            "unknown_local_season",
            "missing_local_number",
        }
    assert target == original


def test_single_unassigned_season_maps_only_with_single_provider_season(monkeypatch):
    """CHECK-007A: 本地全部未分季 + 在线只有一个常规季时才在该季命名空间映射，本地 null 不变。"""

    _install_client(monkeypatch, detail=_continuous_detail(3), seasons={1: [1, 2, 3]})
    target = _target(
        [
            _episode("u1", season=None, episode=1, season_kind="unassigned"),
            _episode("u2", season=None, episode=2, season_kind="unassigned"),
        ]
    )

    result = metadata_module.default_metadata_provider(target)

    assert _mapped_ids(result) == ["u1", "u2"]
    numbers = {
        str(item["episode_id"]): (item["provider_season_number"], item["provider_episode_number"])
        for item in result["episode_mappings"]
    }
    assert numbers == {"u1": (1, 1), "u2": (1, 2)}
    assert target["episodes"][0]["local_season_number"] is None
    assert target["episodes"][0]["local_episode_number"] == 1


def test_explicit_absolute_number_maps_only_when_online_episode_exists(monkeypatch):
    """CHECK-007A: 明确绝对编号在线上存在才映射；缺失只记缺项。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(24), seasons={1: list(range(1, 25))}
    )
    target = _target(
        [
            _episode("abs13", season=2, episode=13, absolute=13, absolute_origin="explicit_absolute"),
            _episode("abs25", season=2, episode=25, absolute=25, absolute_origin="explicit_absolute"),
        ]
    )

    result = metadata_module.default_metadata_provider(target)

    assert _mapped_ids(result) == ["abs13"]
    mapping = result["episode_mappings"][0]
    assert (mapping["provider_season_number"], mapping["provider_episode_number"]) == (1, 13)
    assert _unmapped_reason(result, "abs25") == "provider_resource_missing"
    assert result["episode_mapping_status"] == "partial"


def test_versioned_verified_offset_maps_and_keeps_local_numbers(monkeypatch):
    """CHECK-007A: 带版本的已核验偏移才允许映射，本地编号不变。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(30), seasons={1: list(range(1, 31))}
    )
    episodes = [
        _episode("s2e13", season=2, episode=13),
        _episode("s2e14", season=2, episode=14),
    ]
    target = _target(episodes, verified_offsets={2: {"offset": 12, "rule_version": "v1"}})

    result = metadata_module.default_metadata_provider(target)

    assert _mapped_ids(result) == ["s2e13", "s2e14"]
    numbers = {
        str(item["episode_id"]): (item["provider_season_number"], item["provider_episode_number"])
        for item in result["episode_mappings"]
    }
    assert numbers == {"s2e13": (1, 25), "s2e14": (1, 26)}
    assert result["episode_mapping_status"] == "complete"
    assert target["episodes"][0]["local_season_number"] == 2
    assert target["episodes"][0]["local_episode_number"] == 13
    assert target["episodes"][0]["provider_episode_number"] is None


def test_existing_applicable_mapping_is_not_overwritten(monkeypatch):
    """CHECK-007A: 已有适用逐条映射不被偏移规则覆盖。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(30), seasons={1: list(range(1, 31))}
    )
    target = _target(
        [_episode("s2e13", season=2, episode=13, provider_season=1, provider_episode=7)],
        verified_offsets={2: {"offset": 12, "rule_version": "v1"}},
    )

    result = metadata_module.default_metadata_provider(target)

    assert len(result["episode_mappings"]) == 1
    mapping = result["episode_mappings"][0]
    assert (mapping["provider_season_number"], mapping["provider_episode_number"]) == (1, 7)


def test_remote_missing_episode_is_recorded_as_missing_only(monkeypatch):
    """CHECK-007A: 线上缺某一集只记录缺项，不改变已成功的映射与本地事实。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(14), seasons={1: list(range(1, 15))}
    )
    target = _target(
        [
            _episode("s2e1", season=2, episode=1),
            _episode("s2e2", season=2, episode=2),
            _episode("s2e3", season=2, episode=3),
        ],
        verified_offsets={2: {"offset": 12, "rule_version": "v1"}},
    )

    result = metadata_module.default_metadata_provider(target)

    assert _mapped_ids(result) == ["s2e1", "s2e2"]
    assert _unmapped_reason(result, "s2e3") == "provider_resource_missing"
    assert result["metadata_state"] == "ready"
    assert result["work_metadata_status"] == "ready"
    assert result["episode_mapping_status"] == "partial"


# --- CHECK-007B：诚实状态与计数 ---------------------------------------------


def test_two_unmatched_episodes_report_unmapped_zero_of_two(monkeypatch):
    """CHECK-007B: 一季两集全不匹配 → unmapped 0/2，且不是服务故障。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(2), seasons={1: [9]}
    )
    target = _target(
        [_episode("s1e1", season=1, episode=1), _episode("s1e2", season=1, episode=2)]
    )

    result = metadata_module.default_metadata_provider(target)

    assert result["episode_mapping_status"] == "unmapped"
    assert (result["mapped_count"], result["total_count"]) == (0, 2)
    assert sorted(result["unmapped_episode_ids"]) == ["s1e1", "s1e2"]
    assert result["metadata_state"] == "ready"
    assert result["work_metadata_status"] == "ready"
    assert result["retryable"] is False
    assert set(result["unmapped_reason_codes"]) == {"provider_resource_missing"}


@pytest.mark.parametrize(
    "available, expected_reasons",
    [
        ([1, 2, 5, 6], {"s1e3": "provider_resource_missing", "s1e4": "provider_resource_missing"}),
        ([1, 2, 3, 4], {"s1e5": "provider_resource_missing", "s1e6": "provider_resource_missing"}),
    ],
)
def test_partial_coverage_reports_exact_counts(monkeypatch, available, expected_reasons):
    """CHECK-007B: 中间或尾部缺集一律 partial，并给出准确计数与原因。"""

    _install_client(monkeypatch, detail=_continuous_detail(6), seasons={1: available})
    target = _target([_episode(f"s1e{n}", season=1, episode=n) for n in range(1, 7)])

    result = metadata_module.default_metadata_provider(target)

    assert result["episode_mapping_status"] == "partial"
    assert (result["mapped_count"], result["total_count"]) == (len(available), 6)
    assert result["metadata_state"] == "ready"
    assert result["retryable"] is False
    assert {
        episode_id: _unmapped_reason(result, episode_id) for episode_id in sorted(expected_reasons)
    } == expected_reasons


def test_missing_episodes_are_not_reported_as_service_outage(monkeypatch):
    """CHECK-007B: 四集缺项仍是资料可用 + 映射 partial，不写成服务不可用。"""

    _install_client(monkeypatch, detail=_continuous_detail(4), seasons={1: []})
    target = _target([_episode(f"s1e{n}", season=1, episode=n) for n in range(1, 5)])

    result = metadata_module.default_metadata_provider(target)

    assert result["metadata_state"] == "ready"
    assert result["reason_code"] == "episode_mapping_incomplete"
    assert result["episode_mapping_status"] == "unmapped"
    assert (result["mapped_count"], result["total_count"]) == (0, 4)
    assert result["retryable"] is False
    assert "服务不可用" not in str(result.get("reason") or "")
    assert "服务不可用" not in str(result.get("metadata_warning") or "")


def test_complete_mapping_reports_complete_and_succeeded(monkeypatch):
    """CHECK-007B: 全部映射成功时状态 complete，且刷新标记为 succeeded。"""

    _install_client(
        monkeypatch, detail=_continuous_detail(3), seasons={1: [1, 2, 3]}
    )
    target = _target([_episode(f"s1e{n}", season=1, episode=n) for n in range(1, 4)])

    result = metadata_module.default_metadata_provider(target)

    assert result["episode_mapping_status"] == "complete"
    assert (result["mapped_count"], result["total_count"]) == (3, 3)
    assert result["refresh_status"] == "succeeded"
    assert result["unmapped_episode_ids"] == []
    assert result["retryable"] is False


def test_scrape_job_records_mapping_state_and_keeps_metadata(tmp_path, monkeypatch):
    """CHECK-007B: 刮削任务的 job 结果与资料状态分离，作品资料保留。"""

    database = V4Database(tmp_path / "numbering-job.db")
    database.initialize()
    service = V4RevisionService(database)
    evidence, facts = _db_entry()
    service.create_draft("rev-num", [(evidence, facts)])
    service.confirm("rev-num")

    with database.connect() as conn:
        episode_id = str(
            conn.execute(
                "SELECT episode_id FROM revision_bindings WHERE revision_id = 'rev-num'"
            ).fetchone()["episode_id"]
        )

    scrape = V4ScrapeService(database)
    job = next(
        item
        for item in scrape.enqueue_for_revision("rev-num")
        if item["job_type"] == "scrape_work"
    )
    scrape.process(
        job["job_id"],
        lambda target: {
            **_READY_STUB,
            "episodes": target["episodes"],
            "episode_mappings": [],
            "metadata_state": "ready",
            "work_metadata_status": "ready",
            "episode_mapping_status": "unmapped",
            "mapped_count": 0,
            "total_count": 1,
            "unmapped_episode_ids": [episode_id],
            "unmapped_reasons": [
                {"episode_id": episode_id, "reason": "insufficient_numbering_evidence"}
            ],
            "reason_code": "episode_mapping_incomplete",
            "retryable": False,
        },
        mirror_root=tmp_path / "mirror",
    )

    with database.connect() as conn:
        job_row = conn.execute(
            "SELECT status, result_json FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()
        binding = conn.execute(
            "SELECT status, metadata_json FROM scrape_bindings WHERE revision_id = 'rev-num'"
        ).fetchone()
    import json

    payload = json.loads(job_row["result_json"])
    assert job_row["status"] == "succeeded"
    assert payload["outcome"] == "partial"
    assert payload["episode_mapping_status"] == "unmapped"
    assert payload["mapped_count"] == 0 and payload["total_count"] == 1
    assert payload["retryable"] is False
    assert payload["unmapped_reasons"][0]["reason"] == "insufficient_numbering_evidence"
    assert binding["status"] == "confirmed"
    stored = json.loads(binding["metadata_json"])
    assert stored["title"] == "Show"
    assert stored["work_metadata_status"] == "ready"


# --- CHECK-007C：失败分类、未知类型与结果边界 -------------------------------


@pytest.mark.parametrize(
    "error, expected_reason, expected_retryable",
    [
        (
            TMDBClientError("missing", status_code=404, reason_code="provider_resource_missing", retryable=False),
            "provider_resource_missing",
            False,
        ),
        (
            TMDBClientError("auth", status_code=401, reason_code="provider_auth_required", retryable=False),
            "provider_auth_required",
            False,
        ),
        (
            TMDBClientError("rate", status_code=429, reason_code="provider_rate_limited", retryable=True),
            "provider_rate_limited",
            True,
        ),
        (TMDBClientError("timeout"), "source_unavailable", True),
        (
            TMDBClientError("invalid json", status_code=422, retryable=False),
            "invalid_response",
            False,
        ),
    ],
)
def test_season_failures_are_classified_by_reason(monkeypatch, error, expected_reason, expected_retryable):
    """CHECK-007C: 404/401/429/超时/无效响应按原因区分，作品字段保留。"""

    _install_client(
        monkeypatch,
        detail=_continuous_detail(2),
        seasons={},
        season_errors={1: error},
    )
    target = _target([_episode("s1e1", season=1, episode=1)])

    result = metadata_module.default_metadata_provider(target)

    assert result["reason_code"] == expected_reason or result["reason_code"] == "episode_mapping_incomplete"
    assert result["retryable"] is expected_retryable
    assert result["title"] == "Show"
    season_result = (result.get("season_results") or [{}])[0]
    if expected_reason == "provider_resource_missing":
        # 404 是明确“该季在线不存在”，不能写成服务不可用。
        assert result["metadata_state"] == "ready"
        assert season_result.get("status") == "provider_resource_missing"
    else:
        assert result["metadata_state"] == "source_unavailable"
        assert season_result.get("reason_code") == expected_reason


def test_unknown_media_type_never_queries_provider(monkeypatch):
    """CHECK-007C: 未知类型不猜电影、不发起任何在线查询。"""

    class ExplodingClient:
        def __init__(self, **_kwargs):
            raise AssertionError("未知类型不得构造 Provider 客户端")

    monkeypatch.setattr(metadata_module, "TMDBClient", ExplodingClient)
    monkeypatch.setattr(
        metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="fixture")
    )
    target = {
        "work_type": "unknown",
        "preferred_title": "Mystery",
        "episodes": [_episode("u1", season=None, episode=1, season_kind="unassigned")],
        "provider_bindings": [],
    }

    result = metadata_module.default_metadata_provider(target)

    assert result["metadata_state"] == "waiting_review"
    assert result["reason_code"] == "unknown_media_type"
    assert result["identity_status"] == "unresolved"
    assert result.get("episode_mappings") in (None, [])
    assert result["episode_mapping_status"] == "not_applicable"
    assert result["retryable"] is False


def test_provider_hint_conflict_never_queries_provider(monkeypatch):
    """CHECK-007C: 提示冲突不自动绑定，也不发起搜索。"""

    class ExplodingClient:
        def __init__(self, **_kwargs):
            raise AssertionError("提示冲突不得构造 Provider 客户端")

    monkeypatch.setattr(metadata_module, "TMDBClient", ExplodingClient)
    monkeypatch.setattr(
        metadata_module, "load_config", lambda: SimpleNamespace(tmdb_bearer_token="fixture")
    )
    target = {
        "work_type": "series",
        "preferred_title": "Show",
        "provider_hint_conflict": True,
        "episodes": [_episode("s1e1", season=1, episode=1)],
        "provider_bindings": [],
    }

    result = metadata_module.default_metadata_provider(target)

    assert result["metadata_state"] == "waiting_review"
    assert result["reason_code"] == "provider_hint_conflict"
    assert result["identity_status"] == "conflict"


def test_foreign_episode_mapping_is_rejected_without_touching_local_facts(tmp_path):
    """CHECK-007C: Provider 结果夹带非当前 Episode 时拒绝落库，本地事实不变。"""

    database = V4Database(tmp_path / "numbering-foreign.db")
    database.initialize()
    service = V4RevisionService(database)
    evidence, facts = _db_entry()
    service.create_draft("rev-foreign", [(evidence, facts)])
    service.confirm("rev-foreign")

    with database.connect() as conn:
        before = {
            "parsed": [dict(row) for row in conn.execute("SELECT * FROM parsed_facts").fetchall()],
            "episodes": [
                dict(row) for row in conn.execute("SELECT * FROM episodes").fetchall()
            ],
        }

    scrape = V4ScrapeService(database)
    job = next(
        item
        for item in scrape.enqueue_for_revision("rev-foreign")
        if item["job_type"] == "scrape_work"
    )
    with pytest.raises(ValueError, match="不属于当前 revision"):
        scrape.process(
            job["job_id"],
            lambda _target: {
                **_READY_STUB,
                "episode_mappings": [
                    {
                        "episode_id": "episode-from-another-work",
                        "provider_season_number": 1,
                        "provider_episode_number": 1,
                        "provider_episode_id": "999",
                    }
                ],
            },
            mirror_root=tmp_path / "mirror",
        )

    with database.connect() as conn:
        job_status = conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job["job_id"],)
        ).fetchone()["status"]
        after = {
            "parsed": [dict(row) for row in conn.execute("SELECT * FROM parsed_facts").fetchall()],
            "episodes": [
                dict(row) for row in conn.execute("SELECT * FROM episodes").fetchall()
            ],
        }
        mappings = conn.execute("SELECT COUNT(*) FROM episode_provider_mappings").fetchone()[0]
    assert job_status == "failed"
    assert after == before
    assert mappings == 0


def test_successful_mapping_does_not_rewrite_local_coordinates(tmp_path):
    """CHECK-007C: Provider 映射只写自己的坐标，不改本地季集事实。"""

    database = V4Database(tmp_path / "numbering-local-facts.db")
    database.initialize()
    service = V4RevisionService(database)
    evidence, facts = _db_entry(season=2, episode=13)
    service.create_draft("rev-local", [(evidence, facts)])
    service.confirm("rev-local")

    with database.connect() as conn:
        episode_id = str(
            conn.execute(
                "SELECT episode_id FROM revision_bindings WHERE revision_id = 'rev-local'"
            ).fetchone()["episode_id"]
        )

    scrape = V4ScrapeService(database)
    job = next(
        item
        for item in scrape.enqueue_for_revision("rev-local")
        if item["job_type"] == "scrape_work"
    )
    scrape.process(
        job["job_id"],
        lambda target: {
            **_READY_STUB,
            "episode_mappings": [
                {
                    "episode_id": str(target["episodes"][0]["episode_id"]),
                    "provider_season_number": 1,
                    "provider_episode_number": 13,
                    "provider_episode_id": "900",
                    "title": "远端标题",
                }
            ],
            "metadata_state": "ready",
            "episode_mapping_status": "complete",
            "mapped_count": 1,
            "total_count": 1,
        },
        mirror_root=tmp_path / "mirror",
    )

    with database.connect() as conn:
        season_row = conn.execute(
            "SELECT local_season_number FROM seasons"
        ).fetchone()
        episode_row = conn.execute(
            """
            SELECT local_episode_number, absolute_episode_number FROM episodes
            WHERE episode_id = ?
            """,
            (episode_id,),
        ).fetchone()
        mapping = conn.execute(
            "SELECT provider_season_number, provider_episode_number FROM episode_provider_mappings WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
    assert season_row["local_season_number"] == 2
    assert episode_row["local_episode_number"] == 13
    assert episode_row["absolute_episode_number"] is None
    assert (mapping["provider_season_number"], mapping["provider_episode_number"]) == (1, 13)


def _db_entry(*, season: int = 2, episode: int = 13):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id="ev-num",
        scan_id="scan-num",
        root_id="root-num",
        source_key="Show/Season 2/Show.S02E13.mkv",
        relative_path="Show/Season 2/Show.S02E13.mkv",
        entry_kind="video",
        provider="local",
    )
    facts = ParsedFacts(
        parsed_fact_id="facts-num",
        evidence_id=evidence.evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        media_type="tv",
        group_type="season",
        season_candidate=season,
        episode_candidate=episode,
    )
    return evidence, facts
