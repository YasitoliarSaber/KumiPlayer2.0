"""成功资料与当前成员分离；只消费 confirmed facts，不重新识别。"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.media_v4.domain.identity import canonical_json


def _digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


def identity_signature(conn, work_id: str) -> str:
    work = conn.execute('SELECT identity_key,work_type,year,card_type FROM works WHERE work_id=?', (work_id,)).fetchone()
    slots = [tuple(row) for row in conn.execute(
        'SELECT provider,media_type,provider_id FROM provider_bindings WHERE work_id=? ORDER BY provider,media_type',
        (work_id,),
    )]
    return _digest([tuple(work) if work else None, slots])


def episode_signature(episode: dict) -> str:
    return _digest([episode.get('identity_key'), episode.get('season_id'),
                    episode.get('local_episode_number'), episode.get('absolute_episode_number'),
                    episode.get('episode_kind')])


def current_episode_signatures(conn, revision_id: str | None, work_id: str) -> dict[str, str]:
    return {str(r['episode_id']): episode_signature(dict(r)) for r in conn.execute(
        'SELECT DISTINCT e.* FROM episodes e JOIN seasons s ON s.season_id=e.season_id '
        'JOIN revision_bindings rb ON rb.episode_id=e.episode_id '
        'JOIN import_revisions ir ON ir.revision_id=rb.revision_id '
        'JOIN source_roots sr ON sr.root_id=ir.root_id '
        "WHERE e.episode_kind != 'special' AND s.season_kind != 'special' "
        "AND (? IS NULL OR rb.revision_id=?) AND rb.work_id=? AND (? IS NOT NULL OR (ir.status='confirmed' AND sr.retired_at=''))",
        (revision_id, revision_id, work_id, revision_id),
    )}


def snapshot_artifacts(conn, snapshot_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        'SELECT DISTINCT a.*,ar.role,ar.subject_id FROM artifact_references ar '
        'JOIN artifacts a ON a.artifact_id=ar.artifact_id WHERE ar.snapshot_id=?', (snapshot_id,),
    )]


def artifacts_valid(rows: list[dict]) -> bool:
    if not rows or not any(row['role'] == 'work_metadata' for row in rows):
        return False
    try:
        return all(row['status'] == 'published' and Path(row['target_path']).is_file()
                   and hashlib.sha256(Path(row['target_path']).read_bytes()).hexdigest() == row['digest']
                   for row in rows)
    except OSError:
        return False


def snapshot_requirements_valid(metadata: dict, rows: list[dict]) -> bool:
    from app.media_v4.jobs.completeness import _valid_remote_artwork_url, load_config
    if not artifacts_valid(rows):
        return False
    if load_config().artwork_storage_mode == 'remote':
        return all(_valid_remote_artwork_url(str(metadata.get(key) or '')) for key in ('poster_url', 'fanart_url'))
    kinds = {row['artifact_type'] for row in rows}
    return {'poster', 'fanart'} <= kinds


def select_applicable_snapshot(conn, revision_id: str, work_id: str) -> dict | None:
    signature = identity_signature(conn, work_id)
    for row in conn.execute(
        'SELECT ms.* FROM metadata_snapshots ms JOIN import_revisions ir ON ir.revision_id=ms.created_by_revision_id '
        'JOIN source_roots sr ON sr.root_id=ir.root_id '
        "WHERE ms.work_id=? AND ms.identity_signature=? AND sr.retired_at='' "
        'ORDER BY ms.created_at DESC,ms.snapshot_id DESC', (work_id, signature),
    ):
        artifacts = snapshot_artifacts(conn, str(row['snapshot_id']))
        if not snapshot_requirements_valid(json.loads(row['metadata_json']), artifacts):
            continue
        result = dict(row)
        result['metadata'] = json.loads(row['metadata_json'])
        # 历史版本曾把正片缺项保存为 ready；不能继续复用这个伪成功结果。
        if result['metadata'].get('reason_code') in {'episode_mapping_incomplete', 'episode_details_incomplete'}:
            continue
        old_signatures = json.loads(row['episode_signatures_json'])
        current = current_episode_signatures(conn, revision_id, work_id)
        valid = {key for key, value in current.items() if old_signatures.get(key) == value}
        result['metadata']['episode_mappings'] = [
            item for item in result['metadata'].get('episode_mappings', [])
            if str(item.get('episode_id')) in valid
        ]
        from app.media_v4.jobs.metadata_quality import current_episode_metadata
        # 增量新增成员不应废弃旧成员的有效资料，但不能据此宣称覆盖新增成员。
        result['metadata'] = current_episode_metadata(conn, revision_id, work_id, result['metadata'], valid)
        if result['metadata'].get('metadata_state') != 'ready':
            continue
        result['covers_members'] = set(current) <= valid
        result['artifacts'] = artifacts
        return result
    return None


def attach_snapshot_refs(conn, revision_id: str, work_id: str, snapshot: dict) -> None:
    if snapshot['identity_signature'] != identity_signature(conn, work_id):
        raise ValueError('metadata identity changed before publication')
    now = datetime.now(UTC).isoformat()
    conn.execute(
        'INSERT INTO revision_metadata_refs(revision_id,work_id,snapshot_id,created_at) VALUES (?,?,?,?) '
        'ON CONFLICT(revision_id,work_id) DO UPDATE SET snapshot_id=excluded.snapshot_id,created_at=excluded.created_at',
        (revision_id, work_id, snapshot['snapshot_id'], now),
    )
    # 保留历史快照的产物清单。有效引用由当前 metadata_ref 选中的代次限定。
    current = current_episode_signatures(conn, revision_id, work_id)
    for row in snapshot['artifacts']:
        if row['role'] == 'episode_metadata' and row['subject_id'] not in current:
            continue
        conn.execute(
            'INSERT OR IGNORE INTO artifact_references(reference_id,artifact_id,revision_id,work_id,'
            'snapshot_id,role,subject_id,created_at) VALUES (?,?,?,?,?,?,?,?)',
            (str(uuid.uuid4()), row['artifact_id'], revision_id, work_id, snapshot['snapshot_id'],
             row['role'], row['subject_id'], now),
        )


def publish_snapshot(conn, *, revision_id: str, work_id: str, snapshot_id: str,
                     metadata: dict, artifact_ids: list[str], artifact_subjects: dict[str, str] | None = None) -> dict:
    signatures = current_episode_signatures(conn, revision_id, work_id)
    mapped = {str(item['episode_id']) for item in metadata.get('episode_mappings', [])}
    signatures = {key: value for key, value in signatures.items() if key in mapped}
    signature = identity_signature(conn, work_id)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        'INSERT INTO metadata_snapshots(snapshot_id,work_id,provider,media_type,provider_id,'
        'identity_signature,metadata_json,episode_signatures_json,created_by_revision_id,created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (snapshot_id, work_id, metadata['provider'], metadata.get('media_type', ''), str(metadata['provider_id']),
         signature, canonical_json(metadata), canonical_json(signatures), revision_id, now),
    )
    for artifact_id in artifact_ids:
        row = conn.execute('SELECT * FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
        if row is None or row['revision_id'] != revision_id or row['work_id'] != work_id:
            raise ValueError('metadata artifact is outside publication')
        kind = row['artifact_type']
        role = 'work_metadata' if kind == 'nfo' else 'episode_metadata' if kind == 'episode_nfo' else 'artwork'
        subject = Path(row['target_path']).stem.removeprefix('episode-') if kind == 'episode_nfo' else work_id
        if kind == 'episode_thumb':
            subject = Path(row['target_path']).stem.removeprefix('episode-').removesuffix('-thumb')
        if artifact_subjects and artifact_id in artifact_subjects:
            subject = artifact_subjects[artifact_id]
        conn.execute(
            'INSERT INTO artifact_references(reference_id,artifact_id,revision_id,work_id,snapshot_id,role,subject_id,created_at) '
            'VALUES (?,?,?,?,?,?,?,?)', (str(uuid.uuid4()), artifact_id, revision_id, work_id, snapshot_id, role, subject, now),
        )
    snapshot = dict(conn.execute('SELECT * FROM metadata_snapshots WHERE snapshot_id=?', (snapshot_id,)).fetchone())
    snapshot['artifacts'] = snapshot_artifacts(conn, snapshot_id)
    attach_snapshot_refs(conn, revision_id, work_id, snapshot)
    return snapshot


def import_legacy_snapshot(conn, work_id: str) -> None:
    """用户确认时才采用可证明的旧成功资料；迁移与投影绝不批量转换。"""
    if conn.execute('SELECT 1 FROM metadata_snapshots WHERE work_id=? LIMIT 1', (work_id,)).fetchone():
        return
    work = conn.execute('SELECT work_type,year FROM works WHERE work_id=?', (work_id,)).fetchone()
    media_type = {'series': 'tv', 'movie': 'movie'}.get(work['work_type'], '')
    if not media_type:
        return
    rows = conn.execute(
        "SELECT sb.* FROM scrape_bindings sb JOIN import_revisions ir ON ir.revision_id=sb.revision_id "
        "JOIN source_roots sr ON sr.root_id=ir.root_id AND sr.retired_at='' "
        "JOIN provider_bindings pb ON pb.work_id=sb.work_id AND pb.provider=sb.provider AND pb.provider_id=sb.provider_id "
        "WHERE sb.work_id=? AND sb.status='confirmed' AND pb.media_type=? AND ir.confirmed_at!='' "
        'ORDER BY sb.updated_at DESC,sb.binding_id DESC', (work_id, media_type),
    ).fetchall()
    for row in rows:
        try:
            metadata = json.loads(row['metadata_json'])
        except (TypeError, ValueError):
            continue
        if metadata.get('metadata_state') != 'ready' or metadata.get('metadata_snapshot_id'):
            continue
        if metadata.get('media_type', media_type) != media_type:
            continue
        if work['year'] and metadata.get('year') and int(metadata['year']) != work['year']:
            continue
        artifacts = [dict(r) for r in conn.execute(
            "SELECT * FROM artifacts WHERE revision_id=? AND work_id=? AND status='published' "
            "AND artifact_type IN ('nfo','poster','fanart','clearlogo','episode_nfo')",
            (row['revision_id'], work_id),
        )]
        for artifact in artifacts:
            artifact['role'] = 'work_metadata' if artifact['artifact_type'] == 'nfo' else 'artwork'
        if not snapshot_requirements_valid(metadata, artifacts):
            continue
        episodes = {str(e['episode_id']): dict(e) for e in conn.execute(
            'SELECT DISTINCT e.*,s.local_season_number FROM episodes e JOIN seasons s ON s.season_id=e.season_id '
            'JOIN revision_bindings rb ON rb.episode_id=e.episode_id WHERE rb.revision_id=? AND rb.work_id=?',
            (row['revision_id'], work_id),
        )}
        subjects = {}
        valid_mappings = []
        for mapping in metadata.get('episode_mappings', []):
            episode = episodes.get(str(mapping.get('episode_id')))
            if not episode or episode['local_season_number'] is None or episode['local_episode_number'] is None:
                continue
            name = f"S{episode['local_season_number']:02d}E{episode['local_episode_number']:02d}.nfo"
            matching = [a for a in artifacts if a['artifact_type'] == 'episode_nfo' and Path(a['target_path']).name == name]
            if len(matching) == 1:
                subjects[matching[0]['artifact_id']] = episode['episode_id']
                valid_mappings.append(mapping)
        artifacts = [a for a in artifacts if a['artifact_type'] != 'episode_nfo' or a['artifact_id'] in subjects]
        metadata = {**metadata, 'provider': row['provider'], 'provider_id': row['provider_id'],
                    'media_type': media_type, 'episode_mappings': valid_mappings,
                    'legacy_binding_id': row['binding_id']}
        publish_snapshot(conn, revision_id=row['revision_id'], work_id=work_id, snapshot_id=str(uuid.uuid4()),
                         metadata=metadata, artifact_ids=[a['artifact_id'] for a in artifacts], artifact_subjects=subjects)
        return


def referenced_metadata(conn, work_id: str, revision_id: str | None = None) -> dict | None:
    row = conn.execute(
        'SELECT ms.*,r.revision_id FROM revision_metadata_refs r '
        'JOIN metadata_snapshots ms ON ms.snapshot_id=r.snapshot_id '
        'JOIN import_revisions ir ON ir.revision_id=r.revision_id '
        'JOIN source_roots sr ON sr.root_id=ir.root_id '
        "WHERE r.work_id=? AND ir.status='confirmed' AND sr.retired_at='' AND (? IS NULL OR r.revision_id=?) "
        'ORDER BY r.created_at DESC,r.revision_id DESC LIMIT 1', (work_id, revision_id, revision_id),
    ).fetchone()
    if row is None or row['identity_signature'] != identity_signature(conn, work_id):
        return None
    if not snapshot_requirements_valid(json.loads(row['metadata_json']), snapshot_artifacts(conn, row['snapshot_id'])):
        return None
    metadata = json.loads(row['metadata_json'])
    current = current_episode_signatures(conn, revision_id, work_id)
    old = json.loads(row['episode_signatures_json'])
    mappings = [m for m in metadata.get('episode_mappings', [])
                if str(m.get('episode_id')) in current and old.get(str(m.get('episode_id'))) == current[str(m['episode_id'])]]
    if revision_id is None:
        # 一个Work可由多个活动来源共同贡献；最新单根snapshot不能遮蔽其他根的有效资料。
        combined = {str(m['episode_id']): m for m in mappings}
        for other in conn.execute(
            'SELECT DISTINCT ms.* FROM revision_metadata_refs mr JOIN metadata_snapshots ms ON ms.snapshot_id=mr.snapshot_id '
            'JOIN import_revisions ir ON ir.revision_id=mr.revision_id JOIN source_roots sr ON sr.root_id=ir.root_id '
            "WHERE mr.work_id=? AND ms.identity_signature=? AND ir.status='confirmed' AND sr.retired_at='' "
            'ORDER BY ms.created_at DESC,ms.snapshot_id DESC', (work_id, row['identity_signature']),
        ):
            data = json.loads(other['metadata_json'])
            if not snapshot_requirements_valid(data, snapshot_artifacts(conn, other['snapshot_id'])):
                continue
            signatures = json.loads(other['episode_signatures_json'])
            for mapping in data.get('episode_mappings', []):
                key = str(mapping.get('episode_id'))
                if key in current and signatures.get(key) == current[key]:
                    combined.setdefault(key, mapping)
        mappings = list(combined.values())
    mapped = {str(m['episode_id']) for m in mappings}
    total = len(current)
    metadata.update(episode_mappings=mappings, mapped_count=len(mapped), total_count=total,
                    unmapped_episode_ids=sorted(set(current) - mapped),
                    episode_mapping_status=('not_applicable' if not total else 'complete' if len(mapped) == total else 'partial' if mapped else 'unmapped'),
                    metadata_state='ready', metadata_snapshot_id=row['snapshot_id'],
                    metadata_source='current' if row['created_by_revision_id'] == row['revision_id'] else 'retained')
    from app.media_v4.jobs.metadata_quality import current_episode_metadata
    metadata = current_episode_metadata(conn, revision_id, work_id, metadata)
    attempt = conn.execute("SELECT status,result_json,last_error FROM jobs WHERE revision_id=? AND work_id=? AND job_type='scrape_work' ORDER BY updated_at DESC LIMIT 1",
                           (row['revision_id'], work_id)).fetchone()
    result = json.loads(attempt['result_json'] or '{}') if attempt else {}
    refresh = ('failed' if attempt and attempt['status'] == 'failed' else
               'cancelled' if attempt and attempt['status'] == 'cancelled' else
               'pending' if attempt and attempt['status'] in {'queued', 'running'} else
               result.get('refresh_status') or 'not_requested')
    metadata['refresh_status'] = refresh
    if refresh in {'failed', 'cancelled'}:
        metadata['metadata_source'] = 'retained'
        metadata['metadata_refresh_error'] = result.get('reason_codes') or (attempt['last_error'] if attempt else '')
    return metadata


def assert_artifact_unreferenced(conn, artifact_id: str) -> None:
    rows = conn.execute(
        'SELECT used.target_path AS used_path,candidate.target_path AS candidate_path '
        'FROM artifact_references ar JOIN artifacts used ON used.artifact_id=ar.artifact_id '
        'JOIN artifacts candidate ON candidate.artifact_id=? '
        'JOIN import_revisions ir ON ir.revision_id=ar.revision_id '
        'JOIN source_roots sr ON sr.root_id=ir.root_id '
        "WHERE ir.status='confirmed' AND sr.retired_at='' "
        'AND (ar.snapshot_id IS NULL OR EXISTS (SELECT 1 FROM revision_metadata_refs mr '
        'WHERE mr.revision_id=ar.revision_id AND mr.work_id=ar.work_id AND mr.snapshot_id=ar.snapshot_id))',
        (artifact_id,),
    )
    for row in rows:
        if os.path.normcase(str(Path(row['used_path']).resolve(strict=False))) == os.path.normcase(str(Path(row['candidate_path']).resolve(strict=False))):
            raise ValueError('artifact has active references')


def collect_cleanup_candidates(conn, revision_id: str, mirror_root: str | Path) -> list[dict]:
    # 旧版本没有subject引用时，无法证明同语义替代：保守保留，不能凭superseded删除。
    root = Path(mirror_root).resolve(strict=False)
    source_paths = [Path(r[0]).resolve(strict=False) for r in conn.execute(
        "SELECT source_locator FROM source_roots WHERE source_locator!=''")
        if '://' not in str(r[0])]
    candidates = []
    for row in conn.execute(
        'SELECT DISTINCT a.*,fresh.target_path AS replacement_path,fresh.digest AS replacement_digest '
        'FROM artifacts a JOIN artifact_references old ON old.artifact_id=a.artifact_id '
        'JOIN artifact_references replacement ON replacement.subject_id=old.subject_id AND replacement.role=old.role '
        'JOIN artifacts fresh ON fresh.artifact_id=replacement.artifact_id '
        "WHERE replacement.revision_id=? AND a.artifact_id!=fresh.artifact_id AND a.status='published' AND fresh.status='published' "
        'AND a.artifact_type=fresh.artifact_type AND a.work_id=fresh.work_id '
        'AND (replacement.snapshot_id IS NULL OR EXISTS (SELECT 1 FROM revision_metadata_refs mr '
        'WHERE mr.revision_id=replacement.revision_id AND mr.work_id=replacement.work_id AND mr.snapshot_id=replacement.snapshot_id))',
        (revision_id,),
    ):
        try:
            assert_artifact_unreferenced(conn, row['artifact_id'])
        except ValueError:
            continue
        path = Path(row['target_path'])
        resolved = path.resolve(strict=False)
        if any(resolved == source or source in resolved.parents for source in source_paths):
            continue
        if resolved == root or root not in resolved.parents or path.is_symlink() or not path.is_file():
            continue
        replacement_path = Path(row['replacement_path'])
        try:
            valid = (hashlib.sha256(path.read_bytes()).hexdigest() == row['digest']
                     and replacement_path.is_file() and not replacement_path.is_symlink()
                     and hashlib.sha256(replacement_path.read_bytes()).hexdigest() == row['replacement_digest'])
        except OSError:
            valid = False
        if not valid:
            continue
        candidates.append(dict(row))
    return list({item['artifact_id']: item for item in candidates}.values())
