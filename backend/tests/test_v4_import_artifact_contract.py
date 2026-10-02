"""CHECK-006A/B/C：镜像与元数据产物的稳定命名、未知消费与引用保护。

全部断言走真实入口：目录树扫描 → ``V4Parser`` → ``V4RevisionService`` 确认 →
``V4JobRunner``（镜像 + 刮削）→ 真实临时镜像根与 SQLite。TXT/目录树来源只做
词法定位符校验，绝不探测源盘。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from app.media_v4.jobs.artifact_paths import asset_label, mirror_relative_path
from app.media_v4.jobs.runner import V4JobRunner
from app.media_v4.parsing.parser import V4Parser, normalize_batch_parsed_facts
from app.media_v4.persistence.database import V4Database
from app.media_v4.revisions.service import V4RevisionService
from app.media_v4.sources.scanner import build_directory_tree_evidence

PARSER = V4Parser()
SOURCE_ROOT = r"Z:\library"


def _database(tmp_path, name: str) -> V4Database:
    database = V4Database(tmp_path / name)
    database.initialize()
    return database


def _tree_entries(text: str, *, root_id: str, scan_id: str, source_root: str = SOURCE_ROOT):
    _scan, evidence = build_directory_tree_evidence(
        text, root_id=root_id, scan_id=scan_id, provider="baidu", source_root=source_root
    )
    return normalize_batch_parsed_facts(
        [(item, PARSER.parse(item, root_container="动画")) for item in evidence]
    )


def _confirm(database, revision_id: str, entries) -> V4RevisionService:
    service = V4RevisionService(database)
    service.create_draft(revision_id, entries)
    service.confirm(revision_id)
    return service


def _ready_metadata(target: dict) -> dict:
    """Provider stub：身份与作品资料都成功，不触发任何网络或源盘访问。"""

    return {
        "provider": "tmdb",
        "provider_id": "42",
        "media_type": "tv",
        "title": f"{target.get('preferred_title')} (online)",
        "plot": "stub",
        "episode_mappings": [],
        "metadata_state": "ready",
        "identity_status": "confirmed",
        "work_metadata_status": "ready",
    }


def _runner(database) -> V4JobRunner:
    return V4JobRunner(database, metadata_provider=_ready_metadata)


def _mirror_files(mirror_root) -> list:
    return sorted(path for path in mirror_root.rglob("*") if path.is_file())


def _strm_files(mirror_root) -> list:
    return [path for path in _mirror_files(mirror_root) if path.suffix == ".strm"]


def _relative_mirror_paths(mirror_root) -> set[str]:
    return {
        path.relative_to(mirror_root).as_posix() for path in _mirror_files(mirror_root)
    }


def _asset_rows(database, revision_id: str):
    with database.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT rb.asset_id, se.playback_locator
                FROM revision_bindings rb
                JOIN source_evidence se ON se.evidence_id = rb.evidence_id
                WHERE rb.revision_id = ?
                """,
                (revision_id,),
            ).fetchall()
        ]


# --- CHECK-006A：稳定命名、完整标签与重复执行 --------------------------------


def test_two_versions_of_one_episode_get_distinct_full_labels(tmp_path):
    """CHECK-006A: 两个版本各自一个 .strm，文件名带完整资产标签而不是指纹尾串。"""

    database = _database(tmp_path, "artifact-labels.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "\n".join(
            [
                "Show/Season 1/Show.S01E01.1080p.mkv",
                "Show/Season 1/Show.S01E01.2160p.mkv",
            ]
        ),
        root_id="root-l",
        scan_id="scan-l",
    )
    _confirm(database, "rev-l", entries)
    _runner(database).process_available(mirror_root=mirror_root)

    files = _strm_files(mirror_root)
    assert len(files) == 2
    locators = {path.read_text(encoding="utf-8") for path in files}
    assert len(locators) == 2

    labels = {path.name.split("-", 1)[1].removesuffix(".strm") for path in files}
    assert len(labels) == 2
    for label in labels:
        assert len(label) == 64
        assert all(char in "0123456789abcdef" for char in label), label

    expected = {
        asset_label(row["asset_id"], row["playback_locator"]) for row in _asset_rows(database, "rev-l")
    }
    assert labels == expected
    assert all(path.name.startswith("S01E01-") for path in files)
    assert all(path.parent.name == "Season 01" for path in files)


