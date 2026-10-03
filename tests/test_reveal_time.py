import copy

from humble_bundle_keys.deadlines import deadline_info
from humble_bundle_keys.month_claim import save_modal_key
from humble_bundle_keys.web import Inventory


def test_reveal_time_survives_rescan_restart_and_repeat(tmp_path):
    inventory = Inventory(tmp_path)
    order = {'gamekey': 'synthetic', 'product': {'human_name': 'Synthetic Bundle'},
             'tpkd_dict': {'all_tpks': [{'human_name': 'Synthetic', 'key_type': 'steam'}]}}
    save_modal_key(inventory, order, 'Synthetic', 'AAAAA-BBBBB-CCCCC')
    timestamp = inventory.snapshot['rows'][0]['revealed_at']
    rescanned = copy.deepcopy(inventory.snapshot)
    del rescanned['rows'][0]['revealed_at']
    inventory.publish(rescanned)
    restarted = Inventory(tmp_path)
    assert restarted.snapshot['rows'][0]['revealed_at'] == timestamp
    save_modal_key(restarted, order, 'Synthetic', 'AAAAA-BBBBB-CCCCC')
    assert restarted.snapshot['rows'][0]['revealed_at'] == timestamp
    unknown = copy.deepcopy(restarted.snapshot)
    unknown['rows'][0]['key'] = 'DDDDD-EEEEE-FFFFF'
    unknown['rows'][0].pop('revealed_at')
    restarted.publish(unknown)
    assert not restarted.snapshot['rows'][0].get('revealed_at')


def test_deadline_sort_values_cover_formats_and_unknown_dates():
    early = deadline_info('Must be redeemed by January 2, 2025')['deadline_at']
    late = deadline_info('Must be redeemed by 2026-03-04')['deadline_at']
    assert early < late
    assert deadline_info('2026-03-04T10:00:00+02:00')['deadline_at'].endswith('+02:00')
    assert deadline_info('Expired')['deadline_at'] is None
    assert deadline_info('')['deadline_at'] is None
