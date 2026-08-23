"""V4 来源适配器合同。"""

from __future__ import annotations


def test_source_entry_adapter_emits_provider_and_ingest_method_separately():
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    entry = SourceEntry(
        root_id="root-1",
        scan_id="scan-1",
        provider="pan115",
        ingest_method="txt_tree",
        relative_path="动画\\Show\\Show.S01E01.mkv",
        source_locator="115://folder/Show/Show.S01E01.mkv",
        raw_file_id="115-file-1",
        size=10,
        mtime=20.0,
    )

    evidence = to_source_evidence(entry)

    assert evidence.provider == "pan115"
    assert evidence.ingest_method == "txt_tree"
    assert evidence.relative_path == "动画/Show/Show.S01E01.mkv"
    assert evidence.source_locator == entry.source_locator
    assert evidence.raw_file_id == "115-file-1"
    assert evidence.evidence_id


def test_source_entry_adapter_keeps_stable_key_and_does_not_parse_media_identity():
    from app.media_v4.sources.adapters import SourceEntry, to_source_evidence

    entry = SourceEntry(
        root_id="root-1",
        scan_id="scan-1",
        provider="local",
        ingest_method="local_scan",
        relative_path="Show/Season 1/Show S01E01.mkv",
        source_key="local-id:show-1",
    )

    first = to_source_evidence(entry)
    second = to_source_evidence(entry)

    assert first == second
    assert first.source_key == "local-id:show-1"
    assert first.fingerprint == ""