def test_repeated_mirror_run_keeps_bytes_and_artifacts(tmp_path):
    """CHECK-006A: 重复执行同一任务文件字节不变、产物行不变。"""

    database = _database(tmp_path, "artifact-repeat.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "Show/Season 1/Show.S01E01.mkv", root_id="root-rep", scan_id="scan-rep"
    )
    _confirm(database, "rev-rep", entries)
    _runner(database).process_available(mirror_root=mirror_root)

    before = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in _mirror_files(mirror_root)
    }
    with database.connect() as conn:
        artifacts_before = [
            dict(row)
            for row in conn.execute(
                "SELECT artifact_id, artifact_type, target_path, digest, status FROM artifacts ORDER BY target_path"
            ).fetchall()
        ]
    assert before
    assert artifacts_before

    _runner(database).process_available(mirror_root=mirror_root)

    after = {
        path: (path.read_bytes(), path.stat().st_mtime_ns) for path in _mirror_files(mirror_root)
    }
    assert after == before
    with database.connect() as conn:
        artifacts_after = [
            dict(row)
            for row in conn.execute(
                "SELECT artifact_id, artifact_type, target_path, digest, status FROM artifacts ORDER BY target_path"
            ).fetchall()
        ]
    assert artifacts_after == artifacts_before


def test_rescan_keeps_same_target_path(tmp_path):
    """CHECK-006A: 同来源再次确认时目标路径与内容保持稳定。"""

    database = _database(tmp_path, "artifact-stable.db")
    mirror_root = tmp_path / "mirror"
    text = "Show/Season 1/Show.S01E01.mkv"
    first = _tree_entries(text, root_id="root-st", scan_id="scan-st1")
    _confirm(database, "rev-st1", first)
    _runner(database).process_available(mirror_root=mirror_root)
    first_paths = _relative_mirror_paths(mirror_root)
    first_bytes = {path: path.read_bytes() for path in _mirror_files(mirror_root)}

    # 同一扫描再次确认到新 revision：观察与 Asset 连续，目标名必须不变。
    second = _tree_entries(text, root_id="root-st", scan_id="scan-st2")
    _confirm(database, "rev-st2", second)
    _runner(database).process_available(mirror_root=mirror_root)

    assert {p for p in _relative_mirror_paths(mirror_root) if p.endswith('.strm')} == {p for p in first_paths if p.endswith('.strm')}
    assert all(path.read_bytes() == data for path, data in first_bytes.items())


# --- CHECK-006B：未知/绝对/中文与特别篇不落 Specials/S00E00 ------------------


def test_unknown_and_absolute_targets_avoid_specials_and_s00e00(tmp_path):
    """CHECK-006B: 未知季/未分季集/绝对编号的镜像与 NFO 都不含 Specials/S00E00。"""

    database = _database(tmp_path, "artifact-unknown.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "\n".join(
            [
                "Show/Season 1/Show.S01E01.mkv",
                "Show/Show ABS13.mkv",
                "Show/Show 第01集.mkv",
            ]
        ),
        root_id="root-u",
        scan_id="scan-u",
    )
    _confirm(database, "rev-u", entries)
    _runner(database).process_available(mirror_root=mirror_root)

    paths = _relative_mirror_paths(mirror_root)
    assert paths
    for path in paths:
        assert "Specials" not in path, path
        assert "S00E" not in path, path

    strm = {path for path in paths if path.endswith(".strm")}
    nfo = {path for path in paths if path.endswith(".nfo")}
    assert len(strm) == 3, paths
    assert any("/Unassigned/ABS0013-" in path for path in strm), strm
    assert any("/Unassigned/E01-" in path for path in strm), strm
    assert any("/Season 01/S01E01-" in path for path in strm), strm
    # stub 没有逐集映射，完整性门控拒绝发布本次 NFO；镜像仍保留可播放结构。
    assert not nfo, nfo


