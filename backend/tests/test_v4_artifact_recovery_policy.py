"""O10：只缺本地图片时，恢复动作与文案必须指向"重下图片"而不是"重新获取在线资料"。

背景：`degraded` 分支原先不写 `reason_code`，于是恢复策略只能靠文本兜底 → `action="none"`
且文案落到 `_friendly_metadata_reason` 的兜底句"媒体信息需要处理，请检查后重试"，指向
在线资料；而 B1-2 早就写好的 `artifact_incomplete` 友好文案（"媒体资料已获取，但部分
图片下载或发布失败。"）永远走不到。更危险的是：一旦真的带上该原因码，原策略的兜底会把
它映射成 `retry_metadata` —— 那会发起一轮**完整联网抓取**，与"只重新下载缺失图片"的
零网络承诺直接冲突。
"""

from __future__ import annotations

from types import SimpleNamespace


def _policy(metadata: dict) -> dict:
    from app.media_v4.revisions.service import metadata_recovery_policy

    return metadata_recovery_policy(metadata)


def test_artifact_incomplete_offers_no_online_retry_and_uses_friendly_reason():
    policy = _policy({
        "metadata_state": "ready",
        "reason_code": "artifact_incomplete",
        "artifact_state": "degraded",
        "artifact_reasons": ["海报下载或发布失败"],
        "reason": "海报下载或发布失败",
        "completeness": ["海报下载或发布失败"],
    })

    assert policy["action"] == "none", "只缺图片时不得提供联网重试动作"
    assert "图片下载或发布失败" in policy["reason"]
    assert "需要处理" not in policy["reason"], "不得用指向在线资料的兜底文案"
    assert "重新下载" in policy["hint"]


def test_action_fallback_never_routes_the_artifact_only_case_to_online_retry():
    """有产物上下文时策略提前拦下；无上下文的遗留快照保持原行为。"""

    from app.media_v4.revisions.service import _metadata_recovery_action_fallback

    assert _metadata_recovery_action_fallback("ready", "artifact_incomplete", "") == "retry_metadata"
    # 关键点在于 metadata_recovery_policy 会先命中产物上下文分支（见上一个用例），
    # 因而这个兜底只在"历史快照、没有 degraded/completeness 证据"时才会生效。
    legacy = _policy({"metadata_state": "failed", "reason_code": "artifact_incomplete"})
    assert legacy["action"] == "retry_metadata", "没有产物上下文的历史行仍应给出可用动作"
    degraded = _policy({
        "metadata_state": "ready",
        "reason_code": "artifact_incomplete",
        "artifact_state": "degraded",
    })
    assert degraded["action"] == "none"


def test_real_degraded_run_records_the_artifact_reason_code(tmp_path, monkeypatch):
    """走真实刮削路径：仅图片缺失必须落 `artifact_state=degraded` 与专用原因码。"""

    from app.media_v4.jobs import metadata_artifacts as artifacts_module
    from app.media_v4.jobs import scrape as scrape_module
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "artifact-code.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("1080p")])
    revisions.confirm("rev-1")
    with database.connect() as conn:
        job_id = str(conn.execute(
            "SELECT job_id FROM jobs WHERE revision_id = 'rev-1' AND job_type = 'scrape_work' LIMIT 1"
        ).fetchone()["job_id"])

    monkeypatch.setattr(
        scrape_module,
        "assess_metadata_completeness",
        lambda *_args, **_kwargs: (False, ["海报下载或发布失败", "背景图下载或发布失败"]),
    )

    def fake_download(url: str, path, *, client):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image-bytes")
        return "digest-" + path.name

    monkeypatch.setattr(artifacts_module, "_download_artwork", fake_download)
    monkeypatch.setattr(
        artifacts_module,
        "load_config",
        lambda: SimpleNamespace(artwork_storage_mode="local", tmdb_timeout=5, proxy_url=""),
    )

    service = scrape_module.V4ScrapeService(database)
    service.process(job_id, lambda _target: _ready_metadata(), mirror_root=tmp_path / "mirror")

    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, metadata_json FROM scrape_bindings "
            "WHERE revision_id = 'rev-1' ORDER BY updated_at DESC, binding_id DESC LIMIT 1"
        ).fetchone()
        job = conn.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    assert job["status"] == "succeeded"
    import json

    metadata = json.loads(str(row["metadata_json"] or "{}"))
    assert metadata["metadata_state"] == "ready", "资料已就绪，只降级产物"
    assert metadata["artifact_state"] == "degraded"
    assert metadata["reason_code"] == "artifact_incomplete"

    policy = _policy(metadata)
    assert policy["action"] == "none"


def _entry(evidence_id: str):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-artifact",
        root_id="root-artifact",
        source_key=evidence_id,
        relative_path=f"Show/{evidence_id}.mkv",
        entry_kind="video",
        provider="local",
        source_locator=f"local://{evidence_id}",
        fingerprint=f"sha256:{evidence_id}",
    )
    facts = ParsedFacts(
        parsed_fact_id=f"facts-{evidence_id}",
        evidence_id=evidence_id,
        parser_version="fixture",
        work_title="Show",
        title_candidates=("Show",),
        year_candidate=2024,
        media_type="movie",
        group_type="movie",
    )
    return evidence, facts


def _ready_metadata() -> dict:
    return {
        "provider": "tmdb",
        "provider_id": "1",
        "metadata_state": "ready",
        "title": "Show",
        "year": 2024,
        "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/w500/fanart.jpg",
    }
