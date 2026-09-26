"""CHECK-002：来源路径保真与文件身份连续性。

只使用真实树解析入口、隔离临时数据库与纯词法函数；不访问源盘、不读挂载盘、
不算内容哈希、不调用 ffprobe，也不启动 mpv。
"""

from __future__ import annotations

import sqlite3

import pytest
from app.media_v4.domain.identity import (
    CONTINUITY_NEW,
    CONTINUITY_RENAMED,
    CONTINUITY_REPLACED,
    CONTINUITY_UNCERTAIN,
    CONTINUITY_UNCHANGED,
)
from app.media_v4.persistence.database import V4Database
from app.media_v4.persistence.source_identity import SourceIdentityRepository
from app.media_v4.sources.file_identity import (
    LocatorError,
    ObservedFile,
    canonical_source_locator,
    decide_source_identity,
    locator_comparison_key,
    namespace_key,
)
from app.media_v4.sources.scanner import (
    DirectoryTreeReadError,
    build_directory_tree_evidence,
    export_root_scope,
)

UNC = "\\\\" + "server" + "\\" + "share"


# --- CHECK-002A：路径词法与来源格式无关 --------------------------------------


def test_canonical_locator_preserves_legal_duplicates_and_unc():
    """CHECK-002A: 合法相邻同名层与 UNC 双斜线必须字节保真。"""

    assert canonical_source_locator("C:\\media\\作品甲\\作品甲\\ep01.mkv") == (
        "C:\\media\\作品甲\\作品甲\\ep01.mkv"
    )
    assert canonical_source_locator(UNC + "\\作品甲\\ep01.mkv") == UNC + "\\作品甲\\ep01.mkv"
    assert canonical_source_locator("//server/share/Show/Show.S01E01.mkv") == (
        "//server/share/Show/Show.S01E01.mkv"
    )
    # `.` 词法消去，显示大小写保留。
    assert canonical_source_locator("C:/media/./Show/ep01.mkv") == "C:/media/Show/ep01.mkv"


def test_canonical_locator_rejects_escaping_parent_segments():
    """CHECK-002A: `..` 逃出所选根必须拒绝，不能靠 normpath 掩盖。"""

    with pytest.raises(LocatorError) as excinfo:
        canonical_source_locator("作品甲/../../ep01.mkv")
    assert excinfo.value.code == "escapes_root"
    with pytest.raises(LocatorError):
        canonical_source_locator(UNC + "\\..\\x.mkv")
    # 根内部的回退是合法的。
    assert canonical_source_locator("C:\\media\\Show\\..\\ep01.mkv") == "C:\\media\\ep01.mkv"


def test_windows_namespace_casefolds_only_for_comparison():
    """CHECK-002A: Windows 比较键统一大小写，显示与实际 locator 保留原样。"""

    display = "C:\\Media\\Show\\Show.S01E01.mkv"
    assert locator_comparison_key(display, namespace_kind_name="local") == locator_comparison_key(
        "c:\\media\\show\\show.s01e01.mkv", namespace_kind_name="local"
    )
    # 远端 POSIX 不做通用 casefold。
    assert locator_comparison_key("/Anime/Show/a.mkv", namespace_kind_name="remote") != (
        locator_comparison_key("/anime/show/a.mkv", namespace_kind_name="remote")
    )


def test_export_header_trimmed_once_and_real_folder_preserved(tmp_path):
    """CHECK-002A: 合成导出根裁一层；真实顶层目录与嵌套所选根原样保留。"""

    shell_tree = "|——根目录\n| |-动画\n| | |-Show\n| | | |-Show.S01E01.mkv\n"
    _scan, evidence = build_directory_tree_evidence(
        shell_tree, root_id="root-shell", provider="pan115", scan_id="scan-shell"
    )
    assert [item.relative_path for item in evidence] == ["动画/Show/Show.S01E01.mkv"]

    real_tree = "|——动画\n| |-Show\n| | |-Show.S01E01.mkv\n"
    _scan, evidence = build_directory_tree_evidence(
        real_tree, root_id="root-real", provider="pan115", scan_id="scan-real"
    )
    assert [item.relative_path for item in evidence] == ["动画/Show/Show.S01E01.mkv"]

    # 嵌套所选根：与所选根同名的段是真实目录，绝不按 basename 裁掉。
    _scan, nested = build_directory_tree_evidence(
        real_tree,
        root_id="root-nested",
        provider="pan115",
        scan_id="scan-nested",
        source_root="K:\\115网盘\\动画",
    )
    assert nested[0].source_locator == "K:\\115网盘\\动画\\动画\\Show\\Show.S01E01.mkv"


