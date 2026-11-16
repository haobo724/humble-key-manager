import copy
import json
import threading
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from humble_bundle_keys.api import ApiUnsupported
from humble_bundle_keys.new_groups import (
    claim_group,
    membership,
    parse_group,
    validate_selection,
)
from humble_bundle_keys.web import Inventory, make_handler

URL = 'https://www.humblebundle.com/home/library/group/fixture-group'


def document(*, maximum=1, used=(), stock=(), providers=('steam', 'gog', 'epic_keyless')):
    keys, options = [], []
    for index, provider in enumerate(providers):
        identity = f'key-{index}'
        redeemed = identity in used
        keys.append({'id': identity, 'title': 'Game A' if index < 2 else 'Game B',
                     'subTitle': 'Fixture Bundle', 'type': provider, 'keyIntegrationId': provider,
                     'redemptionData': {'keyFormatted': 'AAAAA-BBBBB-CCCCC' if redeemed else ''},
                     'redeemedAt': '2026-11-16T00:00:00Z' if redeemed else None,
                     'redeemable': True, 'expired': False, 'outOfStock': identity in stock})
        options.append({'entitlementId': identity, 'keyIntegrationId': provider,
                        'eligible': True, 'redeemed': redeemed, 'outOfStock': identity in stock})
    root = {'userLibraryItem': {'title': 'Fixture Bundle', 'entitlements': {
        'keys': keys, 'redemptionGroups': [{'groupId': 'quota', 'title': 'Choose platforms',
                                          'maxRedemptions': maximum, 'options': options}]}}}
    stream = '1:' + json.dumps(root) + '\n'
    return '<script>self.__next_f.push(' + json.dumps([1, stream]) + ')</script>'


def inventory(tmp_path, model):
    inv = Inventory(tmp_path)
    inv.bind_humble('fixture-account')
    inv.snapshot['memberships'] = [membership(model)]
    inv.group_detail = {**model, 'detail_id': 'fixture-detail', 'revision': inv.revision}
    return inv


def test_options_show_games_platforms_stock_and_quota_without_keys():
    model, _ = parse_group(document(maximum=2, used=('key-0',), stock=('key-1',)), URL)
    assert model['groups'][0]['remaining'] == 1
    options = {o['id']: o for o in model['options']}
    assert options['key-0']['redeemed'] and not options['key-0']['eligible']
    assert options['key-1']['reason'] == 'Key 暂时缺货'
    assert '官网' in options['key-2']['reason']
    assert 'AAAAA' not in json.dumps(model)


def test_platform_selection_counts_against_each_quota():
    model, _ = parse_group(document(), URL)
    options, quotas = validate_selection(model, ['key-1'])
    assert options[0]['platform'] == 'gog' and quotas[0]['after'] == 0
    for bad in (['key-0', 'key-1'], ['key-0', 'key-0'], ['unknown'], ['key-2'], []):
        with pytest.raises(ValueError):
            validate_selection(model, bad)


def test_unknown_quota_never_defaults_to_unlimited():
    with pytest.raises(ApiUnsupported):
        parse_group(document(maximum=None), URL)
    with pytest.raises(ValueError):
        parse_group(document(), 'https://other.test/group/fixture')
    unknown, _ = parse_group(document().replace('redemptionGroups', 'missingGroups'), URL)
    assert all(not option['eligible'] for option in unknown['options'])


def test_preview_is_local_and_bound_to_revision_account_and_options(tmp_path):
    model, _ = parse_group(document(), URL)
    inv = inventory(tmp_path, model)
    plan = inv.plan_group('fixture-detail', ['key-0'], inv.revision)
    assert plan['options'][0]['platform'] == 'steam' and plan['quotas'][0]['selected'] == 1
    assert 'AAAAA' not in json.dumps(plan)
    inv.touch()
    with pytest.raises(ValueError):
        inv.plan_group('fixture-detail', ['key-0'], plan['revision'])
    with pytest.raises(ValueError):
        inv.group_url_allowed('https://www.humblebundle.com/home/library/group/not-owned')


def test_changed_quota_before_execution_submits_nothing(tmp_path):
    model, _ = parse_group(document(maximum=2), URL)
    inv = inventory(tmp_path, model)
    inv.plan_group('fixture-detail', ['key-0'], inv.revision)
    changed, changed_keys = parse_group(document(maximum=1), URL)
    with patch('humble_bundle_keys.new_groups.NewGroupClient') as client:
        client.return_value.load.return_value = (changed, changed_keys, '')
        with pytest.raises(ValueError, match='已变化'):
            claim_group(None, inv, inv.group_plan)
        client.return_value.submit.assert_not_called()


