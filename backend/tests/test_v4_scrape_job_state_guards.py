"""O8：刮削 job 的状态机必须有并发守卫，且下载阶段要持续心跳。

三个真实风险：
1. `requeue_work` 先 SELECT 判 running 再 UPDATE，是读后写；并发两次重试时后到的会把
   已被执行器领取的 running 改回 queued → 同一个 work 被两个执行器同时跑。
2. 图片下载可能持续数分钟（24 集 ≈ 27 张图）。期间没有心跳就会被"失联回收"判 failed，
   而收尾的 `UPDATE ... status='succeeded'` 原先没有 running 守卫，会把终态又改回去，
   状态与真实执行结果自相矛盾。
3. 下载阶段本身没有任何心跳上报。
"""

from __future__ import annotations

from types import SimpleNamespace


def _entry(evidence_id: str):
    from app.media_v4.domain.models import ParsedFacts, SourceEvidence

    evidence = SourceEvidence(
        evidence_id=evidence_id,
        scan_id="scan-guard",
        root_id="root-guard",
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


def _confirmed_job(tmp_path):
    from app.media_v4.jobs.scrape import V4ScrapeService
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.revisions.service import V4RevisionService

    database = V4Database(tmp_path / "guards.db")
    database.initialize()
    revisions = V4RevisionService(database)
    revisions.create_draft("rev-1", [_entry("1080p")])
    revisions.confirm("rev-1")
    with database.connect() as conn:
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE revision_id = 'rev-1' AND job_type = 'scrape_work' LIMIT 1"
        ).fetchone()
    assert row is not None, "确认后应当已排出 scrape_work 任务"
    return database, V4ScrapeService(database), str(row["job_id"])


def _job_status(database, job_id: str) -> tuple[str, str]:
    with database.connect() as conn:
        row = conn.execute(
            "SELECT status, last_error FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return str(row["status"]), str(row["last_error"])


def test_requeue_refuses_to_overwrite_a_concurrently_claimed_job(tmp_path, monkeypatch):
    """竞态确定性复现：UPDATE 之前任务被另一执行器领取 → 必须拒绝而不是覆盖。"""

    from app.media_v4.persistence.database import V4Database

    database, service, job_id = _confirmed_job(tmp_path)
    real_open = V4Database.open_connection
    state = {"armed": True}

    class _RacingConnection:
        def __init__(self, real):
            self._real = real

        def execute(self, sql, *args, **kwargs):
            if state["armed"] and "SET status = 'queued'" in str(sql):
                state["armed"] = False
                # 模拟并发执行器在 SELECT 之后、UPDATE 之前领取了该任务。
                with real_open(database) as other:
                    other.execute(
                        "UPDATE jobs SET status = 'running', started_at = 'now' WHERE job_id = ?",
                        (job_id,),
                    )
            return self._real.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(V4Database, "open_connection", lambda self: _RacingConnection(real_open(self)))

    try:
        service.requeue_work("rev-1", _work_id(database))
    except RuntimeError as exc:
        assert "正在获取媒体信息" in str(exc)
    else:
        raise AssertionError("并发领取后 requeue 必须拒绝，而不是把 running 改回 queued")

    monkeypatch.undo()
    assert _job_status(database, job_id)[0] == "running", "执行器的 running 状态不得被重试覆盖"


def _work_id(database) -> str:
    with database.connect() as conn:
        return str(conn.execute(
            "SELECT work_id FROM revision_bindings WHERE revision_id = 'rev-1' LIMIT 1"
        ).fetchone()["work_id"])


def _ready_metadata() -> dict:
    return {
        "provider": "tmdb",
        "provider_id": "1",
        "metadata_state": "ready",
        "title": "Show",
        "year": 2024,
        "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
        "fanart_url": "https://image.tmdb.org/t/p/w500/fanart.jpg",
        "clearlogo_url": "https://image.tmdb.org/t/p/w500/logo.png",
    }


def _patch_artwork(monkeypatch):
    from app.media_v4.jobs import metadata_artifacts as module

    downloads: list[str] = []

    def fake_download(url: str, path, *, client):
        downloads.append(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image-bytes")
        return "digest-" + path.name

    monkeypatch.setattr(module, "_download_artwork", fake_download)
    monkeypatch.setattr(
        module,
        "load_config",
        lambda: SimpleNamespace(artwork_storage_mode="local", tmdb_timeout=5, proxy_url=""),
    )
    return downloads


def test_process_does_not_overwrite_a_terminal_state_written_during_the_run(tmp_path, monkeypatch):
    """运行期间被"失联回收"判 failed 时，收尾不得把它改回 succeeded。"""

    from app.media_v4.persistence.database import V4Database

    database, service, job_id = _confirmed_job(tmp_path)
    _patch_artwork(monkeypatch)
    real_open = V4Database.open_connection

    def provider(_target):
        # 模拟图片下载耗时期间失联回收把任务判为 failed。
        with real_open(database) as other:
            other.execute(
                "UPDATE jobs SET status = 'failed', last_error = '任务异常中断，可重试' WHERE job_id = ?",
                (job_id,),
            )
        return _ready_metadata()

    service.process(job_id, provider, mirror_root=tmp_path / "mirror")

    status, last_error = _job_status(database, job_id)
    assert status == "failed", "收尾写入必须有 running 守卫，不能把回收判断覆盖成 succeeded"
    assert last_error == "任务异常中断，可重试"


def test_process_heartbeats_while_downloading_artwork(tmp_path, monkeypatch):
    database, service, job_id = _confirmed_job(tmp_path)
    downloads = _patch_artwork(monkeypatch)
    seen: dict[str, str] = {}

    def provider(_target):
        with database.connect() as conn:
            seen["before"] = str(conn.execute(
                "SELECT heartbeat_at FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()["heartbeat_at"])
        return _ready_metadata()

    service.process(job_id, provider, mirror_root=tmp_path / "mirror")

    assert downloads, "前置条件：确实发生了图片下载"
    status, _error = _job_status(database, job_id)
    with database.connect() as conn:
        after = str(conn.execute(
            "SELECT heartbeat_at FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()["heartbeat_at"])

    assert status == "succeeded"
    assert after > seen["before"], f"下载阶段必须推进心跳（{seen['before']} -> {after}）"


def test_publish_reports_progress_for_every_download(tmp_path, monkeypatch):
    from app.media_v4.jobs import metadata_artifacts as module

    downloads = _patch_artwork(monkeypatch)
    ticks: list[int] = []
    database, _service, _job_id = _confirmed_job(tmp_path)

    module.publish_metadata_artifacts(
        database,
        revision_id="rev-1",
        work_id=_work_id(database),
        target={"work_type": "movie", "title": "Show", "year": 2024, "episodes": []},
        metadata=_ready_metadata(),
        mirror_root=tmp_path / "mirror",
        on_progress=lambda: ticks.append(1),
    )

    assert len(downloads) == 3
    assert len(ticks) == 3, "每张图下载前都应上报一次心跳"