def test_absolute_export_root_must_prefix_selected_root():
    """CHECK-002A: 绝对导出根是所选根的逐段相等前缀才裁；否则扫描失败。"""

    text = "├── K:\\115网盘\\动画\n│   ├── Show\n│   │   ├── Show.S01E01.mkv\n"
    _scan, evidence = build_directory_tree_evidence(
        text,
        root_id="root-abs",
        provider="baidu",
        scan_id="scan-abs",
        source_root="K:\\115网盘\\动画",
    )
    assert [item.relative_path for item in evidence] == ["Show/Show.S01E01.mkv"]
    assert evidence[0].source_locator == "K:\\115网盘\\动画\\Show\\Show.S01E01.mkv"
    assert export_root_scope(text) == "K:\\115网盘\\动画"

    with pytest.raises(DirectoryTreeReadError) as excinfo:
        build_directory_tree_evidence(
            text,
            root_id="root-abs",
            provider="baidu",
            scan_id="scan-abs-2",
            source_root="D:\\其他盘",
        )
    assert excinfo.value.kind == "source_root_mismatch"


def test_metadata_node_keeps_following_video_hierarchy():
    """CHECK-002A: NFO 节点被忽略但不破坏后续视频的父子层级。"""

    text = (
        "├── 动画\n"
        "│   ├── 作品甲\n"
        "│   │   ├── tvshow.nfo\n"
        "│   │   ├── Season 1\n"
        "│   │   │   ├── 作品甲.S01E01.mkv\n"
    )
    _scan, evidence = build_directory_tree_evidence(
        text, root_id="root-nfo", provider="baidu", scan_id="scan-nfo"
    )
    assert [item.relative_path for item in evidence] == ["动画/作品甲/Season 1/作品甲.S01E01.mkv"]
    assert all(item.entry_kind == "video" for item in evidence)


@pytest.mark.parametrize("provider", ["local", "pan115", "baidu", "openlist"])
def test_provider_only_changes_format_not_semantic_relative_path(provider):
    """CHECK-002A: 四个来源标准化后相对路径与有效提示保持一致。"""

    if provider == "pan115":
        text = "|——动画\n| |-Show\n| | |-Show.S01E01.mkv\n"
    else:
        text = "├── 动画\n│   ├── Show\n│   │   ├── Show.S01E01.mkv\n"
    _scan, evidence = build_directory_tree_evidence(
        text, root_id="root-p", provider=provider, scan_id=f"scan-{provider}"
    )
    assert [item.relative_path for item in evidence] == ["动画/Show/Show.S01E01.mkv"]


# --- CHECK-002B：连续性决策与槽位登记 ----------------------------------------


def _observed(evidence_id, locator, **kwargs) -> ObservedFile:
    return ObservedFile(
        evidence_id=evidence_id,
        source_key=locator,
        locator=locator,
        identity_namespace="root:1",
        namespace_kind_name=kwargs.pop("namespace_kind_name", "local"),
        **kwargs,
    )


