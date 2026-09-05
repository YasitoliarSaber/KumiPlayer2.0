"""TXT 离线导入零源盘 I/O 合同（11.19）。

TXT 只是目录结构证据：只读取用户选定的 TXT，按层级、文件名及配置根做
词法拼接；忽略清单中的 NFO，不为了识别去挂载盘检查或读取 NFO、视频、
目录。生成受控 .strm 只写推导出的播放路径，不要求此时源盘在线。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
from app.media_v4.sources.scanner import build_directory_tree_evidence


def _tree_evidence(relative: str, *, provider: str = "baidu", source_root: str = ""):
    return to_source_evidence(
        SourceEntry(
            root_id="root-txt",
            scan_id="scan-txt",
            provider=provider,
            ingest_method="directory_tree",
            relative_path=relative,
            source_key=relative,
            source_locator=f"{source_root}\\{relative}" if source_root else relative,
            playback_locator=f"{source_root}\\{relative}" if source_root else relative,
        )
    )


# ── 11.19.1 先堵住 TXT 的 NFO 读取和身份污染 ────────────────────────────────


def test_txt_nfo_never_reaches_source_disk_or_identity():
    text = "Show/Season 1/Show.S01E01.mkv\nShow/tvshow.nfo\n"
    with patch("app.media_v4.parsing.parser.Path.is_file",
               side_effect=AssertionError("TXT must not probe source files")):
        _, entries = build_directory_tree_evidence(
            text, root_id="offline-root", provider="baidu",
            source_root=r"Q:\offline-library",
        )
        assert len(entries) == 1
        assert entries[0].entry_kind == "video"
        from app.media_v4.parsing.parser import V4Parser

        assert V4Parser().parse(entries[0]).is_importable


def test_legacy_txt_metadata_evidence_does_not_read_nfo():
    """directory_tree 来源的遗留 metadata 证据不得触发 sidecar NFO 读取。"""

    from app.media_v4.parsing.parser import V4Parser

    video = _tree_evidence("Show/Season 1/Show.S01E01.mkv", source_root=r"Q:\offline-library")
    metadata = replace(
        video,
        evidence_id="ev_legacy_nfo",
        relative_path="Show/tvshow.nfo",
        source_key="Show/tvshow.nfo",
        source_locator=r"Q:\offline-library\Show\tvshow.nfo",
        playback_locator=r"Q:\offline-library\Show\tvshow.nfo",
        entry_kind="metadata",
    )
    with patch(
        "app.media_v4.parsing.parser._parse_sidecar_nfo",
        side_effect=AssertionError("TXT NFO must not be read"),
    ):
        facts = V4Parser().parse(metadata)
    assert facts.resource_type == "metadata"
    assert facts.is_importable is False
    assert facts.is_auxiliary is True
    assert facts.title_candidates == ()
    assert facts.work_title == ""
    assert facts.original_title == ""
    assert facts.tmdb_hint_id is None
    assert facts.tmdb_hint_type == ""


def test_tree_nfo_filtered_in_both_branches():
    """树形与扁平两种 TXT 形态中 NFO 都不应进入 evidence 列表。"""

    tree_text = (
        "Show/\n"
        "├── Season 1/\n"
        "│   ├── Show.S01E01.mkv\n"
        "│   └── Show.S01E02.mkv\n"
        "└── tvshow.nfo\n"
    )
    flat_text = "Show/Season 1/Show.S01E01.mkv\nShow/tvshow.nfo\n"

    for text in (tree_text, flat_text):
        _scan_id, entries = build_directory_tree_evidence(
            text, root_id="offline-root", provider="baidu",
        )
        assert entries, "至少应有一个视频条目"
        assert all(entry.entry_kind == "video" for entry in entries)
        assert all(".nfo" not in entry.relative_path.casefold() for entry in entries)


# ── 11.19.4 TXT 镜像只写路径，不检查源文件 ────────────────────────────────


def _tree_evidence_with(relative: str, locator: str):
    evidence = _tree_evidence(relative, source_root=r"Q:\offline-library")
    return replace(
        evidence,
        evidence_id="ev_" + relative.replace("/", "_").replace(" ", ""),
        relative_path=relative,
        source_key=relative,
        source_locator=locator,
        playback_locator=locator,
    )


def _confirm_tree_revision(database, revision_id: str, pairs: list):
    from app.media_v4.revisions.service import V4RevisionService

    revisions = V4RevisionService(database)
    revisions.create_draft(revision_id, pairs, source_mode="tree_snapshot")
    revisions.confirm(revision_id)
    return next(
        job for job in revisions.list_jobs(revision_id)
        if job["job_type"] == "materialize_mirror"
    )["job_id"]


def _facts_for(relative: str, *, group_type: str = "season", season: int = 1,
               episode: int | None = 1, special: int | None = None):
    from app.media_v4.domain.models import ParsedFacts

    return ParsedFacts(
        parsed_fact_id="facts_" + relative.replace("/", "_").replace(" ", ""),
        evidence_id="ev_" + relative.replace("/", "_").replace(" ", ""),
        parser_version="fixture",
        work_title="离线测试作品",
        title_candidates=("离线测试作品",),
        media_type="tv",
        group_type=group_type,
        season_candidate=season,
        episode_candidate=episode,
        special_candidate=special is not None,
        special_number=special,
        confidence="high",
    )


def test_txt_mirror_writes_locator_without_source_access(tmp_path, monkeypatch):
    """TXT 镜像：源盘离线时仍把词法定位符写入 .strm，零源盘 I/O。"""

    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "txt-mirror.db")
    database.initialize()
    offline_locator = r"Q:\offline-library\Show\Season 1\Show.S01E01.mkv"
    pairs = [
        (_tree_evidence_with("Show/Season 1/Show.S01E01.mkv", offline_locator),
         _facts_for("Show/Season 1/Show.S01E01.mkv")),
    ]
    job_id = _confirm_tree_revision(database, "rev-txt-mirror", pairs)

    # 源盘路径限定的哨兵：命中即失败。
    original_is_file = Path.is_file
    original_open = Path.open

    def guarded_is_file(self, *args, **kwargs):
        if str(self).casefold().startswith(r"Q:\offline-library".casefold()):
            raise AssertionError("TXT 镜像不得探测源盘")
        return original_is_file(self, *args, **kwargs)

    def guarded_open(self, *args, **kwargs):
        if str(self).casefold().startswith(r"Q:\offline-library".casefold()):
            raise AssertionError("TXT 镜像不得读取源盘")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    monkeypatch.setattr(Path, "open", guarded_open)

    result = V4MirrorMaterializer(database).process(job_id, tmp_path / "mirror")

    assert result.status == "succeeded"
    files = list((tmp_path / "mirror").rglob("*.strm"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == offline_locator
    with database.connect() as conn:
        artifacts = conn.execute(
            "SELECT target_path FROM artifacts WHERE revision_id = 'rev-txt-mirror' AND artifact_type = 'mirror'"
        ).fetchall()
    assert len(artifacts) == 1
    assert artifacts[0]["target_path"] == str(files[0])


def test_txt_mirror_covers_special_and_chinese_paths(tmp_path, monkeypatch):
    """特别篇与中文带空格路径同样按纯语法校验写入。"""

    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "txt-mirror-special.db")
    database.initialize()
    special_locator = r"Q:\offline-library\摇曳露营\Specials\SP01.mkv"
    pairs = [
        (_tree_evidence_with("摇曳露营/Specials/SP01.mkv", special_locator),
         _facts_for("摇曳露营/Specials/SP01.mkv", group_type="special",
                    season=0, episode=None, special=1)),
    ]
    job_id = _confirm_tree_revision(database, "rev-txt-mirror-special", pairs)

    original_is_file = Path.is_file

    def guarded_is_file(self, *args, **kwargs):
        if str(self).casefold().startswith(r"Q:\offline-library".casefold()):
            raise AssertionError("TXT 镜像不得探测源盘")
        return original_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)

    result = V4MirrorMaterializer(database).process(job_id, tmp_path / "mirror")

    assert result.status == "succeeded"
    files = list((tmp_path / "mirror").rglob("*.strm"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == special_locator


def test_txt_mirror_rejects_middle_asset_with_newline(tmp_path):
    """中间 Asset 含换行：逐条语法校验拒绝，整批失败且零 .strm。"""

    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "txt-mirror-newline.db")
    database.initialize()
    pairs = [
        (_tree_evidence_with("Show/Season 1/E01.mkv",
                             r"Q:\offline-library\Show\Season 1\E01.mkv"),
         _facts_for("Show/Season 1/E01.mkv", episode=1)),
        (_tree_evidence_with("Show/Season 1/E02.mkv",
                             "Q:\\offline-library\\Show\\Season 1\\E02.mkv\nC:\\evil.mkv"),
         _facts_for("Show/Season 1/E02.mkv", episode=2)),
    ]
    job_id = _confirm_tree_revision(database, "rev-txt-mirror-newline", pairs)

    with pytest.raises(RuntimeError, match="控制字符"):
        V4MirrorMaterializer(database).process(job_id, tmp_path / "mirror")

    assert list((tmp_path / "mirror").rglob("*.strm")) == []
    with database.connect() as conn:
        status = conn.execute(
            "SELECT status FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()["status"]
    assert status == "failed"


def test_existing_mirror_target_is_never_overwritten(tmp_path):
    """预存同名不同内容 .strm 应继续失败而不是覆盖。"""

    from app.media_v4.jobs.mirror import V4MirrorMaterializer
    from app.media_v4.persistence.database import V4Database

    database = V4Database(tmp_path / "txt-mirror-conflict.db")
    database.initialize()
    locator = r"Q:\offline-library\Show\Season 1\Show.S01E01.mkv"
    pairs = [
        (_tree_evidence_with("Show/Season 1/Show.S01E01.mkv", locator),
         _facts_for("Show/Season 1/Show.S01E01.mkv")),
    ]
    job_id = _confirm_tree_revision(database, "rev-txt-mirror-conflict", pairs)
    mirror_root = tmp_path / "mirror"
    materializer = V4MirrorMaterializer(database)
    # 先成功发布一次拿到真实目标路径，再篡改内容，确认不会被覆盖。
    first = materializer.process(job_id, mirror_root)
    assert first.status == "succeeded"
    existing = Path(first.artifact_paths[0])
    existing.write_text("different-content", encoding="utf-8")
    with database.connect() as conn:
        conn.execute("DELETE FROM artifacts WHERE revision_id = ?", ("rev-txt-mirror-conflict",))
        conn.execute(
            "UPDATE jobs SET status = 'queued', finished_at = '' WHERE job_id = ?",
            (job_id,),
        )

    with pytest.raises(RuntimeError, match="不一致"):
        materializer.process(job_id, mirror_root)

    assert existing.read_text(encoding="utf-8") == "different-content"


# ── 11.19.6 端到端：扫描 → 识别 → 确认 → 镜像 ─────────────────────────────


def test_txt_offline_scan_preview_confirm_mirror(tmp_path, monkeypatch):
    """TXT 全链路离线：durable 扫描 + 后台 finalize + 确认 + 镜像全部成功，
    全程零源盘 I/O（Q盘哨兵），NFO 不产生任何条目。"""

    import time
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import media_v4
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.sources.durable_scan import get_durable_scan

    database = V4Database(tmp_path / "txt-e2e.db")
    database.initialize()
    monkeypatch.setattr(media_v4, "_database", database)

    # 源盘根（虚构挂载点）——清单按词法推导 Q:\ 下路径，但绝不允许访问。
    source_root = r"Q:\百度网盘"
    tree = tmp_path / "01动画_文件目录.txt"
    tree.write_text(
        "Show/Season 1/Show.S01E01.mkv\n"
        "Show/Season 1/Show.S01E02.mkv\n"
        "Show/tvshow.nfo\n"
        "Show/OP1.mkv\n"
        "Show/Specials/Show.SP01.mkv\n"
        "Movie/Movie.mkv\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(media_v4, "load_config", lambda: SimpleNamespace(
        pan115_root="",
        baidu_root=source_root,
        openlist_mount_root="",
        openlist_remote_root="/",
        openlist_routes=[],
    ))

    # 源盘访问哨兵：任何 Q:\ 下的 stat/open/枚举都会失败。
    import os

    original_is_file = Path.is_file
    original_is_dir = Path.is_dir
    original_exists = Path.exists
    original_open = Path.open
    original_os_exists = os.path.exists
    original_os_isdir = os.path.isdir
    original_os_isfile = os.path.isfile

    def _on_source_disk(path) -> bool:
        return str(path).casefold().startswith(source_root.casefold())

    def guarded_is_file(self, *args, **kwargs):
        assert not _on_source_disk(self), f"TXT 链路不得探测源盘: {self}"
        return original_is_file(self, *args, **kwargs)

    def guarded_is_dir(self, *args, **kwargs):
        assert not _on_source_disk(self), f"TXT 链路不得探测源盘目录: {self}"
        return original_is_dir(self, *args, **kwargs)

    def guarded_exists(self, *args, **kwargs):
        assert not _on_source_disk(self), f"TXT 链路不得探测源盘存在性: {self}"
        return original_exists(self, *args, **kwargs)

    def guarded_open(self, *args, **kwargs):
        assert not _on_source_disk(args[0] if args else ""), f"TXT 链路不得打开源盘文件: {args[0] if args else ''}"
        return original_open(self, *args, **kwargs)

    def guarded_os_exists(path):
        assert not _on_source_disk(path), f"TXT 链路不得探测源盘存在性(os): {path}"
        return original_os_exists(path)

    def guarded_os_isdir(path):
        assert not _on_source_disk(path), f"TXT 链路不得探测源盘目录(os): {path}"
        return original_os_isdir(path)

    def guarded_os_isfile(path):
        assert not _on_source_disk(path), f"TXT 链路不得探测源盘文件(os): {path}"
        return original_os_isfile(path)

    monkeypatch.setattr(Path, "is_file", guarded_is_file)
    monkeypatch.setattr(Path, "is_dir", guarded_is_dir)
    monkeypatch.setattr(Path, "exists", guarded_exists)
    monkeypatch.setattr(Path, "open", guarded_open)
    monkeypatch.setattr(os.path, "exists", guarded_os_exists)
    monkeypatch.setattr(os.path, "isdir", guarded_os_isdir)
    monkeypatch.setattr(os.path, "isfile", guarded_os_isfile)

    application = FastAPI()
    application.include_router(media_v4.router)
    client = TestClient(application)

    # 1) durable 扫描：立即返回任务身份，后台线程读取 TXT、构建证据、finalize 草稿。
    scan = client.post("/api/v4/sources/scans", json={
        "source": "tree",
        "tree_file": str(tree),
        "provider": "baidu",
        "revision_id": "rev-txt-e2e",
    })
    assert scan.status_code == 200, scan.text
    scan_id = scan.json()["scan_id"]
    root_id = scan.json()["root_id"]
    assert scan.json()["status"] == "running"

    # 2) 等待后台 finalize 完成（真实线程，必须等终态再断言）。
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = get_durable_scan(database, scan_id, include_entries=False)
        if state["status"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    state = get_durable_scan(database, scan_id, include_entries=False)
    assert state["status"] == "completed", state
    # NFO 不应产生 evidence；视频 5 条（2 正片 + 1 特别篇 + 1 OP + 1 电影）。
    assert state["evidence_count"] == 5
    assert state["total_count"] == 5

    # 3) preview：消费后端持久化草稿，不接受前端回传 locator。
    preview = client.post("/api/v4/imports/preview", json={
        "revision_id": "rev-txt-e2e",
        "root_id": root_id,
        "scan_id": scan_id,
        "entries": [],
    })
    assert preview.status_code == 200, preview.text
    preview_body = preview.json()
    assert preview_body["status"] == "draft"
    assert preview_body["works"], preview_body
    # 词法拼接保留 01动画 范围：校验持久化验证记录中的有效播放根。
    from app.media_v4.sources.scan_validation import load_tree_scan_validation

    validation = load_tree_scan_validation(database, scan_id)
    assert validation is not None
    assert validation["ok"] is True
    assert validation["hits"] == 0 and validation["total"] == 0
    assert validation["effective_root"].casefold().startswith(source_root.casefold())
    assert "01动画" in validation["effective_root"]

    # 4) confirm：不探测源盘，直接成功并产生镜像任务。
    confirmed = client.post("/api/v4/imports/rev-txt-e2e/confirm")
    assert confirmed.status_code == 200, confirmed.text
    jobs = confirmed.json()["jobs"]
    mirror_jobs = [job for job in jobs if job["job_type"] == "materialize_mirror"]
    assert mirror_jobs, jobs
    assert all(job["status"] in {"queued", "succeeded"} for job in mirror_jobs)

    # 5) 镜像：TXT 资产只写词法路径，不检查源文件存在性。
    from app.media_v4.jobs.mirror import V4MirrorMaterializer

    materializer = V4MirrorMaterializer(database)
    mirror_root = tmp_path / "mirror"
    for job in mirror_jobs:
        result = materializer.process(job["job_id"], mirror_root)
        assert result.status == "succeeded", result

    strm_files = sorted((mirror_root).rglob("*.strm"))
    # OP/ED 等辅助视频保留证据但不得生成普通剧集镜像：正片 2 + 特别篇 1 + 电影 1。
    assert len(strm_files) == 4
    contents = {path.read_text(encoding="utf-8") for path in strm_files}
    assert contents == {
        rf"{source_root}\01动画\Show\Season 1\Show.S01E01.mkv",
        rf"{source_root}\01动画\Show\Season 1\Show.S01E02.mkv",
        rf"{source_root}\01动画\Show\Specials\Show.SP01.mkv",
        rf"{source_root}\01动画\Movie\Movie.mkv",
    }
    with database.connect() as conn:
        artifacts = conn.execute(
            "SELECT COUNT(*) FROM artifacts WHERE revision_id = 'rev-txt-e2e' AND artifact_type = 'mirror'"
        ).fetchone()[0]
    assert artifacts == 4