def test_success_is_saved_before_recheck_and_external_change_stops_remaining(tmp_path):
    model, keys = parse_group(document(maximum=2, providers=('steam', 'gog')), URL)
    inv = inventory(tmp_path, model)
    inv.plan_group('fixture-detail', ['key-0', 'key-1'], inv.revision)
    # An additional outside selection appears alongside our first success.
    after, after_keys = parse_group(document(maximum=2, used=('key-0', 'key-1'),
                                             providers=('steam', 'gog')), URL)
    with patch('humble_bundle_keys.new_groups.NewGroupClient') as factory:
        client = factory.return_value
        client.load.side_effect = [(model, copy.deepcopy(keys), ''),
                                   (model, copy.deepcopy(keys), ''), (after, after_keys, '')]
        client.submit.return_value = {'status': 'success', 'key': 'AAAAA-BBBBB-CCCCC'}
        claim_group(None, inv, inv.group_plan)
        client.submit.assert_called_once_with('key-0', 'steam', URL)
    assert any(r.get('key') == 'AAAAA-BBBBB-CCCCC' for r in inv.snapshot['rows'])
    assert inv.report.view()['remaining'] == 1 and inv.report.view()['issues']


def test_uncertain_result_only_reads_back_and_never_submits_again(tmp_path):
    model, keys = parse_group(document(maximum=2, providers=('steam', 'gog')), URL)
    inv = inventory(tmp_path, model)
    inv.plan_group('fixture-detail', ['key-0', 'key-1'], inv.revision)
    with patch('humble_bundle_keys.new_groups.NewGroupClient') as factory:
        client = factory.return_value
        client.load.return_value = (model, keys, '')
        client.submit.return_value = {'status': 'uncertain'}
        claim_group(None, inv, inv.group_plan)
        client.submit.assert_called_once()
    assert inv.report.view()['failed'] == 1 and inv.report.view()['remaining'] == 1


def test_successful_platform_selections_follow_live_remaining_quota(tmp_path):
    model, keys = parse_group(document(maximum=2, providers=('steam', 'gog')), URL)
    once, once_keys = parse_group(document(maximum=2, used=('key-0',),
                                           providers=('steam', 'gog')), URL)
    after, after_keys = parse_group(document(maximum=2, used=('key-0', 'key-1'),
                                             providers=('steam', 'gog')), URL)
    inv = inventory(tmp_path, model)
    inv.plan_group('fixture-detail', ['key-0', 'key-1'], inv.revision)
    with patch('humble_bundle_keys.new_groups.NewGroupClient') as factory, \
            patch.object(inv.stop_event, 'wait', return_value=False):
        client = factory.return_value
        client.load.side_effect = [(model, copy.deepcopy(keys), ''),
                                   (model, copy.deepcopy(keys), ''),
                                   (once, copy.deepcopy(once_keys), ''),
                                   (once, copy.deepcopy(once_keys), ''), (after, after_keys, '')]
        client.submit.side_effect = [{'status': 'success', 'key': 'AAAAA-BBBBB-CCCCC'},
                                     {'status': 'success', 'key': 'DDDDD-EEEEE-FFFFF'}]
        claim_group(None, inv, inv.group_plan)
        assert [call.args[1] for call in client.submit.call_args_list] == ['steam', 'gog']
    assert inv.report.view()['succeeded'] == 2
    assert inv.snapshot['memberships'][0]['redemption_groups'][0]['remaining'] == 0


def test_key_is_durable_even_when_quota_recheck_fails(tmp_path):
    model, keys = parse_group(document(maximum=2, providers=('steam', 'gog')), URL)
    inv = inventory(tmp_path, model)
    inv.plan_group('fixture-detail', ['key-0', 'key-1'], inv.revision)
    with patch('humble_bundle_keys.new_groups.NewGroupClient') as factory:
        client = factory.return_value
        client.load.side_effect = [(model, keys, ''), (model, keys, ''), TimeoutError('fixture')]
        client.submit.return_value = {'status': 'success', 'key': 'AAAAA-BBBBB-CCCCC'}
        claim_group(None, inv, inv.group_plan)
        client.submit.assert_called_once()
    restored = Inventory(tmp_path)
    assert any(row.get('key') == 'AAAAA-BBBBB-CCCCC' for row in restored.snapshot['rows'])
    assert restored.snapshot['memberships'][0]['redemption_groups'][0]['remaining'] == 1


def test_group_claim_api_requires_preview_and_explicit_confirmation(tmp_path):
    model, _ = parse_group(document(), URL)
    inv = inventory(tmp_path, model)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(inv, 'fixture-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post(path, body, token='fixture-token'):
        request = Request(f'http://127.0.0.1:{server.server_port}' + path,
                          data=json.dumps(body).encode(), headers={'X-Local-Token': token})
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    try:
        with pytest.raises(HTTPError):
            post('/api/group-preview', {'revision': inv.revision}, token='wrong')
        plan = post('/api/group-preview', {'detail_id': 'fixture-detail',
                                         'selected_ids': ['key-1'], 'revision': inv.revision})
        body = {'plan_id': plan['plan_id'], 'revision': plan['revision']}
        with patch.object(inv, 'start', return_value=True) as start:
            with pytest.raises(HTTPError):
                post('/api/group-claim', body)
            start.assert_not_called()
            post('/api/group-claim', {**body, 'confirm_claim': True})
            assert start.call_args.args[2]['selected_ids'] == ['key-1']
            assert start.call_args.args[2]['account'] == 'fixture-account'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
