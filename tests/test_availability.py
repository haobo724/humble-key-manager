from humble_bundle_keys.web import Inventory


def test_marks_survive_restart_are_account_scoped_and_skip_expired(tmp_path):
    inv = Inventory(tmp_path)
    inv.bind_humble('synthetic-account')
    url = 'https://www.humblebundle.com/membership/april-2025'
    inv.snapshot['memberships'] = [{'url': url, 'bundle_name': 'April 2025 Humble Choice',
                                  'choice_policy': 'all_games', 'state': 'pending',
                                  'unclaimed_titles': ['Expired Game', 'Other Game']}]
    inv.availability.record('synthetic-account', 'april-2025', 'Expired Game', 'expired',
                            'Must be redeemed by Dec 31st, 2025 by 11:59 PM Pacific Time.')
    inv.publish(inv.snapshot)
    loaded = Inventory(tmp_path)
    view = loaded.view()
    assert view['memberships'][0]['unavailable_games']['Expired Game']['status'] == 'expired'
    plan = loaded.plan_months([{'url': url, 'all_games': True}], loaded.revision)
    assert plan['targets'][0]['titles'] == ['Other Game']
    assert not loaded.availability.games('other-account', url)
    loaded.log('month_key_recovered', month='april-2025', game='Expired Game')
    assert not loaded.availability.games('synthetic-account', url)


def test_shortage_without_order_entry_is_restored_and_survives_scan(tmp_path):
    inv = Inventory(tmp_path)
    inv.bind_humble('synthetic-account')
    url = 'https://www.humblebundle.com/membership/april-2024'
    month = {'url': url, 'bundle_name': 'April 2024 Humble Choice',
             'choice_policy': 'all_games', 'state': 'complete', 'unclaimed_titles': []}
    inv.availability.record('synthetic-account', 'april-2024', 'Missing Game',
                            'key_temporarily_exhausted')
    for _ in range(2):
        inv.publish({'rows': [], 'memberships': [month], 'warnings': [], 'scanned_at': None})
        view = inv.view()
        assert len(view['rows']) == 1
        row = view['rows'][0]
        assert row['game_title'] == 'Missing Game'
        assert row['status'] == 'unrevealed'
        assert row['unavailable_status'] == 'key_temporarily_exhausted'
        assert row['key'] == '' and row['platform'] == 'unknown'
        assert row['steam_app_id'] is None  # No platform or ownership guessed from logs.
    assert len(Inventory(tmp_path).view()['rows']) == 1
    inv.bind_humble('other-account')
    inv.publish({'rows': [], 'memberships': [month], 'warnings': [], 'scanned_at': None})
    assert not inv.view()['rows']


def test_real_key_replaces_shortage_placeholder_without_duplicate(tmp_path):
    inv = Inventory(tmp_path)
    inv.bind_humble('synthetic-account')
    month = {'url': 'https://www.humblebundle.com/membership/april-2024',
             'bundle_name': 'April 2024 Humble Choice', 'choice_policy': 'all_games',
             'state': 'complete', 'unclaimed_titles': []}
    inv.availability.record('synthetic-account', 'april-2024', 'Missing Game',
                            'key_temporarily_exhausted')
    inv.publish({'rows': [], 'memberships': [month], 'warnings': [], 'scanned_at': None})
    real = {**inv.snapshot['rows'][0], 'key': 'AAAAA-BBBBB-CCCCC', 'platform': 'steam'}
    real.pop('availability_restored')
    inv.snapshot['rows'].append(real)
    inv.publish(inv.snapshot)
    rows = inv.view()['rows']
    assert len(rows) == 1
    assert rows[0]['status'] == 'revealed'
    assert 'unavailable_status' not in rows[0]
    inv.log('month_key_recovered', month='april-2024', game='Missing Game')
    assert not inv.availability.games('synthetic-account', month['url'])
