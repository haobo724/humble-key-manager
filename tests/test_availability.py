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
