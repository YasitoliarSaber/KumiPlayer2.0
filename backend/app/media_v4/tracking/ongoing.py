"""追更是来源的显式用途；采集与发布继续使用唯一 V4 链。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from app.media_v4.domain.identity import DecisionTrace, NumberingEvidence
from app.media_v4.domain.models import ParsedFacts, SourceEvidence
from app.media_v4.parsing.parser import V4Parser
from app.media_v4.persistence.database import V4Database


def parse_for_scope(
    parser: V4Parser, evidence: SourceEvidence, *, content_scope: str = "completed", root_container: str = "",
) -> ParsedFacts:
    facts = parser.parse(evidence, root_container=root_container)
    if content_scope != "ongoing" or evidence.entry_kind != "video" or facts.resource_type != "video" or facts.is_auxiliary:
        return facts
    number = facts.episode_candidate or facts.absolute_episode_candidate or facts.special_number
    if not number:
        marker = re.search(r"(?i)(?<![A-Za-z])(?:SP|OVA|OAD)[ ._-]*(\d+)(?!\d|\.\d)", evidence.relative_path.rsplit("/", 1)[-1])
        number = int(marker[1]) if marker else None
    if number is None:
        candidates = {trace.value.get("number") for trace in facts.decision_trace
                      if trace.field == "episode_number_candidate" and isinstance(trace.value, dict)
                      and isinstance(trace.value.get("number"), int)}
        if len(candidates) == 1:
            number = next(iter(candidates))
    if number is not None and number <= 0:
        number = None
    traces = tuple(trace for trace in facts.decision_trace if trace.field not in {
        "media_type", "content_class", "classification_state", "numbering",
    })
    trace = DecisionTrace(field="content_scope", value="ongoing", origin="structured_hint",
                          scope=evidence.root_id, rule_id="explicit_ongoing_source", confidence="high")
    season = facts.season_candidate if facts.season_candidate and facts.season_candidate > 0 else 1
    numbering = NumberingEvidence(
        season_origin=facts.numbering.season_origin if facts.season_candidate and facts.season_candidate > 0 else "explicit_directory",
        episode_origin=(facts.numbering.episode_origin if facts.numbering.episode_origin != "unknown"
                        else "explicit_filename") if number is not None else "unknown",
        scope_key=evidence.root_id, basis=(*facts.numbering.basis, "explicit_ongoing_source"),
    )
    decisions = tuple(DecisionTrace(field=field, value=value, origin="structured_hint", scope=evidence.root_id,
                                    rule_id="explicit_ongoing_source", confidence="high")
                      for field, value in (("media_type", "tv"), ("content_class", "regular"),
                                           ("classification_state", "explicit")))
    number_trace = DecisionTrace(field="numbering", value={"season": season, "episode": number, "absolute": None,
                                                         "episode_origin": numbering.episode_origin},
                                 origin=numbering.episode_origin, scope=evidence.root_id,
                                 rule_id="ongoing_numbering", confidence="high" if number is not None else "low")
    digest = hashlib.sha256((facts.parsed_fact_id + ":ongoing-1").encode("utf-8")).hexdigest()[:32]
    return replace(
        facts, parsed_fact_id="pf_" + digest, parser_version=facts.parser_version + "+ongoing-1",
        media_type="tv", group_type="season", content_class="regular", classification_state="explicit",
        card_type="series", relation_type="", special_candidate=False, special_number=None,
        season_candidate=season,
        episode_candidate=number, absolute_episode_candidate=None, episode_range=facts.episode_range,
        is_importable=True, is_auxiliary=False, needs_review=number is None,
        tmdb_hint_type="tv" if facts.tmdb_hint_type == "tv" else "",
        tmdb_hint_id=facts.tmdb_hint_id if facts.tmdb_hint_type == "tv" else None,
        reasons=("明确追更来源，按正片处理",) + (("无法定位正片集号，需要确认",) if number is None else ()),
        decision_trace=(*traces, trace, *decisions, number_trace), numbering=numbering,
    )


def set_source_scope(database: V4Database, root_id: str, content_scope: str, *, conn=None) -> None:
    if content_scope not in {"completed", "ongoing"}:
        raise ValueError("来源分类无效")
    if conn is None:
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            set_source_scope(database, root_id, content_scope, conn=connection)
        return
    row = conn.execute("SELECT * FROM source_roots WHERE root_id=?", (root_id,)).fetchone()
    if row is None:
        raise ValueError("来源不存在")
    previous = str(row["content_scope"] or "")
    confirmed = conn.execute("SELECT 1 FROM import_revisions WHERE root_id=? AND status='confirmed'", (root_id,)).fetchone()
    if confirmed and (previous or "completed") != content_scope:
        raise ValueError("已导入来源的分类不能直接更换，请为追更选择独立文件夹")
    def location(source):
        value = str(source["source_locator"] or source["playback_locator"] or "").replace("\\", "/")
        if not value:
            return ""
        # 远端路径区分大小写；Windows 本地盘符与 UNC 仍作不区分大小写比较。
        if not value.startswith("/") or value.startswith("//"):
            value = value.casefold()
        return value.rstrip("/") or "/"

    locator = location(row)
    for other in conn.execute("SELECT * FROM source_roots WHERE root_id!=? AND retired_at=''", (root_id,)):
        if (other["content_scope"] or "completed") == content_scope:
            continue
        candidate = location(other)
        if not locator or not candidate or locator.startswith("/") != candidate.startswith("/"):
            continue
        if locator.startswith("/"):
            def namespace(identity):
                request = conn.execute(
                    "SELECT req.request_json FROM source_scans scan JOIN source_scan_requests req ON req.scan_id=scan.scan_id "
                    "WHERE scan.root_id=? ORDER BY scan.generation DESC LIMIT 1", (identity,),
                ).fetchone()
                payload = json.loads(request[0]) if request else {}
                return payload.get("account_namespace") or payload.get("connection_id") or "legacy"

            if namespace(root_id) != namespace(str(other["root_id"])):
                continue
        if locator and candidate and (locator == candidate or locator.startswith(candidate.rstrip("/") + "/") or candidate.startswith(locator.rstrip("/") + "/")):
            raise ValueError("追更来源必须使用独立文件夹，不能与已完结来源互相包含")
    conn.execute("UPDATE source_roots SET content_scope=? WHERE root_id=?", (content_scope, root_id))


def assess_update(database: V4Database, revision_id: str) -> str:
    """只追加已确认且有在线绑定的已知正片季；未知作品/季/编号交回预览。"""
    from app.media_v4.resolution.title_norm import normalize_identity_title
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    with database.connect() as conn:
        revision = conn.execute("SELECT ir.root_id,sr.content_scope FROM import_revisions ir JOIN source_roots sr ON sr.root_id=ir.root_id WHERE revision_id=?", (revision_id,)).fetchone()
        if revision is None or revision["content_scope"] != "ongoing":
            return "review"
        if conn.execute("SELECT 1 FROM revision_overrides WHERE revision_id=?", (revision_id,)).fetchone():
            return "review"
        previous = conn.execute("SELECT revision_id FROM import_revisions WHERE root_id=? AND status='confirmed'",
                                (revision["root_id"],)).fetchone()
        if previous is None:
            return "review"
        old = service._load_revision_entries(str(previous[0]), conn=conn)
        current = service._load_revision_entries(revision_id, conn=conn)
        old_facts = {item.relative_path: facts for item, facts in old}
        current_facts = {item.relative_path: facts for item, facts in current}
        old_slots = {item.relative_path: item for item, _ in old}
        current_slots = {item.relative_path: item for item, _ in current}
        if not old_slots.keys() <= current_slots.keys():
            return "review"
        for path, original in old_slots.items():
            observed = current_slots[path]
            for field in ("work_title", "year_candidate", "media_type", "group_type", "season_candidate",
                          "episode_candidate", "episode_range", "content_class", "is_importable"):
                if getattr(old_facts[path], field) != getattr(current_facts[path], field):
                    return "review"
            for field in ("size", "mtime", "fingerprint", "raw_file_id"):
                first, second = getattr(original, field), getattr(observed, field)
                if first not in (None, "") and second not in (None, "") and first != second:
                    return "review"
        additions = [(item, facts) for item, facts in current if item.relative_path not in old_slots and facts.is_importable]
        if not additions:
            return "unchanged"
        if service.load_draft_graph(revision_id, refresh_snapshot=False).issues:
            return "review"
        owners: dict[tuple[str, int | None], set[str]] = {}
        existing_numbers: set[tuple[str, int | None, int | None]] = set()
        for item, facts in old:
            title = normalize_identity_title(facts.work_title)
            key = (title, facts.season_candidate)
            existing_numbers.add((*key, facts.episode_candidate))
            for row in conn.execute("SELECT DISTINCT work_id FROM revision_bindings WHERE revision_id=? AND evidence_id=?",
                                    (str(previous[0]), item.evidence_id)):
                owners.setdefault(key, set()).add(str(row[0]))
        for _item, facts in additions:
            key = (normalize_identity_title(facts.work_title), facts.season_candidate)
            work_ids = owners.get(key, set())
            episode_key = (*key, facts.episode_candidate)
            if (facts.needs_review or facts.media_type != "tv" or facts.group_type != "season"
                    or facts.episode_candidate is None or facts.episode_candidate <= 0
                    or facts.episode_range is not None or len(work_ids) != 1
                    or episode_key in existing_numbers):
                return "review"
            existing_numbers.add(episode_key)
            work_id = next(iter(work_ids))
            bindings = conn.execute("SELECT provider_id,metadata_json FROM scrape_bindings "
                                    "WHERE revision_id=? AND work_id=? AND provider='tmdb' AND status='confirmed'",
                                    (str(previous[0]), work_id)).fetchall()
            if len(bindings) != 1:
                return "review"
            frozen = conn.execute("SELECT provider_id FROM provider_bindings WHERE work_id=? AND provider='tmdb' AND media_type='tv'",
                                  (work_id,)).fetchall()
            if len(frozen) != 1 or frozen[0][0] != bindings[0]["provider_id"]:
                return "review"
            try:
                metadata = json.loads(bindings[0]["metadata_json"])
            except (ValueError, TypeError):
                return "review"
            if not str(bindings[0]["provider_id"]).isdigit() or metadata.get("metadata_state") != "ready":
                return "review"
    return "append"


def finish_ongoing_update(database: V4Database, revision_id: str) -> str:
    """发布仍经原 revision 确认事务；重复恢复不重复生成作业。"""
    from app.media_v4.revisions.service import V4RevisionService

    service = V4RevisionService(database)
    if service.get_status(revision_id) == "confirmed":
        return "append"
    outcome = assess_update(database, revision_id)
    if outcome == "append":
        try:
            service.confirm(revision_id, auto_append=True)
        except ValueError:
            return "review"
    elif outcome == "unchanged":
        with database.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            status = conn.execute("SELECT status FROM import_revisions WHERE revision_id=?", (revision_id,)).fetchone()
            if status is None or status[0] != "draft":
                return "append" if status and status[0] == "confirmed" else "review"
            # 预览期间可能已有人工修正；无变化收口同样不能吞掉并发覆盖。
            if assess_update(database, revision_id) != "unchanged":
                return "review"
            conn.execute("UPDATE import_revisions SET status='superseded' WHERE revision_id=? AND status='draft'",
                         (revision_id,))
    return outcome
