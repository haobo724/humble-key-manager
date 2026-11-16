"""Explicit selection plans for the redesigned library's game/platform groups."""
import copy
import hashlib
import json
import re
from datetime import datetime, timezone

from humble_bundle_keys.api import ApiUnsupported, normalise_key
from humble_bundle_keys.diagnostics import error_fields
from humble_bundle_keys.new_reveal import REASONS, NewSteamRevealer
from humble_bundle_keys.operation_report import work_id
from humble_bundle_keys.site_session import LIBRARY_URL, flight_records, objects

RAW_KEY_PROVIDERS = {
    'steam', 'gog', 'origin_key', 'generic', 'nintendo', 'playstation', 'bethesda',
    'squareenix', 'rockstar_social', 'arenanet', 'eve_online', 'telltale', 'desura',
    'multiplayer', 'external_key', 'url', 'epay', 'vaultn_steamkey', 'ztorm_steamkey', 'genba_key',
}


def checked_url(url):
    if not isinstance(url, str) or not re.fullmatch(
            re.escape(LIBRARY_URL) + r'/(group|content)/[A-Za-z0-9_-]+', url):
        raise ValueError('新版分组链接无效，请重新扫描。')
    return url


def parse_group(document, url):
    checked_url(url)
    roots = [o['userLibraryItem'] for o in objects(flight_records(document))
             if isinstance(o.get('userLibraryItem'), dict)]
    if not roots or any(root != roots[0] for root in roots):
        raise ApiUnsupported('无法唯一识别官网分组；未执行领取。')
    root = roots[0]
    entitlements = root.get('entitlements')
    if not isinstance(entitlements, dict) or not isinstance(entitlements.get('keys'), list):
        raise ApiUnsupported('官网分组选项格式变化；未执行领取。')
    keys = {}
    for key in entitlements['keys']:
        if not isinstance(key, dict) or not isinstance(key.get('id'), str):
            raise ApiUnsupported('官网权益标识无法识别。')
        if key['id'] in keys:
            raise ApiUnsupported('官网分组有重复权益标识。')
        keys[key['id']] = key
    groups, used, option_states = [], set(), {}
    raw_groups = entitlements.get('redemptionGroups') or []
    if not isinstance(raw_groups, list):
        raise ApiUnsupported('官网额度信息格式变化。')
    for group in raw_groups:
        if (not isinstance(group, dict) or not isinstance(group.get('groupId'), str)
                or type(group.get('maxRedemptions')) is not int or group['maxRedemptions'] < 0
                or not isinstance(group.get('options'), list)):
            raise ApiUnsupported('无法确认官网的领取上限；未执行领取。')
        ids = []
        claimed = set()
        for option in group['options']:
            if (not isinstance(option, dict) or option.get('entitlementId') not in keys
                    or any(type(option.get(name)) is not bool
                           for name in ('eligible', 'redeemed', 'outOfStock'))):
                raise ApiUnsupported('无法核对官网分组选项；未执行领取。')
            identity = option['entitlementId']
            if identity in ids:
                raise ApiUnsupported('官网额度分组内有重复选项。')
            key = keys[identity]
            if option.get('keyIntegrationId') != key.get('keyIntegrationId'):
                raise ApiUnsupported('官网平台标识不一致；未执行领取。')
            ids.append(identity)
            used.add(identity)
            consumed = bool(option['redeemed'] or key.get('redeemedAt')
                            or (key.get('redemptionData') or {}).get('keyFormatted')
                            or key.get('giftStatus') == 'gifted')
            if consumed:
                claimed.add(identity)
            states = option_states.setdefault(identity, [])
            states.append({'eligible': option['eligible'], 'redeemed': consumed,
                           'out_of_stock': option['outOfStock']})
        groups.append({'id': group['groupId'], 'title': group.get('title') or root.get('title', ''),
                       'maximum': group['maxRedemptions'], 'claimed': len(claimed),
                       'remaining': max(0, group['maxRedemptions'] - len(claimed)),
                       'option_ids': ids})
    ungrouped = [identity for identity in keys if identity not in used]
    unknown = set()
    if ungrouped:
        consumed = [identity for identity in ungrouped if keys[identity].get('redeemedAt')
                    or (keys[identity].get('redemptionData') or {}).get('keyFormatted')
                    or keys[identity].get('giftStatus') == 'gifted']
        if raw_groups or '/group/' in url or len(keys) != 1:
            unknown.update(ungrouped)
            groups.append({'id': 'unverified', 'title': '额度待核查', 'maximum': 0,
                           'claimed': len(consumed), 'remaining': 0, 'unknown': True,
                           'option_ids': ungrouped})
        else:
            groups.append({'id': 'independent', 'title': '独立权益', 'maximum': 1,
                           'claimed': len(consumed), 'remaining': 1 - len(consumed),
                           'option_ids': ungrouped})
    if len({group['id'] for group in groups}) != len(groups):
        raise ApiUnsupported('官网额度分组标识重复。')
    options = []
    for identity, key in keys.items():
        provider = key.get('keyIntegrationId')
        states = option_states.get(identity, [])
        redeemed = bool(key.get('redeemedAt')
                        or (key.get('redemptionData') or {}).get('keyFormatted')
                        or key.get('giftStatus') == 'gifted' or any(s['redeemed'] for s in states))
        stock = bool(key.get('outOfStock') or any(s['out_of_stock'] for s in states))
        reason = ('已领取' if redeemed else '已过期' if key.get('expired') else
                  'Key 暂时缺货' if stock else
                  '官网未提供此权益的额度归属，需核查后领取' if identity in unknown else
                  '需在官网关联账号或完成平台兑换' if provider not in RAW_KEY_PROVIDERS else
                  '官网暂不可领取' if not key.get('redeemable')
                  or any(not s['eligible'] for s in states) else '')
        if not reason and any(group['remaining'] == 0 for group in groups
                              if identity in group['option_ids']):
            reason = '剩余额度为 0'
        options.append({'id': identity,
                        'game': key.get('title') or root.get('title') or '未命名游戏',
                        'platform': key.get('type') or provider or 'unknown',
                        'provider': provider, 'redeemed': redeemed, 'eligible': not bool(reason),
                        'reason': reason, 'out_of_stock': stock})
    model = {'url': url, 'title': root.get('title') or '官网分组',
             'groups': groups, 'options': options}
    model['fingerprint'] = hashlib.sha256(json.dumps(model, sort_keys=True).encode()).hexdigest()
    return model, keys


