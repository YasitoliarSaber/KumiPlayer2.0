"""只读身份重建预览（STEP-006，F-004 延伸）。

用途：当历史 confirmed revision 由已修正的旧解析器产生（例如同一作品被拆成
两个 Work、编号退化为 unknown）时，先给出**只读**比较，让用户看清
「旧绑定 → 新解析图」的逐文件映射，以及观看记录、Provider 身份和其他来源引用
是否会受影响，再决定是否另行授权真实库修复。

红线（本模块必须一直满足）：

- 只读：不写数据库、不写镜像、不删产物、不动真实媒体；
- 不改写 confirmed 事实与旧 ParsedFacts；新解析只在内存里发生；
- 不自动合并、不自动迁移；只描述差异并明确列出需要授权才能执行的动作；
- 不重新识别来源以外的任何东西（证据仍来自该 revision 自己的 evidence 行）。
"""

from __future__ import annotations

from collections import defaultdict

from app.media_v4.persistence.database import V4Database


def preview_revision_reparse(database: V4Database, revision_id: str) -> dict:
    """比较一个 revision 的旧绑定与当前解析器在同一批证据上的新解析图。"""

    revision, evidence_rows, bindings, old_works = _read_revision(database, revision_id)
    if revision is None:
        raise KeyError(revision_id)

    from app.media_v4.parsing.parser import V4Parser
    from app.media_v4.resolution.resolver import MediaResolver

    parser = V4Parser()
    entries = []
    for row in evidence_rows:
        evidence = _evidence_from_row(row)
        entries.append((evidence, parser.parse(evidence)))

    graph = MediaResolver().resolve(entries) if entries else None
    new_title_by_key = {work.work_key: work.preferred_title for work in (graph.works if graph else ())}
    new_work_by_evidence: dict[str, str] = {}
    new_episode_by_evidence: dict[str, tuple[int | None, int | None]] = {}
    if graph is not None:
        for work in graph.works:
            for evidence_id in work.source_evidence_ids:
                new_work_by_evidence.setdefault(evidence_id, work.work_key)
        for episode in graph.episodes:
            for evidence_id in episode.asset_evidence_ids:
                new_episode_by_evidence[evidence_id] = (
                    episode.local_season_number,
                    episode.local_episode_number,
                )
                new_work_by_evidence.setdefault(evidence_id, episode.work_key)

    old_by_evidence: dict[str, dict] = {}
    for row in bindings:
        old_by_evidence.setdefault(str(row["evidence_id"]), {
            "work_id": str(row["work_id"] or ""),
            "episode_id": str(row["episode_id"] or ""),
            "asset_id": str(row["asset_id"] or ""),
        })

    files = []
    changed = 0
    unplaced: list[str] = []
    for row in evidence_rows:
        evidence_id = str(row["evidence_id"])
        old = old_by_evidence.get(evidence_id, {})
        old_title = old_works.get(old.get("work_id", ""), "")
        new_work_key = new_work_by_evidence.get(evidence_id, "")
        new_title = new_title_by_key.get(new_work_key, "")
        new_episode = new_episode_by_evidence.get(evidence_id)
        identity_changed = bool(old_title) and bool(new_title) and (
            _normalize(old_title) != _normalize(new_title)
        )
        placement_lost = bool(old.get("work_id")) and not new_work_key
        if identity_changed or placement_lost:
            changed += 1
        if not new_work_key and str(row["entry_kind"]) == "video":
            unplaced.append(evidence_id)
        files.append({
            "evidence_id": evidence_id,
            "relative_path": str(row["relative_path"] or ""),
            "old_work_id": old.get("work_id", ""),
            "old_work_title": old_title,
            "old_episode_id": old.get("episode_id", ""),
            "old_asset_id": old.get("asset_id", ""),
            "new_work_key": new_work_key,
            "new_work_title": new_title,
            "new_local_season": new_episode[0] if new_episode else None,
            "new_local_episode": new_episode[1] if new_episode else None,
            "work_identity_changed": identity_changed,
            "placement_lost": placement_lost,
        })

    old_work_ids = sorted({str(row["work_id"]) for row in bindings if str(row["work_id"] or "")})
    new_work_keys = sorted({work.work_key for work in (graph.works if graph else ())})
    merges = _work_merges(files, old_works, new_title_by_key)
    return {
        "revision_id": revision_id,
        "revision_status": str(revision["status"] or ""),
        "root_id": str(revision["root_id"] or ""),
        "read_only": True,
        "writes_performed": False,
        "requires_explicit_authorization": True,
        "old": {
            "work_count": len(old_work_ids),
            "work_ids": old_work_ids,
            "bound_evidence_count": len(bindings),
        },
        "new": {
            "parser_version": parser.VERSION,
            "work_count": len(new_work_keys),
            "work_keys": new_work_keys,
            "episode_count": len(graph.episodes) if graph else 0,
            "issue_codes": sorted({issue.code for issue in (graph.issues if graph else ())}),
        },
        "changed_file_count": changed,
        "unplaced_evidence_ids": unplaced,
        "work_merges": merges,
        "files": files,
        "preservation": _preservation_snapshot(database, old_work_ids),
        "notes": [
            "本预览不写入任何数据；旧 confirmed 绑定、ParsedFacts、镜像与观看记录都未改动。",
            "新解析图只在内存中产生，使用当前 parser 版本重新解析同一批 source evidence。",
            "真实库修复（新建 revision、合并 Work、迁移观看记录、清理镜像产物）需要单独授权。",
        ],
    }