def test_explicit_special_episode_has_no_artifact(tmp_path):
    """CHECK-006B: 明确特别篇没有 binding，因此既无镜像也无 NFO。"""

    database = _database(tmp_path, "artifact-special.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "\n".join(["Show/Season 1/Show.S01E01.mkv", "Show/Specials/Show.S00E03.mkv"]),
        root_id="root-sp",
        scan_id="scan-sp",
    )
    special_ids = {
        evidence.evidence_id
        for evidence, facts in entries
        if facts.content_class == "attached_special"
    }
    assert special_ids
    _confirm(database, "rev-sp", entries)
    _runner(database).process_available(mirror_root=mirror_root)

    paths = _relative_mirror_paths(mirror_root)
    assert paths
    for path in paths:
        assert "Specials" not in path
        assert "SP01" not in path
        assert "S00E" not in path
    with database.connect() as conn:
        bound = {
            str(row["evidence_id"])
            for row in conn.execute(
                "SELECT evidence_id FROM revision_bindings WHERE revision_id = 'rev-sp'"
            ).fetchall()
        }
    assert bound.isdisjoint(special_ids)


def test_unc_locator_bytes_are_preserved(tmp_path):
    """CHECK-006B: UNC 双反斜杠内容原样写入 .strm。"""

    database = _database(tmp_path, "artifact-unc.db")
    mirror_root = tmp_path / "mirror"
    unc = "\\\\server\\share\\Show\\S01E01.mkv"
    entries = _tree_entries(
        "Show/Season 1/Show.S01E01.mkv", root_id="root-unc", scan_id="scan-unc"
    )
    entries = [
        (
            replace(evidence, source_locator=unc, playback_locator=unc),
            facts,
        )
        for evidence, facts in entries
    ]
    _confirm(database, "rev-unc", entries)
    _runner(database).process_available(mirror_root=mirror_root)

    files = _mirror_files(mirror_root)
    strm = [path for path in files if path.suffix == ".strm"]
    assert len(strm) == 1
    assert strm[0].read_text(encoding="utf-8") == unc
    assert strm[0].read_text(encoding="utf-8").startswith("\\\\server\\share\\")


def test_chinese_path_with_spaces_is_written_by_syntax_only(tmp_path):
    """CHECK-006B: 中文带空格路径按纯语法校验写入，不访问源盘。"""

    database = _database(tmp_path, "artifact-chinese.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "摇曳露营/Season 1/摇曳露营 S01E01.mkv", root_id="root-cn", scan_id="scan-cn"
    )
    _confirm(database, "rev-cn", entries)

    import pathlib

    def guarded_is_file(self, *args, **kwargs):
        text = str(self)
        if text.startswith("Z:"):
            raise AssertionError("TXT/目录树来源的镜像不得探测源盘")
        return original_is_file(self, *args, **kwargs)

    original_is_file = pathlib.Path.is_file
    monkeypatched = pytest.MonkeyPatch()
    monkeypatched.setattr(pathlib.Path, "is_file", guarded_is_file)
    try:
        _runner(database).process_available(mirror_root=mirror_root)
    finally:
        monkeypatched.undo()

    strm = [path for path in _mirror_files(mirror_root) if path.suffix == ".strm"]
    assert len(strm) == 1
    assert strm[0].read_text(encoding="utf-8") == rf"{SOURCE_ROOT}\摇曳露营\Season 1\摇曳露营 S01E01.mkv"
    assert "Specials" not in strm[0].as_posix()


# --- CHECK-006C：冲突、取消与引用对象 ----------------------------------------