def validate_selection(model, selected):
    if (not isinstance(selected, list) or not 0 < len(selected) <= 500
            or any(not isinstance(identity, str) for identity in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('请选择游戏和平台，不要重复选择。')
    options = {option['id']: option for option in model['options']}
    if any(identity not in options or not options[identity]['eligible'] for identity in selected):
        raise ValueError('所选项目已领取、缺货或暂不可领取，请刷新分组后重选。')
    chosen = set(selected)
    quotas = []
    for group in model['groups']:
        count = len(chosen.intersection(group['option_ids']))
        if count > group['remaining']:
            raise ValueError(f"「{group['title']}」超出剩余额度，请减少选择。")
        if count:
            quotas.append({'title': group['title'], 'remaining': group['remaining'],
                           'selected': count, 'after': group['remaining'] - count})
    return [options[identity] for identity in selected], quotas


def membership(model):
    remaining = sum(group['remaining'] for group in model['groups'])
    state = ('unknown' if any(group.get('unknown') for group in model['groups']) else
             'pending' if remaining else 'exhausted')
    return {'url': model['url'], 'bundle_name': model['title'], 'source': 'humble_next',
            'state': state, 'choice_policy': 'native',
            'unclaimed_titles': [o['game'] for o in model['options'] if not o['redeemed']],
            'claimed_games': sum(o['redeemed'] for o in model['options']),
            'total_games': len(model['options']), 'redemption_groups': model['groups']}


class NewGroupClient(NewSteamRevealer):
    def load(self, url):
        document = self.text(checked_url(url))
        model, keys = parse_group(document, url)
        return model, keys, document


def save_group_snapshot(inventory, model, keys):
    from humble_bundle_keys.new_library import merge_rows, rows_from_item

    kind, identity = model['url'].rsplit('/', 2)[-2:]
    fresh = []
    for key in keys.values():
        item = {'id': identity, 'title': key.get('title') or model['title'],
                'subTitle': key.get('subTitle') or model['title'],
                'entitlements': {'keys': [key]}}
        for row in rows_from_item(item):
            row['library_url'] = model['url']
            needs_selection = (kind == 'group' or len(keys) != 1
                               or any(group['id'] != 'independent' for group in model['groups']))
            row['modern_group_url'] = model['url'] if needs_selection else None
            row['modern_item_kind'] = kind
            fresh.append(row)
    with inventory.lock:
        snapshot = {**inventory.snapshot,
                    'rows': merge_rows(inventory.snapshot['rows'], fresh)}
        months = [m for m in snapshot['memberships'] if m['url'] != model['url']]
        if kind == 'group':
            months.append(membership(model))
        snapshot['memberships'] = months
        inventory.publish(snapshot)


def _structure(model):
    return (sorted((g['id'], g['maximum'], tuple(sorted(g['option_ids'])))
                   for g in model['groups']),
            sorted((o['id'], o['provider'], o['platform']) for o in model['options']))


def with_consumed(model, identity):
    updated = copy.deepcopy(model)
    for option in updated['options']:
        if option['id'] == identity:
            option.update(redeemed=True, eligible=False, reason='已领取')
    for group in updated['groups']:
        if identity in group['option_ids']:
            group['claimed'] += 1
            group['remaining'] = max(0, group['remaining'] - 1)
        if not group['remaining']:
            for option in updated['options']:
                if option['id'] in group['option_ids'] and not option['redeemed']:
                    option.update(eligible=False, reason='剩余额度为 0')
    return updated


def claim_group(context, inventory, plan):
    if plan['account'] != inventory.hb_activation.data['current_account']:
        raise ValueError('HB 账号与预览不同，请重新预览。')
    inventory.begin_report('group-claim')
    for option in plan['options']:
        inventory.report_item(work_id(plan['url'], option['id']), option['game'], plan['title'])
    client = NewGroupClient(context)
    model, keys, document = client.load(plan['url'])
    if model['fingerprint'] != plan['fingerprint']:
        save_group_snapshot(inventory, model, keys)
        raise ValueError('官网额度或选择状态已变化，未领取；已更新分组，请重新预览。')
    selected, _ = validate_selection(model, plan['selected_ids'])
    for option in selected:
        if inventory.stop_event.is_set():
            break
        identity = work_id(plan['url'], option['id'])
        inventory.report_phase('核对分组额度与所选平台', option['game'], model['title'])
        fresh, keys, document = client.load(plan['url'])
        if fresh['fingerprint'] != model['fingerprint']:
            save_group_snapshot(inventory, fresh, keys)
            inventory.report_issue('官网额度或选择状态已变化，已停止；请重新预览。')
            inventory.update('官网额度或选择状态已变化，已停止；请重新预览。')
            return
        validate_selection(fresh, [option['id']])
        client.resolve_action(document)
        inventory.report_phase('领取所选游戏与平台', option['game'], model['title'])
        inventory.update(f"正在领取：{option['game']} · {option['platform']}")
        result = client.submit(option['id'], option['provider'], plan['url'])
        if result['status'] == 'success':
            # Persist a returned key before any further network request.
            key = dict(keys[option['id']])
            key['redemptionData'] = {**(key.get('redemptionData') or {}),
                                     'keyFormatted': result['key']}
            key['redeemedAt'] = result.get('time') or datetime.now(timezone.utc).isoformat()
            key['outOfStock'] = False
            keys[option['id']] = key
            save_group_snapshot(inventory, with_consumed(fresh, option['id']), keys)
            inventory.report_item(identity, option['game'], model['title'], 'success')
            inventory.log('new_group_key_revealed', game=option['game'], bundle=model['title'],
                          source=option['provider'])
        else:
            reason = REASONS.get(result['status'], '领取结果不确定；已停止，未重复提交')
            inventory.report_item(identity, option['game'], model['title'], 'failed', reason)
            try:
                readback, read_keys, _ = client.load(plan['url'])
                save_group_snapshot(inventory, readback, read_keys)
                recovered = normalise_key((read_keys[option['id']].get('redemptionData') or {}).get(
                    'keyFormatted'))
                if recovered:
                    inventory.report_item(identity, option['game'], model['title'], 'success',
                                          '通过只读核查取回 Key；其余项目未处理')
            except Exception as exc:
                inventory.log('new_group_readback_failed', **error_fields(exc))
            inventory.report_issue(reason + '；其余项目未处理，请核查官网。')
            inventory.update(reason + '；已停止，其余项目未处理。')
            return
        try:
            after, after_keys, _ = client.load(plan['url'])
            save_group_snapshot(inventory, after, after_keys)
        except Exception as exc:
            inventory.log('new_group_recheck_failed', **error_fields(exc))
            inventory.report_issue('Key 已保存，但额度重新核对失败；其余项目未处理。')
            inventory.update('Key 已保存，但额度重新核对失败，已停止。')
            return
        consumed_before = {o['id'] for o in fresh['options'] if o['redeemed']}
        consumed_after = {o['id'] for o in after['options'] if o['redeemed']}
        if (_structure(fresh) != _structure(after)
                or consumed_after != consumed_before | {option['id']}):
            inventory.report_issue('领取后官网额度或选择记录与预期不一致，已停止；Key 已保存。')
            inventory.update('领取后官网额度或选择记录与预期不一致，已停止；Key 已保存。')
            return
        model = after
        if inventory.stop_event.wait(3):
            break
    inventory.update('分组领取已停止，Key 与额度记录已保存。' if inventory.stop_event.is_set()
                     else '分组领取已结束，Key 与额度记录已保存。')
