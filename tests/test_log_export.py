import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from humble_bundle_keys.log_export import export_logs
from humble_bundle_keys.web import Inventory, make_handler


def test_export_redacts_secrets_and_keeps_debug_context(tmp_path):
    secrets = ['AAAAA-BBBBB-CCCCC', 'opaque-api-secret', 'opaque-cookie-secret',
               'private-order-ref', 'user@example.com', '76561198000000000']
    (tmp_path / 'inventory.json').write_text(json.dumps({'rows': [
        {'key': secrets[0], 'humble_url': 'https://www.humblebundle.com/?key=' + secrets[3]}]}))
    (tmp_path / 'itad-settings.json').write_text(json.dumps({'api_key': secrets[1]}))
    (tmp_path / 'storage_state.json').write_text(json.dumps({
        'cookies': [{'name': 'session', 'value': secrets[2]}]}))
    event = {'event': 'operation_failed', 'bundle': 'April Fixture Choice', 'game': 'Fixture Game',
             'phase': 'API 领取并刮取', 'error_type': 'TypeError',
             'operation_id': 'fixture-operation',
             'error': ' '.join(secrets) + '\nhttps://private.example/order\nC:\\Users\\Private\\data',
             'locations': [{'file': 'C:\\Users\\Private\\web.py', 'function': 'publish',
                            'line': 42, 'locals': {'key': secrets[0]}}],
             'cookies': secrets[2], 'account': 'Private Account', 'response': {'key': secrets[0]}}
    (tmp_path / 'scan.log').write_text(json.dumps(event) + '\n', encoding='utf-8')
    exported = export_logs(tmp_path)
    text = json.dumps(exported)
    for secret in secrets + ['private.example', 'Private', 'response', 'cookies', 'locals']:
        assert secret not in text
    safe = exported['events'][0]
    assert safe['bundle'] == event['bundle'] and safe['game'] == event['game']
    assert safe['phase'] == event['phase'] and safe['error_type'] == 'TypeError'
    assert safe['locations'] == [{'file': 'web.py', 'function': 'publish', 'line': 42}]


def test_export_reads_rotated_logs_and_skips_malformed_payload(tmp_path):
    for name, event in [('scan.log.2', 'old'), ('scan.log.1', 'previous'), ('scan.log', 'current')]:
        (tmp_path / name).write_text(json.dumps({'event': event}) + '\n', encoding='utf-8')
    with (tmp_path / 'scan.log').open('a') as file:
        file.write('raw secret payload that is not JSON\n')
    exported = export_logs(tmp_path)
    assert [e['event'] for e in exported['events']] == ['old', 'previous', 'current']
    assert exported['malformed_lines_skipped'] == 1
    assert 'raw secret' not in json.dumps(exported)


def test_export_endpoint_requires_local_token(tmp_path):
    inventory = Inventory(tmp_path)
    inventory.log('reveal_failed', bundle='Fixture Bundle', game='Fixture Game',
                  error_type='TypeError')
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(inventory, 'fixture-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/api/logs-export'
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(url, timeout=5)
        request = urllib.request.Request(url, headers={'X-Local-Token': 'fixture-token'})
        with urllib.request.urlopen(request, timeout=5) as response:
            exported = json.load(response)
        assert exported['events'][0]['game'] == 'Fixture Game'
        assert exported['format'] == 'humble-key-manager-debug-v1'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
