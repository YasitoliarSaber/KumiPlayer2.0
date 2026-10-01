"""可丢弃的名称证据缓存，复用 v4_meta 命名空间，不创建媒体事实或绑定。"""

import hashlib
import json
import time
import unicodedata

from app.scrape.alias_contract import AliasEvidence, clean_aliases

_PREFIX = 'alias-cache-v1:'
_STRATEGY = 'names-only-2'


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()


def _key(target, provider) -> str:
    def title(raw):
        return ' '.join(unicodedata.normalize('NFKC', str(raw or '')).casefold().split())
    facts = {
        'titles': sorted({title(t) for t in [target.get('preferred_title'), target.get('original_title'),
                                           *(target.get('identity_titles') or [])] if t}),
        'year': target.get('year'), 'type': target.get('work_type'), 'show_type': target.get('show_type'),
        'provider': provider, 'api_strategy': _STRATEGY,
        'seasons': sorted({str(e.get('local_season_number')) + ':' + str(e.get('season_kind') or '')
                           for e in target.get('episodes') or []}),
        'hint_conflict': bool(target.get('provider_hint_conflict')),
    }
    return _PREFIX + _digest(facts)


class RecoveryCache:
    def __init__(self, database, *, now=time.time):
        self.database = database
        self.now = now

    def get(self, target, provider) -> dict | None:
        with self.database.connect() as conn:
            row = conn.execute('SELECT value FROM v4_meta WHERE key=?', (_key(target, provider),)).fetchone()
        try:
            data = json.loads(row['value']) if row else None
            if (not data or data['expires_at'] <= self.now()
                    or data['evidence_digest'] != _digest(data.get('evidence') or [])):
                return None
            return data
        except (KeyError, TypeError, ValueError):
            return None

    def put(self, target, provider, evidence: list[AliasEvidence]) -> None:
        accepted = clean_aliases([e.title for e in evidence], limit=6)
        unique = {e.title: e.to_dict() for e in evidence if e.title in accepted}
        values = list(unique.values())[:6]
        self._write(target, provider, {'status': 'positive' if values else 'empty', 'evidence': values},
                    7 * 86400 if values else 3600)

    def put_error(self, target, provider, reason_code: str, seconds: int = 60) -> None:
        self._write(target, provider, {'status': 'cooldown', 'evidence': [], 'reason_code': reason_code},
                    max(1, min(int(seconds), 86400)))

    def _write(self, target, provider, payload, ttl) -> None:
        payload = {**payload, 'expires_at': self.now() + ttl,
                   'evidence_digest': _digest(payload.get('evidence') or [])}
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(serialized.encode('utf-8')) > 65536:
            return
        with self.database.connect() as conn:
            conn.execute('INSERT OR REPLACE INTO v4_meta(key,value) VALUES (?,?)', (_key(target, provider), serialized))
            # 限制缓存体积；按 expiry 淘汰只影响此命名空间，不触碰快照和媒体事实。
            rows = conn.execute('SELECT key,value FROM v4_meta WHERE key LIKE ?', (_PREFIX + '%',)).fetchall()
            if len(rows) > 512:
                def expiry(row):
                    try:
                        return json.loads(row['value']).get('expires_at', 0)
                    except (TypeError, ValueError):
                        return 0
                victims = sorted(rows, key=expiry)[:len(rows) - 512]
                conn.executemany('DELETE FROM v4_meta WHERE key=?', [(r['key'],) for r in victims])

    def clear(self) -> None:
        with self.database.connect() as conn:
            conn.execute('DELETE FROM v4_meta WHERE key LIKE ?', (_PREFIX + '%',))
