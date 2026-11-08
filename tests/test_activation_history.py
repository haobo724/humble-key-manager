from unittest.mock import patch

import pytest

from humble_bundle_keys.key_tags import identities
from humble_bundle_keys.steam import atomic_json, key_digest
from humble_bundle_keys.web import Inventory


def fixture(tmp_path):
    inv = Inventory(tmp_path)
    inv.bind_humble('fixture-account')
    inv.steam.set_account('76561198000000001', 'Fixture')
    rows = [{'game_title': name, 'bundle_name': 'Fixture Bundle', 'platform': 'steam',
             'key': f'AAAAA-BBBBB-{index:05d}'} for index, name in enumerate(
                 ['Success', 'Used', 'Owned', 'Uncertain', 'Manual', 'Other account'])]
    for row, status in zip(rows, ['activated', 'duplicate', 'already_owned', 'uncertain'],
                           strict=False):
        inv.steam.record(row['key'], status)
    inv.steam.record(rows[-1]['key'], 'activated')
    inv.steam.data['results'][inv.steam.account()['steamid']][key_digest(rows[-1]['key'])][
        'humble_account'] = 'other-humble-account'
    inv.steam.save()
    identity, _ = identities(rows[4])
    inv.key_tags.data['fixture-account'] = {
        identity: {'tags': ['已激活', '保留'], 'exclude_activation': True}}
    atomic_json(inv.key_tags.path, inv.key_tags.data)
    inv.publish({'rows': rows, 'memberships': [], 'warnings': [], 'scanned_at': None})
    return inv


def test_history_preview_and_confirmed_update_persist_once(tmp_path):
    inv = fixture(tmp_path)
    before = inv.key_tags.path.read_bytes()
    plan = inv.view()['activation_migration']
    assert plan['counts'] == {'system': 1, 'duplicate': 1, 'manual': 1}
    assert {g['game'] for g in plan['games']} == {'Success', 'Used', 'Manual'}
    assert inv.key_tags.path.read_bytes() == before  # Reading the preview never applies it.
    assert inv.apply_activation_history(plan['plan_id'], plan['revision']) == {'updated': 3}
    restored = Inventory(tmp_path)
    assert restored.view()['activation_migration'] is None
    rows = {r['game_title']: r for r in restored.view()['rows']}
    assert rows['Success']['key_activated'] and not rows['Success']['manual_activated']
    assert rows['Used']['manual_activated'] and rows['Used']['steam_ownership'] != 'owned'
    assert not rows['Owned']['key_activated']
    assert '保留' in rows['Manual']['custom_tags'] and rows['Manual']['exclude_activation']


def test_history_rejects_stale_busy_and_changed_account_plans(tmp_path):
    inv = fixture(tmp_path)
    plan = inv.view()['activation_migration']
    inv.busy = True
    with pytest.raises(ValueError):
        inv.apply_activation_history(plan['plan_id'], plan['revision'])
    inv.busy = False
    inv.touch()
    with pytest.raises(ValueError):
        inv.apply_activation_history(plan['plan_id'], plan['revision'])
    inv.hb_activation.data['current_account'] = 'another-account'
    with pytest.raises(ValueError):
        inv.apply_activation_history(plan['plan_id'], inv.revision)


def test_history_save_failure_preserves_labels_and_revision(tmp_path):
    inv = fixture(tmp_path)
    plan = inv.view()['activation_migration']
    before = inv.key_tags.path.read_bytes()
    with patch('humble_bundle_keys.key_tags.atomic_json',
               side_effect=OSError('Fixture disk error')):
        with pytest.raises(OSError):
            inv.apply_activation_history(plan['plan_id'], plan['revision'])
    assert inv.key_tags.path.read_bytes() == before
    assert inv.view()['activation_migration'] == plan
