"""清理已经被新 revision 取代、且不再被当前图引用的生成产物。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.media_v4.jobs.control import cancel_requested, claim_running, mark_cancelled
from app.media_v4.persistence.database import V4Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


class V4ArtifactCleanup:
    def __init__(self, database: V4Database):
        self.database = database

    def process(self, job_id: str, mirror_root: str | Path) -> tuple[str, ...]:
        """后台仅计算候选；物理删除必须经维护预览和明确确认。"""
        from app.media_v4.persistence.metadata_lifecycle import collect_cleanup_candidates

        with self.database.connect() as conn:
            job = conn.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job['job_type'] != 'cleanup_superseded_artifacts':
                raise ValueError('不是清理候选任务')
            if job['status'] == 'succeeded':
                return ()
        if not claim_running(self.database, job_id):
            if cancel_requested(self.database, job_id):
                mark_cancelled(self.database, job_id)
                return ()
            raise RuntimeError('清理候选任务已被领取')
        with self.database.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            live = conn.execute(
                "SELECT 1 FROM jobs j JOIN import_revisions ir ON ir.revision_id=j.revision_id "
                "WHERE j.job_id=? AND j.status='running' AND j.cancel_requested=0 AND ir.status='confirmed'",
                (job_id,),
            ).fetchone()
            if live is None:
                return ()
            candidates = collect_cleanup_candidates(conn, job['revision_id'], mirror_root)
            result = {'outcome': 'deferred_cleanup', 'candidate_count': len(candidates),
                      'candidate_artifact_ids': [r['artifact_id'] for r in candidates], 'removed_count': 0}
            stamp = _now()
            conn.execute(
                "UPDATE jobs SET status='succeeded',result_json=?,last_error='',updated_at=?,heartbeat_at=?,finished_at=? WHERE job_id=?",
                (json.dumps(result), stamp, stamp, stamp, job_id),
            )
        return ()
