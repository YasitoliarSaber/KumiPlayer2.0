"""新资料代次的图种来源；分集来源只能是该分集在线对象。"""


def ensure_artwork_provenance(metadata: dict) -> None:
    if metadata.get('provider') != 'tmdb' or not metadata.get('provider_id'):
        return
    current = metadata.get('artwork_provenance') or {}
    provenance: dict = {'version': 1, 'roles': {}, 'episodes': {}}
    def entry(role, identifier, url, previous):
        if previous and previous.get('source_url') == url and previous.get('provider_object_id') == str(identifier):
            return dict(previous)
        return {'provider': 'tmdb', 'provider_object_id': str(identifier or ''), 'source_url': str(url or ''),
                'role': role, 'selection_reason': 'tmdb_role_selection' if url else 'not_available',
                'status': 'remote' if url and identifier else 'missing'}
    for role, key in (('poster', 'poster_url'), ('backdrop', 'fanart_url'), ('logo', 'clearlogo_url')):
        provenance['roles'][role] = entry(role, metadata['provider_id'], metadata.get(key), current.get('roles', {}).get(role))
    for mapping in metadata.get('episode_mappings') or []:
        episode = str(mapping.get('episode_id') or '')
        if episode:
            provenance['episodes'][episode] = entry('still', mapping.get('provider_episode_id'), mapping.get('still_url'),
                                                   current.get('episodes', {}).get(episode))
    metadata['artwork_provenance'] = provenance


def artwork_download_status(metadata: dict, role: str, digest: str, *, episode_id: str = '') -> None:
    provenance = metadata.get('artwork_provenance') or {}
    record = (provenance.get('episodes', {}).get(episode_id) if episode_id else provenance.get('roles', {}).get(role))
    if record:
        record['status'] = 'local_published' if digest else 'download_failed'
        if digest:
            record['digest'] = digest