def test_unchanged_rescan_reuses_slot_and_mtime_drift_is_not_a_new_file():
    """CHECK-002B: 不变重扫沿用槽位；挂载盘 mtime 漂移不单独判换片。"""

    previous = [_observed("ev1", "C:\\media\\Show\\a.mkv", size=100, mtime=1.0, source_file_id="sf_1")]
    same = decide_source_identity(
        _observed("ev2", "C:\\media\\Show\\a.mkv", size=100, mtime=1.0), previous
    )
    assert same.continuity == CONTINUITY_UNCHANGED
    assert same.source_file_id == "sf_1"

    mounted = decide_source_identity(
        _observed("ev3", "C:\\media\\Show\\a.mkv", size=100, mtime=9.0, mtime_reliable=False),
        previous,
    )
    assert mounted.continuity == CONTINUITY_UNCHANGED
    assert "mtime_drift_not_content_evidence" in mounted.reasons

    local = decide_source_identity(
        _observed("ev4", "C:\\media\\Show\\a.mkv", size=100, mtime=9.0, mtime_reliable=True),
        previous,
    )
    assert local.continuity == CONTINUITY_UNCERTAIN
    assert local.source_file_id == "sf_1"


def test_size_or_hash_change_replaces_content_generation():
    """CHECK-002B: 同路径 size/哈希变化 → 新内容代次，保留槽位。"""

    previous = [_observed("ev1", "C:\\media\\Show\\a.mkv", size=100, source_file_id="sf_1")]
    replaced = decide_source_identity(
        _observed("ev2", "C:\\media\\Show\\a.mkv", size=200), previous
    )
    assert replaced.continuity == CONTINUITY_REPLACED
    assert replaced.source_file_id == "sf_1"

    hashed = [_observed("ev1", "C:\\media\\Show\\a.mkv", content_hash="aa", source_file_id="sf_1")]
    assert decide_source_identity(
        _observed("ev2", "C:\\media\\Show\\a.mkv", content_hash="aa"), hashed
    ).continuity == CONTINUITY_UNCHANGED
    assert decide_source_identity(
        _observed("ev3", "C:\\media\\Show\\a.mkv", content_hash="bb"), hashed
    ).continuity == CONTINUITY_REPLACED


def test_rename_requires_trusted_id_or_unique_hash():
    """CHECK-002B: 可靠 file id / 唯一哈希可判改名；无 ID 改名是两个对象。"""

    previous = [
        _observed("ev1", "C:\\media\\Show\\a.mkv", raw_file_id="fid-1", source_file_id="sf_1")
    ]
    renamed = decide_source_identity(
        _observed("ev2", "C:\\media\\Show\\b.mkv", raw_file_id="fid-1"), previous
    )
    assert renamed.continuity == CONTINUITY_RENAMED
    assert renamed.source_file_id == "sf_1"
    assert renamed.previous_evidence_id == "ev1"

    no_id = decide_source_identity(_observed("ev3", "C:\\media\\Show\\c.mkv"), previous)
    assert no_id.continuity == CONTINUITY_NEW
    assert no_id.source_file_id != "sf_1"

    # 相同哈希出现在多个位置：按复制处理，不任取旧项。
    duplicated = [
        _observed("ev1", "C:\\media\\A\\a.mkv", content_hash="aa", source_file_id="sf_a"),
        _observed("ev2", "C:\\media\\B\\a.mkv", content_hash="aa", source_file_id="sf_b"),
    ]
    ambiguous = decide_source_identity(
        _observed("ev3", "C:\\media\\C\\a.mkv", content_hash="aa"), duplicated
    )
    assert ambiguous.continuity == CONTINUITY_NEW
    assert "duplicate_hash_ambiguous" in ambiguous.reasons


def test_namespace_change_never_reuses_physical_asset(tmp_path):
    """CHECK-002B: 来源命名空间改变时默认新槽位；路径相同也不跨 namespace 复用。"""

    previous = [_observed("ev1", "/Anime/Show/a.mkv", size=10, source_file_id="sf_1")]
    other_namespace = ObservedFile(
        evidence_id="ev2",
        source_key="/Anime/Show/a.mkv",
        locator="/Anime/Show/a.mkv",
        identity_namespace="root:2",
        namespace_kind_name="local",
        size=10,
    )
    decision = decide_source_identity(other_namespace, previous, namespace="root:2")
    assert decision.continuity == CONTINUITY_NEW
    assert decision.source_file_id != "sf_1"