def test_conflicting_target_fails_before_writing_any_file(tmp_path):
    """CHECK-006C: 预存同名不同内容的目标时整批失败，不先写半批。"""

    database = _database(tmp_path, "artifact-conflict.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "\n".join(
            [
                "Show/Season 1/Show.S01E01.mkv",
                "Show/Season 1/Show.S01E02.mkv",
            ]
        ),
        root_id="root-cf",
        scan_id="scan-cf",
    )
    _confirm(database, "rev-cf", entries)

    rows = _asset_rows(database, "rev-cf")
    assert len(rows) == 2
    second = sorted(rows, key=lambda row: row["playback_locator"])[-1]
    with database.connect() as conn:
        work_id = str(
            conn.execute(
                "SELECT work_id FROM revision_bindings WHERE revision_id = 'rev-cf' LIMIT 1"
            ).fetchone()["work_id"]
        )
    target_rel = mirror_relative_path(
        work_id=work_id,
        asset_id=second["asset_id"],
        playback_locator=second["playback_locator"],
        season_kind="regular",
        local_season_number=1,
        local_episode_number=2,
    )
    target = mirror_root.joinpath(*target_rel.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("pre-existing-different-content", encoding="utf-8")

    with pytest.raises(RuntimeError, match="不一致"):
        _runner(database).process_available(mirror_root=mirror_root)

    assert target.read_text(encoding="utf-8") == "pre-existing-different-content"
    assert [path for path in _mirror_files(mirror_root) if path.suffix == ".strm"] == [target]


def test_cancelled_mirror_job_writes_nothing(tmp_path):
    """CHECK-006C: 取消请求在写盘前生效时不产生任何镜像文件。"""

    database = _database(tmp_path, "artifact-cancel.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "Show/Season 1/Show.S01E01.mkv", root_id="root-cancel", scan_id="scan-cancel"
    )
    _confirm(database, "rev-cancel", entries)
    with database.connect() as conn:
        conn.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE revision_id = 'rev-cancel' AND job_type = 'materialize_mirror'"
        )

    results = _runner(database).process_available(mirror_root=mirror_root)

    assert results
    with database.connect() as conn:
        status = conn.execute(
            "SELECT status FROM jobs WHERE revision_id = 'rev-cancel' AND job_type = 'materialize_mirror'"
        ).fetchone()["status"]
    assert status == "cancelled"
    assert _mirror_files(mirror_root) == []


def test_multi_episode_file_shares_one_asset_reference(tmp_path):
    """CHECK-006C: E01-E02 合集文件的两个 Episode 引用同一 Asset 与同一内容。"""

    database = _database(tmp_path, "artifact-range.db")
    mirror_root = tmp_path / "mirror"
    entries = _tree_entries(
        "Show/Season 1/Show.S01E01-E02.mkv", root_id="root-rg", scan_id="scan-rg"
    )
    _confirm(database, "rev-rg", entries)
    _runner(database).process_available(mirror_root=mirror_root)

    with database.connect() as conn:
        rows = conn.execute(
            """
            SELECT rb.work_id, rb.asset_id, rb.episode_id, se.playback_locator
            FROM revision_bindings rb
            JOIN source_evidence se ON se.evidence_id = rb.evidence_id
            WHERE rb.revision_id = 'rev-rg'
            """
        ).fetchall()
        mirror_refs = conn.execute(
            """
            SELECT a.target_path, a.digest, ar.subject_id FROM artifact_references ar
            JOIN artifacts a ON a.artifact_id = ar.artifact_id
            WHERE ar.revision_id = 'rev-rg' AND ar.role = 'mirror'
            """
        ).fetchall()
    assert len(rows) == 2
    assert len({str(row["asset_id"]) for row in rows}) == 1
    assert len({str(row["episode_id"]) for row in rows}) == 2
    asset_id = str(rows[0]["asset_id"])
    locator = str(rows[0]["playback_locator"])

    # 多集同 Asset 可输出各逻辑 Episode 镜像，但内容与引用都指向同一 Asset。
    strm = _strm_files(mirror_root)
    assert len(strm) == 2
    assert {path.read_text(encoding="utf-8") for path in strm} == {locator}
    assert len({path.name.split("-", 1)[0] for path in strm}) == 2
    assert {str(row["subject_id"]) for row in mirror_refs} == {asset_id}
    assert {str(row["target_path"]) for row in mirror_refs} == {str(path) for path in strm}
    digest = hashlib.sha256(locator.encode("utf-8")).hexdigest()
    assert {str(row["digest"]) for row in mirror_refs} == {digest}
