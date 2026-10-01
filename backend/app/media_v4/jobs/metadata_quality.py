"""正片在线资料门控；只评估当前成员，不修改已确认媒体事实。"""
from __future__ import annotations


def require_regular_episode_metadata(episodes: list[dict], metadata: dict) -> dict:
    result = dict(metadata)
    regular = {str(item['episode_id']) for item in episodes
               if item.get('episode_id') and item.get('episode_kind', 'regular') not in {'special', 'auxiliary', 'movie'}
               and item.get('season_kind', 'regular') != 'special'}
    mappings = {str(item.get('episode_id')): item for item in result.get('episode_mappings') or []
                if isinstance(item, dict) and str(item.get('provider_episode_id') or '').strip()}
    missing = regular - mappings.keys()
    missing_titles = {key for key in regular & mappings.keys() if not str(mappings[key].get('title') or '').strip()}
    missing_stills = {key for key in regular & mappings.keys() if not str(mappings[key].get('still_url') or '').strip()}
    result.update(episode_metadata_status='incomplete' if missing or missing_titles or missing_stills else 'complete',
                  missing_episode_title_count=len(missing_titles), missing_episode_still_count=len(missing_stills))
    if not (missing or missing_titles or missing_stills) or result.get('metadata_state') not in {'ready', 'confirmed'}:
        return result
    code = 'episode_mapping_incomplete' if missing else 'episode_details_incomplete'
    reason = (f'{len(missing)} 集正片未匹配在线资料，作品信息已保留，分集刮削尚未完成。' if missing else
              f'正片分集资料不完整：{len(missing_titles)} 集缺少标题、{len(missing_stills)} 集缺少在线缩略图。')
    result.update(
        metadata_state='failed', reason_code=code, reason=reason,
        failure_stage='episode_mapping', retryable=False, refresh_status='partial',
        refresh_reason_code=code,
    )
    result.pop('metadata_warning', None)
    return result


def current_episode_metadata(conn, revision_id: str | None, work_id: str, metadata: dict,
                             episode_ids: set[str] | None = None) -> dict:
    episodes = [dict(row) for row in conn.execute(
        'SELECT DISTINCT e.episode_id,e.episode_kind,s.season_kind FROM episodes e '
        'JOIN seasons s ON s.season_id=e.season_id '
        'JOIN revision_bindings rb ON rb.episode_id=e.episode_id '
        'JOIN import_revisions ir ON ir.revision_id=rb.revision_id '
        'JOIN source_roots sr ON sr.root_id=ir.root_id '
        "WHERE rb.work_id=? AND (? IS NULL OR rb.revision_id=?) AND ir.status='confirmed' AND sr.retired_at=''",
        (work_id, revision_id, revision_id),
    )]
    if episode_ids is not None:
        episodes = [item for item in episodes if str(item['episode_id']) in episode_ids]
    return require_regular_episode_metadata(episodes, metadata)