def test_slot_and_observation_rows_round_trip(tmp_path):
    """CHECK-002B: 不变 TXT 两 scan → 两观察、一个 SourceFile；观察不可改写。

    Asset 的完整 SQL 断言在 STEP-005 接入确认事务后补上；本用例只验证
    槽位与观察关联（asset_id 此时为 null，表示尚未绑定可播放代次）。
    """

    database = V4Database(tmp_path / "identity.db")
    database.initialize()
    repository = SourceIdentityRepository(database)
    from app.media_v4.persistence.repositories import V4Repository

    facts_repository = V4Repository(database)
    namespace = namespace_key(root_id="root-1")
    text = "Show\n├── a.mkv\n"

    with database.connect() as conn:
        conn.execute(
            "INSERT INTO source_roots(root_id, provider, ingest_method, created_at, updated_at) "
            "VALUES ('root-1', 'local', 'local_scan', 'now', 'now')"
        )
        for generation in (1, 2):
            conn.execute(
                "INSERT INTO source_scans(scan_id, root_id, generation, status) "
                "VALUES (?, 'root-1', ?, 'completed')",
                (f"scan-{generation}", generation),
            )

    _scan_one, first = build_directory_tree_evidence(
        text, root_id="root-1", provider="local", scan_id="scan-1", source_root="C:\\media"
    )
    _scan_two, second = build_directory_tree_evidence(
        text, root_id="root-1", provider="local", scan_id="scan-2", source_root="C:\\media"
    )
    assert first[0].evidence_id != second[0].evidence_id
    facts_repository.save_scan_evidence_bulk(first)
    facts_repository.save_scan_evidence_bulk(second)

    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        context = repository.load_identity_context("root-1", provider="local", conn=conn)
        assert context.previous_observations == ()
        first_decision = decide_source_identity(
            ObservedFile(
                evidence_id=first[0].evidence_id,
                source_key=first[0].source_key,
                locator=first[0].source_locator,
                identity_namespace=namespace,
                namespace_kind_name="local",
                size=first[0].size,
                mtime=first[0].mtime,
                mtime_reliable=True,
            ),
            context.previous_observations,
            namespace=namespace,
        )
        slot_id = repository.register_source_file(
            evidence=first[0],
            decision=first_decision,
            namespace=namespace,
            conn=conn,
            created_at="now",
        )
        repository.register_observation(
            evidence=first[0],
            source_file_id=slot_id,
            asset_id=None,
            decision=first_decision,
            conn=conn,
            created_at="now",
        )
        conn.commit()

    with database.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        context = repository.load_identity_context("root-1", provider="local", conn=conn)
        assert len(context.previous_observations) == 1
        previous = context.previous_observations[0]
        assert previous.source_file_id == slot_id
        second_decision = decide_source_identity(
            ObservedFile(
                evidence_id=second[0].evidence_id,
                source_key=second[0].source_key,
                locator=second[0].source_locator,
                identity_namespace=namespace,
                namespace_kind_name="local",
                size=second[0].size,
                mtime=second[0].mtime,
                mtime_reliable=True,
            ),
            context.previous_observations,
            namespace=namespace,
        )
        assert second_decision.continuity == CONTINUITY_UNCHANGED
        assert second_decision.source_file_id == slot_id
        repository.register_source_file(
            evidence=second[0],
            decision=second_decision,
            namespace=namespace,
            conn=conn,
            created_at="now",
        )
        repository.register_observation(
            evidence=second[0],
            source_file_id=slot_id,
            asset_id=None,
            decision=second_decision,
            conn=conn,
            created_at="now",
        )
        conn.commit()

    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM source_files").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM source_file_observations").fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(DISTINCT source_file_id) FROM source_file_observations"
        ).fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE source_file_observations SET asset_id = 'x'")
        # 重复登记同一观察幂等，不产生第二行。
        repository.register_observation(
            evidence=second[0],
            source_file_id=slot_id,
            asset_id=None,
            decision=second_decision,
            conn=conn,
            created_at="now",
        )
        assert conn.execute("SELECT COUNT(*) FROM source_file_observations").fetchone()[0] == 2
