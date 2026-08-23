"""V4 TXT 基线 + OpenList 风险受控增量扫描契约。"""

from __future__ import annotations

import pytest


def _baseline():
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    return [
        to_source_evidence(SourceEntry(
            root_id="root-hybrid",
            scan_id="scan-tree",
            provider="pan115",
            ingest_method="directory_tree",
            relative_path=f"Show{i:03d}/Show{i:03d}.S01E01.mkv",
            source_key=f"/Anime/Show{i:03d}/Show{i:03d}.S01E01.mkv",
            source_locator=f"Show{i:03d}/Show{i:03d}.S01E01.mkv",
            playback_locator=f"Show{i:03d}/Show{i:03d}.S01E01.mkv",
        ))
        for i in range(5)
    ]


class _FakeClient:
    def __init__(self, *, overlap: bool = True):
        from app.integrations.openlist.models import OpenListDirPage, OpenListEntry

        names = [f"Show{i:03d}" for i in range(5)] if overlap else ["Other"]
        self.calls: list[str] = []
        self.pages = {
            "/Anime": OpenListDirPage(
                entries=[
                    OpenListEntry(
                        name=name,
                        is_dir=True,
                        modified=2 if name == "Show004" else None,
                        remote_path=f"/Anime/{name}",
                    )
                    for name in names
                ],
                total=len(names),
            ),
        }
        for i in range(5):
            files = [
                OpenListEntry(
                    name=f"Show{i:03d}.S01E01.mkv",
                    is_dir=False,
                    size=100 + i,
                    modified=10 + i,
                    remote_path=f"/Anime/Show{i:03d}/Show{i:03d}.S01E01.mkv",
                )
            ]
            if i == 0:
                files.append(OpenListEntry(
                    name="Show000.S01E02.mkv",
                    is_dir=False,
                    size=200,
                    modified=20,
                    remote_path="/Anime/Show000/Show000.S01E02.mkv",
                ))
            self.pages[f"/Anime/Show{i:03d}"] = OpenListDirPage(entries=files, total=len(files))

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        del page, per_page, refresh
        self.calls.append(path)
        return self.pages[path]


class _FlatRootClient:
    def __init__(self, names):
        from app.integrations.openlist.models import OpenListDirPage, OpenListEntry

        self.calls = []
        self.page = OpenListDirPage(
            entries=[
                OpenListEntry(
                    name=name,
                    is_dir=False,
                    size=100,
                    modified=10,
                    remote_path=f"/Anime/{name}",
                )
                for name in names
            ],
            total=len(names),
        )

    def list_dir(self, path, page=1, per_page=100, refresh=False):
        del page, per_page, refresh
        self.calls.append(path)
        return self.page


def test_incremental_scan_uses_txt_baseline_and_a_bounded_rolling_sample():
    from app.media_v4.sources.incremental import build_tree_baseline_state, scan_openlist_incremental

    baseline = _baseline()
    state = build_tree_baseline_state("root-hybrid", "/Anime", baseline)
    client = _FakeClient()

    _scan_id, evidence, next_state, stats = scan_openlist_incremental(
        client,
        baseline=baseline,
        state=state,
        mapping_root="/",
        mount_root="",
        default_provider="pan115",
        verification_budget=2,
        now=1000,
    )

    assert client.calls == ["/Anime", "/Anime/Show000", "/Anime/Show001"]
    assert len(evidence) == 6
    assert "Show000/Show000.S01E02.mkv" in {item.relative_path for item in evidence}
    assert all(item.source_key.startswith("/Anime/") for item in evidence)
    assert stats == {"requested_directories": 3, "rolling_verified": 2, "changed_directories": 0}
    assert next_state["directories"]["Show000"]["last_verified_at"] == 1000


def test_directory_modified_time_only_prioritizes_checks_and_never_replaces_rolling_verification():
    from app.media_v4.sources.incremental import build_tree_baseline_state, scan_openlist_incremental

    baseline = _baseline()
    state = build_tree_baseline_state("root-hybrid", "/Anime", baseline)
    state["directories"]["Show004"]["modified"] = 1
    client = _FakeClient()

    _scan_id, _evidence, _next_state, stats = scan_openlist_incremental(
        client,
        baseline=baseline,
        state=state,
        mapping_root="/",
        mount_root="",
        default_provider="pan115",
        verification_budget=1,
        now=1000,
    )

    assert client.calls == ["/Anime", "/Anime/Show000", "/Anime/Show004"]
    assert stats["rolling_verified"] == 1
    assert stats["changed_directories"] == 1


def test_first_openlist_reconcile_rejects_an_obviously_wrong_remote_root():
    from app.media_v4.sources.incremental import build_tree_baseline_state, scan_openlist_incremental

    baseline = _baseline()
    state = build_tree_baseline_state("root-hybrid", "/Anime", baseline)
    client = _FakeClient(overlap=False)

    with pytest.raises(ValueError, match="没有共同条目"):
        scan_openlist_incremental(
            client,
            baseline=baseline,
            state=state,
            mapping_root="/",
            mount_root="",
            default_provider="pan115",
            verification_budget=2,
        )
    assert client.calls == ["/Anime"]


def test_first_reconcile_also_checks_videos_stored_directly_under_the_root():
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.incremental import build_tree_baseline_state, scan_openlist_incremental

    baseline = [to_source_evidence(SourceEntry(
        root_id="root-hybrid",
        scan_id="scan-tree",
        provider="pan115",
        ingest_method="directory_tree",
        relative_path="Expected.S01E01.mkv",
    ))]
    state = build_tree_baseline_state("root-hybrid", "/Anime", baseline)

    with pytest.raises(ValueError, match="没有共同条目"):
        scan_openlist_incremental(
            _FlatRootClient(["Unrelated.S01E01.mkv"]),
            baseline=baseline,
            state=state,
            mapping_root="/",
            mount_root="",
            default_provider="pan115",
        )


def test_auto_incremental_stops_when_most_top_level_entries_disappear():
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence
    from app.media_v4.sources.incremental import build_tree_baseline_state, scan_openlist_incremental

    baseline = [
        to_source_evidence(SourceEntry(
            root_id="root-hybrid",
            scan_id="scan-tree",
            provider="pan115",
            ingest_method="directory_tree",
            relative_path=f"Show{i:03d}.S01E01.mkv",
        ))
        for i in range(25)
    ]
    state = build_tree_baseline_state("root-hybrid", "/Anime", baseline)

    with pytest.raises(ValueError, match="内容骤减"):
        scan_openlist_incremental(
            _FlatRootClient(["Show000.S01E01.mkv"]),
            baseline=baseline,
            state=state,
            mapping_root="/",
            mount_root="",
            default_provider="pan115",
        )


def test_incremental_checkpoint_only_becomes_active_after_revision_confirmation(tmp_path, monkeypatch):
    from app.media_v4.sources import incremental

    monkeypatch.setattr(incremental, "get_data_dir", lambda: tmp_path)
    state = incremental.build_tree_baseline_state("root-hybrid", "/Anime", _baseline())

    incremental.stage_scan_state("scan-tree", state)
    assert incremental.load_active_state("root-hybrid") is None

    incremental.activate_scan_state("scan-tree")
    assert incremental.load_active_state("root-hybrid") == state
