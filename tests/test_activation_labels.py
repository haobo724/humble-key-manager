from unittest.mock import MagicMock, patch

import pytest

from humble_bundle_keys.steam import activate_batch, row_id
from humble_bundle_keys.web import Inventory


@pytest.mark.parametrize('response,status,tagged,owned,manual,system,include_owned', [
    ({'success': 1}, 'activated', True, True, False, True, False),
    ({'purchase_result_details': 15}, 'duplicate', True, False, True, False, False),
    ({'purchase_result_details': 15}, 'duplicate', True, True, True, False, True),
    ({'purchase_result_details': 9}, 'already_owned', False, True, False, False, True),
])
def test_batch_activation_tags_persist_and_keep_ownership_separate(
        tmp_path, response, status, tagged, owned, manual, system, include_owned):
    inv = Inventory(tmp_path)
    row = {'game_title': 'Fixture Game', 'bundle_name': 'Fixture Bundle', 'platform': 'steam',
           'humble_url': 'https://www.humblebundle.com/membership/april-2024',
           'key': 'AAAAA-BBBBB-CCCCC', 'steam_app_id': 620}
    inv.snapshot['rows'] = [row]
    inv.bind_humble('fixture-account')
    inv.steam.set_account('76561198000000001', 'Fixture')
    inv.steam.set_library([620] if include_owned else [])
    inv.key_tags.set('fixture-account', [row], ['留作纪念'], False)
    context, page = MagicMock(), MagicMock()
    context.cookies.return_value = [{'name': 'sessionid', 'value': 'fixture-session'}]
    with patch('humble_bundle_keys.steam.account_from_context', return_value=inv.steam.account()), \
            patch('humble_bundle_keys.steam.submit_key', return_value={'status': 200,
                                                                      'data': response}), \
            patch.object(inv.stop_event, 'wait', return_value=False):
        assert activate_batch(page, context, inv.steam, inv.tagged_rows(), [row_id(row)],
                              inv, include_owned=include_owned) == 'completed'
    restored = Inventory(tmp_path)
    restored.snapshot['rows'] = [row]
    current = restored.view()['rows'][0]
    assert current['activation_status'] == status
    assert current['key_activated'] == tagged
    assert ('已激活' in current['custom_tags']) == tagged
    assert '留作纪念' in current['custom_tags']
    assert current['steam_ownership'] == ('owned' if owned else 'not_owned')
    assert current['manual_activated'] == manual
    assert current['system_activated'] == system
    assert current['activation_excluded'] == tagged


def test_historical_receipts_get_common_tag_without_changing_ownership(tmp_path):
    inv = Inventory(tmp_path)
    inv.bind_humble('fixture-account')
    inv.steam.set_account('76561198000000001', 'Fixture')
    inv.steam.set_library([])
    for status in ('activated', 'duplicate'):
        row = {'game_title': status, 'platform': 'steam', 'key': 'AAAAA-BBBBB-CCCCC',
               'steam_app_id': 620}
        inv.snapshot['rows'] = [row]
        inv.steam.record(row['key'], status)
        current = inv.view()['rows'][0]
        assert current['key_activated'] and '已激活' in current['custom_tags']
        assert current['manual_activated'] == (status == 'duplicate')


def test_task_failure_keeps_completion_status_and_message(tmp_path):
    inv = Inventory(tmp_path)
    with patch('humble_bundle_keys.web.threading.Thread'):
        assert inv.start('reveal')
    identity = inv.task['id']
    with patch('humble_bundle_keys.web.sync_playwright'), \
            patch('humble_bundle_keys.web.get_authenticated_context',
                  side_effect=RuntimeError('Fixture login failed')):
        inv.run('reveal')
    task = inv.view()['task']
    assert task['id'] == identity and task['status'] == 'failed'
    assert not inv.busy and 'Fixture login failed' in task['message']


def test_persisted_system_tag_still_filters_without_steam_session(tmp_path):
    inv = Inventory(tmp_path)
    inv.bind_humble('fixture-account')
    row = {'game_title': 'Fixture Game', 'platform': 'steam', 'key': 'AAAAA-BBBBB-CCCCC'}
    inv.key_tags.mark_activated('fixture-account', row, source='system')
    restored = Inventory(tmp_path)
    restored.snapshot['rows'] = [row]
    current = restored.view()['rows'][0]
    assert current['key_activated'] and '已激活' in current['custom_tags']
    assert not current['manual_activated']
    assert current['steam_ownership'] == 'unknown'