def _work_merges(files: list[dict], old_works: dict[str, str], new_title_by_key: dict[str, str]) -> list[dict]:
    """一个新媒体库作品覆盖了两个以上旧作品时列出合并关系（只读描述）。"""

    by_new: dict[str, set[str]] = {}
    for row in files:
        new_key = row["new_work_key"]
        old_id = row["old_work_id"]
        if new_key and old_id:
            by_new.setdefault(new_key, set()).add(old_id)
    merges = []
    for new_key, old_ids in sorted(by_new.items()):
        if len(old_ids) < 2:
            continue
        merges.append({
            "new_work_key": new_key,
            "new_work_title": new_title_by_key.get(new_key, ""),
            "old_work_ids": sorted(old_ids),
            "old_work_titles": sorted({old_works.get(work_id, "") for work_id in old_ids if old_works.get(work_id, "")}),
        })
    return merges


def _normalize(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _read_revision(database: V4Database, revision_id: str):
    with database.connect() as conn:
        revision = conn.execute(
            "SELECT revision_id, root_id, scan_id, status, confirmed_at FROM import_revisions WHERE revision_id = ?",
            (revision_id,),
        ).fetchone()
        if revision is None:
            return None, [], [], {}
        evidence_rows = conn.execute(
            """
            SELECT se.* FROM revision_evidence re
            JOIN source_evidence se ON se.evidence_id = re.evidence_id
            WHERE re.revision_id = ?
            ORDER BY se.relative_path, se.evidence_id
            """,
            (revision_id,),
        ).fetchall()
        bindings = conn.execute(
            "SELECT evidence_id, work_id, episode_id, asset_id FROM revision_bindings "
            "WHERE revision_id = ? ORDER BY evidence_id",
            (revision_id,),
        ).fetchall()
        work_rows = conn.execute(
            "SELECT work_id, preferred_title FROM works"
        ).fetchall()
    return revision, evidence_rows, bindings, {
        str(row["work_id"]): str(row["preferred_title"] or "") for row in work_rows
    }


def _preservation_snapshot(database: V4Database, work_ids: list[str]) -> dict:
    """观看记录、Provider 身份与其他来源引用（只读计数）。"""

    if not work_ids:
        return {
            "playback_progress_rows": 0,
            "playback_history_rows": 0,
            "provider_binding_rows": 0,
            "other_source_reference_rows": 0,
        }
    placeholders = ",".join("?" for _ in work_ids)
    params = tuple(work_ids)
    with database.connect() as conn:
        progress = conn.execute(
            f"SELECT COUNT(*) FROM playback_progress WHERE work_id IN ({placeholders})", params
        ).fetchone()[0]
        history = conn.execute(
            f"SELECT COUNT(*) FROM playback_history WHERE work_id IN ({placeholders})", params
        ).fetchone()[0]
        provider = conn.execute(
            f"SELECT COUNT(*) FROM provider_bindings WHERE work_id IN ({placeholders})", params
        ).fetchone()[0]
        other_sources = conn.execute(
            f"""
            SELECT COUNT(*) FROM revision_bindings rb
            JOIN import_revisions ir ON ir.revision_id = rb.revision_id
            JOIN source_roots sr ON sr.root_id = ir.root_id
            WHERE rb.work_id IN ({placeholders}) AND ir.status = 'confirmed'
              AND sr.retired_at = ''
            """,
            params,
        ).fetchone()[0]
    return {
        "playback_progress_rows": int(progress or 0),
        "playback_history_rows": int(history or 0),
        "provider_binding_rows": int(provider or 0),
        "other_source_reference_rows": int(other_sources or 0),
    }


def _evidence_from_row(row):
    from app.media_v4.domain.models import SourceEvidence

    return SourceEvidence(
        evidence_id=str(row["evidence_id"]),
        scan_id=str(row["scan_id"]),
        root_id=str(row["root_id"]),
        source_key=str(row["source_key"]),
        relative_path=str(row["relative_path"]),
        entry_kind=str(row["entry_kind"]),
        provider=str(row["provider"] or ""),
        size=row["size"],
        mtime=row["mtime"],
        fingerprint=str(row["fingerprint"] or ""),
        raw_file_id=str(row["raw_file_id"] or ""),
        ingest_method=str(row["ingest_method"] or ""),
        source_route_id=str(row["source_route_id"] or ""),
        source_locator=str(row["source_locator"] or ""),
        playback_locator=str(row["playback_locator"] or ""),
        tmdb_hint_id=str(row["tmdb_hint_id"] or ""),
        tmdb_hint_type=str(row["tmdb_hint_type"] or ""),
        import_family=str(row["import_family"] or "anime"),
        target_filename=str(row["target_filename"] or ""),
        observed_at=str(row["observed_at"] or ""),
    )
