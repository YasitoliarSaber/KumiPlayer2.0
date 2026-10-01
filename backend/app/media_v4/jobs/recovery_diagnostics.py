"""执行页名称恢复的受限摘要，不透传 job 原始结果、路径或响应正文。"""

import json

from app.scrape.alias_contract import clean_aliases

_PROVIDERS = {'tmdb': 'TMDB', 'anilist_names': 'AniList', 'bangumi_names': 'Bangumi', 'web_names': '联网名称核对'}
_REASONS = {'no_candidates', 'ambiguous_candidates', 'provider_timeout', 'provider_network_error',
            'provider_unavailable', 'provider_unauthorized', 'provider_rate_limited', 'provider_invalid_response'}


def name_recovery_summary(job) -> dict | None:
    if not job or job.get('job_type') != 'recover_work_aliases':
        return None
    try:
        payload = json.loads(job.get('result_json') or '{}')
    except (ValueError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    trace = payload.get('alias_recovery_trace') or []
    if not isinstance(trace, list):
        trace = []
    steps = []
    for item in trace[:8]:
        if not isinstance(item, dict) or item.get('provider') not in _PROVIDERS:
            continue
        evidence = item.get('evidence') or []
        if not isinstance(evidence, list):
            evidence = []
        steps.append({'provider': _PROVIDERS[item['provider']],
                      'status': item.get('status') if item.get('status') in {'completed', 'unavailable'} else 'retry',
                      'reason_code': item.get('reason_code') if item.get('reason_code') in _REASONS else '',
                      'cache_status': item.get('cache_status') if item.get('cache_status') in {'hit', 'miss'} else '',
                      'aliases': clean_aliases([e.get('title') for e in evidence[:6] if isinstance(e, dict)], limit=4)})
    status = job.get('status')
    return {'status': status if status in {'queued', 'running', 'succeeded', 'failed', 'cancelled'} else '', 'steps': steps}
