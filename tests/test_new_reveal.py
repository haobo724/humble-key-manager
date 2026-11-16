import json
from unittest.mock import MagicMock

import pytest

from humble_bundle_keys.api import ApiUnsupported
from humble_bundle_keys.new_reveal import NewSteamRevealer


def fixture(*, key='', quota=False, multiple=False):
    entitlement = {'id': 'fixture-entitlement', 'type': 'steam', 'keyIntegrationId': 'steam',
                   'redemptionData': {'keyFormatted': key}, 'redeemable': True,
                   'outOfStock': False, 'expired': False, 'redeemedAt': None}
    keys = [entitlement, {**entitlement, 'id': 'other'}] if multiple else [entitlement]
    root = {'userLibraryItem': {'id': 'detail-wrapper', 'entitlements': {'keys': keys}}}
    if quota:
        root['group'] = {'maxRedemptions': 1}
    stream = '1:' + json.dumps(root) + '\n'
    html = ('<script>self.__next_f.push(' + json.dumps([1, stream]) + ')</script>'
            '<script src="/_next/static/chunks/redeem.js"></script>')
    context = MagicMock()

    def get(url, **kwargs):
        response = MagicMock(status=200)
        response.text.return_value = (html if '/content/' in url else
                                      f'createServerReference)("{"b" * 42}",x,x,x,'
                                      '"redeemEntitlementAction")')
        return response

    context.request.get.side_effect = get
    row = {'modern_item_id': 'fixture-item', 'modern_entitlement_id': 'fixture-entitlement'}
    return context, row


@pytest.mark.parametrize('quota,multiple', [(True, False), (False, True)])
def test_choice_groups_never_submit_a_redemption(quota, multiple):
    context, row = fixture(quota=quota, multiple=multiple)
    with pytest.raises(ApiUnsupported):
        NewSteamRevealer(context).reveal(row)
    context.request.post.assert_not_called()


def test_existing_key_is_read_without_another_redemption():
    context, row = fixture(key='AAAAA-BBBBB-CCCCC')
    result = NewSteamRevealer(context).reveal(row)
    assert result['status'] == 'recovered' and result['key'] == 'AAAAA-BBBBB-CCCCC'
    context.request.post.assert_not_called()


def test_standalone_key_uses_exact_entitlement_and_no_mutation_retries():
    context, row = fixture()
    response = MagicMock(status=200)
    response.text.return_value = '0:{"a":"$@1"}\n1:{"success":true,"key":"AAAAA-BBBBB-CCCCC"}\n'
    context.request.post.return_value = response
    result = NewSteamRevealer(context).reveal(row)
    assert result['status'] == 'success'
    assert json.loads(context.request.post.call_args.kwargs['data']) == [
        'fixture-entitlement', 'steam', '$undefined']
    context.request.post.assert_called_once()
    context.request.post.reset_mock()
    context.request.post.side_effect = TimeoutError('Fixture timeout')
    assert NewSteamRevealer(context).reveal(row)['status'] == 'uncertain'
    context.request.post.assert_called_once()
