"""多连接身份、凭据、缓存与旧配置兼容的离线合同。"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core import config as config_module
from app.core.config import AppConfig, load_config, save_config
from app.main import app


def create(client, name):
    response = client.post('/api/openlist/connections', json={'name': name})
    assert response.status_code == 200, response.text
    return response.json()['connection_id']


def save(client, connection_id, password='fixture-password', name=None):
    payload = dict(connection_id=connection_id, server_url='http://localhost:5244',
                   remote_root='/', mount_root='X:\\', username='fixture-user',
                   password=password, skip_verification=True)
    if name is not None:
        payload['name'] = name
    response = client.post('/api/openlist/config', json=payload)
    assert response.status_code == 200, response.text


def test_two_connections_keep_credentials_and_rename_identity():
    client = TestClient(app)
    a, b = create(client, '连接甲'), create(client, '连接乙')
    assert a != b
    save(client, a, 'fixture-a')
    save(client, b, 'fixture-b')
    from app.core.config import resolve_openlist_credentials
    assert resolve_openlist_credentials(a) == ('fixture-user', 'fixture-a', 'found')
    assert resolve_openlist_credentials(b) == ('fixture-user', 'fixture-b', 'found')
    save(client, a, '', '改名后的甲')
    data = load_config().to_public_dict()
    selected = next(c for c in data['openlist_connections'] if c['connection_id'] == a)
    assert selected['name'] == '改名后的甲'
    assert 'fixture-a' not in json.dumps(data)
    assert 'fixture-b' not in json.dumps(data)
    assert resolve_openlist_credentials(a)[1] == 'fixture-a'
    assert resolve_openlist_credentials(b)[1] == 'fixture-b'


def test_unknown_id_never_uses_legacy_credentials():
    save_config(AppConfig(openlist_server_url='http://localhost:5244',
                          openlist_username='old-user', openlist_password='old-password'))
    response = TestClient(app).get('/api/openlist/browse?connection_id=missing&cache_only=true')
    assert response.status_code == 404


def test_legacy_connection_is_preserved_without_migration():
    save_config(AppConfig(openlist_server_url='http://localhost:5244',
                          openlist_username='old-user', openlist_password='old-password'))
    client = TestClient(app)
    create(client, '新连接')
    assert load_config().openlist_password == 'old-password'
    listed = client.get('/api/openlist/connections').json()['connections']
    assert listed[0]['connection_id'] == 'legacy'
    from app.core.config import resolve_openlist_credentials
    assert resolve_openlist_credentials() == ('old-user', 'old-password', 'found')


def test_routes_are_owned_by_connection_and_cannot_be_transplanted():
    client = TestClient(app)
    a, b = create(client, '甲'), create(client, '乙')
    save(client, a)
    save(client, b)
    route = dict(remote_prefix='/Anime', label='动画', provider_id='other', enabled=True)
    response = client.put('/api/openlist/routes', json={'connection_id': a, 'routes': [route]})
    assert response.status_code == 200, response.text
    route = response.json()['routes'][0]
    assert client.get('/api/openlist/routes', params={'connection_id': b}).json()['routes'] == []
    transplanted = {key: route[key] for key in ('route_id', 'label', 'remote_prefix', 'provider_id', 'enabled')}
    assert client.put('/api/openlist/routes', json={'connection_id': b, 'routes': [transplanted]}).status_code == 400


def test_same_endpoint_accounts_have_separate_cache_and_root_identity():
    from app.integrations.openlist.cache import connection_key
    from app.media_v4.sources.scanner import openlist_root_id
    assert connection_key('http://localhost:5244', 'fixture-user', '/', connection_id='a') != connection_key(
        'http://localhost:5244', 'fixture-user', '/', connection_id='b')
    assert openlist_root_id('http://localhost:5244', 'fixture-user', '/', connection_id='a') != openlist_root_id(
        'http://localhost:5244', 'fixture-user', '/', connection_id='b')
    assert openlist_root_id('http://localhost:5244', 'fixture-user', '/') == openlist_root_id(
        'http://localhost:5244', 'fixture-user', '/', connection_id='legacy')


def test_clients_have_independent_tokens_but_share_account_budget():
    from app.integrations.openlist.client import clear_openlist_client_pool, get_openlist_client
    a = get_openlist_client('http://localhost:5244', 'fixture-user', 'fixture-password', connection_id='a')
    b = get_openlist_client('http://localhost:5244', 'fixture-user', 'fixture-password', connection_id='b')
    assert a is not b
    assert a._conn_key == b._conn_key
    a._token = 'fixture-token-a'
    assert b._token is None
    clear_openlist_client_pool(connection_id='a')
    assert get_openlist_client('http://localhost:5244', 'fixture-user', 'fixture-password', connection_id='b') is b


def test_durable_import_and_restarted_handler_keep_selected_connection(monkeypatch, tmp_path):
    from app.api import media_v4, openlist_v4
    from app.media_v4.persistence.database import V4Database
    from app.media_v4.projection.source_libraries import list_source_cards
    from app.media_v4.sources import scan_handlers, source_scan_runner
    database = V4Database(tmp_path / 'connections.db')
    database.initialize()
    monkeypatch.setattr(media_v4, 'get_database', lambda: database)
    monkeypatch.setattr(source_scan_runner, 'get_source_scan_runner', lambda _: SimpleNamespace(wake=lambda: None))
    client = TestClient(app)
    a, b = create(client, '甲'), create(client, '乙')
    save(client, a, 'fixture-a')
    save(client, b, 'fixture-b')
    roots = []
    for identity in (a, b):
        assert client.put('/api/openlist/routes', json={'connection_id': identity, 'routes': [
            dict(remote_prefix='/Anime', label='动画', provider_id='other', enabled=True),
        ]}).status_code == 200
        response = client.post('/api/v4/sources/scans', json={
            'source': 'openlist', 'root_path': '/Anime', 'scan_mode': 'full', 'connection_id': identity,
        })
        assert response.status_code == 200, response.text
        roots.append(response.json()['root_id'])
    assert roots[0] != roots[1]
    with database.connect() as conn:
        row = conn.execute('SELECT s.scan_id, r.request_json FROM source_scans s JOIN source_scan_requests r '
                           'ON r.scan_id = s.scan_id WHERE s.root_id = ?', (roots[1],)).fetchone()
    request = json.loads(row['request_json'])
    assert request['connection_id'] == b
    assert 'fixture-b' not in row['request_json'] and 'fixture-user' not in row['request_json']
    config_module.invalidate_config_cache()
    captured = []
    monkeypatch.setattr(openlist_v4, '_client', lambda config: captured.append(config) or object())
    monkeypatch.setattr(media_v4, 'scan_openlist_directory', lambda _client, **kw: (kw['scan_id'], []))
    runtime = SimpleNamespace(cancellation_requested=lambda: False, persist_evidence_batch=lambda _: None,
                              report_progress=lambda **_: None)
    task = SimpleNamespace(request=request, root_id=roots[1], scan_id=row['scan_id'])
    assert scan_handlers.scan_openlist_full_source(database, task, runtime) == []
    assert captured[0].openlist_password == 'fixture-b'
    assert captured[0]._openlist_connection_id == b
    # 追更沿用此来源连接，不受默认连接尚未配置的影响。
    from app.api import tracking_v4
    monkeypatch.setattr(media_v4, '_confirmed_source_evidence', lambda _: [object()])
    result = tracking_v4._enqueue_root_incremental(database, load_config(), root_id=roots[1], remote_root='/Anime')
    with database.connect() as conn:
        incremental = conn.execute('SELECT request_json FROM source_scan_requests WHERE scan_id = ?',
                                   (result['task_id'],)).fetchone()
    assert json.loads(incremental['request_json'])['connection_id'] == b
    cards = list_source_cards(database)
    assert {c['connection_id'] for c in cards} == {a, b}
    assert client.get('/api/v4/sources/openlist/status', params={
        'connection_id': b, 'remote_root': '/Anime',
    }).json()['root_id'] == roots[1]
    # 改名不影响登记任务；改账号/端点不能让旧任务请求新服务。
    save(client, b, '', '乙的新名称')
    assert scan_handlers.scan_openlist_full_source(database, task, runtime) == []
    selected = openlist_v4._connection_config(b)
    selected.openlist_server_url = 'http://localhost:5300'
    from app.core.openlist_connections import save_selected_connection
    save_selected_connection(selected)
    with pytest.raises(ValueError, match='映射已改变'):
        scan_handlers.scan_openlist_full_source(database, task, runtime)


def test_stale_connection_snapshot_never_receives_another_account_credentials():
    from fastapi import HTTPException

    from app.api import openlist_v4
    from app.core.openlist_connections import save_selected_connection
    client = TestClient(app)
    identity = create(client, '甲')
    save(client, identity)
    stale = openlist_v4._connection_config(identity)
    current = openlist_v4._connection_config(identity)
    current.openlist_username = 'other-fixture-user'
    save_selected_connection(current)
    with pytest.raises(HTTPException) as error:
        openlist_v4._saved_credentials(stale)
    assert error.value.status_code == 409


def test_new_connection_credentials_fail_closed_when_secure_store_unavailable(monkeypatch, tmp_path):
    from app.core.credential_store import CredentialStoreError
    from app.core.openlist_connections import OpenListConnectionConfig
    target = tmp_path / 'unavailable.json'
    monkeypatch.setattr(config_module, 'CONFIG_FILE', None)
    monkeypatch.setattr(config_module, 'get_config_file', lambda: target)
    monkeypatch.setattr(config_module, '_credential_storage_enabled', lambda: False)
    candidate = AppConfig(openlist_connections=[OpenListConnectionConfig(
        'a', openlist_username='fixture-user', openlist_password='fixture-secret')])
    with pytest.raises(CredentialStoreError):
        save_config(candidate)
    assert not target.exists()


def test_secure_store_rolls_back_only_changed_connection_on_json_failure(monkeypatch, tmp_path):
    from app.core.openlist_connections import OpenListConnectionConfig
    values = {'openlist:a:username': 'fixture-user', 'openlist:a:password': 'old-a',
              'openlist:b:username': 'fixture-b', 'openlist:b:password': 'old-b'}
    original = dict(values)
    store = SimpleNamespace(read=lambda key: values.get(key, ''),
                            write=lambda key, value: values.update({key: value}),
                            delete=lambda key: values.pop(key, None))
    monkeypatch.setattr(config_module, 'CONFIG_FILE', tmp_path / 'rollback.json')
    monkeypatch.setattr(config_module, 'SECURE_CREDENTIAL_STORE', store)
    monkeypatch.setattr(config_module, '_credential_storage_enabled', lambda: True)
    def fail_json(*_):
        raise OSError('fixture atomic write failure')
    monkeypatch.setattr(config_module, 'write_json_atomic', fail_json)
    candidate = AppConfig(openlist_connections=[OpenListConnectionConfig(
        'a', openlist_username='fixture-user', openlist_password='new-a'), OpenListConnectionConfig('b')])
    with pytest.raises(OSError):
        save_config(candidate)
    assert values == original


def test_secure_store_is_per_connection_and_json_never_contains_credentials(monkeypatch, tmp_path):
    values = {}
    store = SimpleNamespace(available=True, values=values,
                            read=lambda key: values.get(key, ''),
                            write=lambda key, value: values.update({key: value}),
                            delete=lambda key: values.pop(key, None))
    path = tmp_path / 'config.json'
    monkeypatch.setattr(config_module, 'CONFIG_FILE', path)
    monkeypatch.setattr(config_module, 'SECURE_CREDENTIAL_STORE', store)
    monkeypatch.setattr(config_module, '_credential_storage_enabled', lambda: True)
    config_module.invalidate_config_cache()
    client = TestClient(app)
    a, b = create(client, '甲'), create(client, '乙')
    save(client, a, 'fixture-a')
    save(client, b, 'fixture-b')
    serialized = path.read_text(encoding='utf-8')
    assert 'fixture-a' not in serialized and 'fixture-b' not in serialized and 'fixture-user' not in serialized
    config_module.invalidate_config_cache()
    from app.core.config import resolve_openlist_credentials
    assert resolve_openlist_credentials(a)[1] == 'fixture-a'
    assert resolve_openlist_credentials(b)[1] == 'fixture-b'
    assert len([k for k in store.values if k.startswith('openlist:')]) == 4


@pytest.fixture(autouse=True)
def forbid_mount_probe(monkeypatch):
    # 路由展示只验证字符串映射，测试不得探测真实挂载盘。
    probe = SimpleNamespace(is_dir=lambda: False)
    probe.expanduser = lambda: probe
    monkeypatch.setattr('app.api.openlist_v4.Path', lambda _: probe)
