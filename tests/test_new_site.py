import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from humble_bundle_keys.api import ApiUnsupported
from humble_bundle_keys.auth import (
    AuthError,
    AuthOptions,
    _is_authenticated,
    get_authenticated_context,
)
from humble_bundle_keys.new_library import (
    NewLibraryScanner,
    is_library_page_response,
    merge_rows,
    rows_from_item,
)
from humble_bundle_keys.site_session import (
    account_email,
    flight_records,
    library_action,
    script_urls,
)

ACTION = 'a' * 42


def document():
    user = {'id': 'fixture-user', 'email': 'fixture@example.test', 'emailVerified': True}
    stream = '1:' + json.dumps(user) + '\n'
    return ('<script>self.__next_f.push(' + json.dumps([1, stream]) + ')</script>'
            '<script src="/_next/static/chunks/fixture.js"></script>')


def item(identity='fixture', key='AAAAA-BBBBB-CCCCC'):
    return {'id': identity, 'title': 'Fixture Game', 'subTitle': 'Fixture Bundle',
            'itemType': 'content-entitlement', 'entitlements': {'keys': [
                {'id': identity + '-key', 'type': 'steam', 'redemptionData': {'keyFormatted': key},
                 'redeemedAt': '2026-11-01T00:00:00Z' if key else None}]}}


def test_flight_parser_and_account_identity_never_execute_scripts():
    assert account_email(document()) == 'fixture@example.test'
    records = flight_records('1:{"x":"$undefined","time":"$D2026-11-01T00:00:00Z"}\n')
    assert records == [{'x': None, 'time': '2026-11-01T00:00:00Z'}]
    assert script_urls(document() + '<script src="https://other.test/x.js"></script>') == [
        'https://www.humblebundle.com/_next/static/chunks/fixture.js']
    assert library_action(f'createServerReference)("{ACTION}",x,x,x,"filterUserLibrary")') == ACTION
    assert library_action(
        f'createServerReference)("{ACTION}",x,x,x,"redeemEntitlementAction")') is None
    with pytest.raises(ValueError):
        account_email('<title>someone@example.test</title>')


def test_library_shell_does_not_prove_authentication():
    page = MagicMock()
    page.url = 'https://www.humblebundle.com/home/library'
    response = page.context.request.get.return_value
    response.status = 200
    response.json.return_value = {'status': 'anonymous'}
    assert not _is_authenticated(page)
    response.json.return_value = {'status': 'authenticated'}
    assert _is_authenticated(page)
    response.dispose.assert_called()


def test_cancelled_new_login_keeps_previous_session(tmp_path, monkeypatch):
    monkeypatch.delenv('HUMBLE_SESSION_COOKIE', raising=False)
    state = tmp_path / 'storage_state.json'
    state.write_text('previous session', encoding='utf-8')
    with patch('humble_bundle_keys.auth._interactive_login', side_effect=AuthError('cancelled')):
        with pytest.raises(AuthError):
            get_authenticated_context(MagicMock(), AuthOptions(storage_state_path=state,
                                                              force_login=True))
    assert state.read_text(encoding='utf-8') == 'previous session'


def test_merge_keeps_old_keys_metadata_and_record_identities():
    fresh = rows_from_item(item())
    previous = [{**fresh[0], 'humble_url': 'https://www.humblebundle.com/downloads?key=fixture',
                 'steam_app_id': 620, 'exclusive_countries': ['CN'], 'disallowed_countries': []},
                {'game_title': 'Not migrated', 'platform': 'steam', 'key': 'DDDDD-EEEEE-FFFFF'}]
    previous[0].pop('modern_entitlement_id')
    merged = merge_rows(previous, fresh)
    assert len(merged) == 2
    assert merged[0]['humble_url'] == previous[0]['humble_url']
    assert merged[0]['steam_app_id'] == 620 and merged[0]['exclusive_countries'] == ['CN']
    assert merged[0]['library_url'].endswith('/content/fixture')
    assert merged[1] == previous[1]


def test_new_stock_record_does_not_guess_platform_or_claim():
    value = item(key='')
    value['entitlements']['keys'][0].update(outOfStock=True, expired=False)
    row = rows_from_item(value)[0]
    assert row['unavailable_status'] == 'key_temporarily_exhausted' and not row['key']
    assert 'revealed_at' not in row and row['steam_app_id'] is None


def test_match_native_pagination_response_with_query_string():
    request = SimpleNamespace(method="POST", post_data='name="_1_page"\r\n\r\n12\r\n')
    response = SimpleNamespace(
        url="https://www.humblebundle.com/home/library?page=12", request=request)
    assert is_library_page_response(response, 12)
    assert not is_library_page_response(response, 1)


@pytest.mark.parametrize('conflict', [False, True])
def test_scanner_reads_all_pages_before_publishing_and_handles_duplicates(conflict):
    published = []
    scanner = NewLibraryScanner(
        None, published.append, lambda _: None, {'rows': [], 'memberships': []},
        hashlib.sha256(b'fixture@example.test').hexdigest())

    async def query(page):
        value = item()
        if conflict and page == 2:
            value['title'] = 'Conflicting metadata'
        return {'results': [value], 'success': True,
                'pagination': {'page': page, 'totalPages': 2, 'totalDocs': 2}}

    async def first():
        return await query(1)

    scanner.open_library, scanner.query = first, query
    if conflict:
        with pytest.raises(ApiUnsupported):
            asyncio.run(scanner.run())
        assert not published
    else:
        asyncio.run(scanner.run())
        assert len(published[0]['rows']) == 1
        assert published[0]['partial'] and any('重复' in x for x in published[0]['warnings'])
