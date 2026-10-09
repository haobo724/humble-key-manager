"""API-first claiming for unlimited Choice, with no automatic mutation retries."""
import re
from dataclasses import dataclass

from humble_bundle_keys.api import ORDER_DETAIL_URL, _normalise_platform, normalise_key
from humble_bundle_keys.async_inventory import order_tpks
from humble_bundle_keys.choice import (
    CHOOSECONTENT_URL,
    REDEEMKEY_URL,
    ChoiceClaimer,
    ChoiceOptions,
    build_choosecontent_body,
    build_redeemkey_body,
    extract_revealed_key,
)
from humble_bundle_keys.choice_policy import choice_policy


@dataclass
class ModernResult:
    status: str
    order: dict
    key: str = ""
    revealed: bool = False

    def __post_init__(self):
        self.key = normalise_key(self.key)


def matching_tpk(order, title):
    matches = [t for t in order_tpks(order) if t.get("human_name") == title
               and _normalise_platform(t.get("key_type")) == "steam"]
    return matches[0] if len(matches) == 1 else None


def response_problem(body):
    if not isinstance(body, dict):
        return None
    text = " ".join(str(body.get(k, "")) for k in ("error", "message", "reason", "error_code"))
    text = text.lower()
    if "expired" in text:
        return "expired"
    if "exhausted" in text or "out of stock" in text:
        return "key_temporarily_exhausted"
    return None


def read_card_metadata(card):
    return card.evaluate("""e => {
        const name=e.querySelector('[data-machine-name]')?.dataset.machineName;
        return {identifier:name&&e.querySelector('[id="choice-'+name+'"]')?name:null,
                platform:e.querySelector('.hb-steam')?'steam':null};
    }""")


class ModernChoiceAPI:
    def __init__(self, context, page, read_order):
        self.context = context
        self.read_order = read_order
        self.client = ChoiceClaimer(context, ChoiceOptions())
        # Reuse the already-loaded month page for same-origin fetch requests.
        self.client._anchor_page = page

    def fresh(self, order):
        fresh = self.read_order(self.context, ORDER_DETAIL_URL.format(gamekey=order["gamekey"]))
        if not isinstance(fresh, dict):
            raise ValueError("Invalid refreshed order")
        fresh["gamekey"] = order["gamekey"]
        if ((fresh.get("product") or {}).get("machine_name") !=
                (order.get("product") or {}).get("machine_name")):
            raise ValueError("Refreshed order does not match selected month")
        return fresh

    def claim(self, order, card, url):
        if choice_policy(order.get("product") or {}, url) != "all_games":
            return ModernResult("fallback", order)
        identifier = card.get("identifier", "")
        if (card.get("platform") != "steam" or not isinstance(identifier, str)
                or not re.fullmatch(r"[a-z0-9_]+", identifier)):
            return ModernResult("fallback", order)
        title = card["title"]
        order = self.fresh(order)
        if choice_policy(order.get("product") or {}, url) != "all_games":
            raise ValueError("Month policy changed; rescan before claiming")
        tpk = matching_tpk(order, title)
        if tpk and tpk.get("redeemed_key_val"):
            return ModernResult("success", order, tpk["redeemed_key_val"])
        if tpk and tpk.get("is_expired"):
            return ModernResult("expired", order)
        sent = False
        try:
            # Only explicitly chosen entries can skip the allocation request.
            chosen = (order.get("tpkd_dict") or {}).get("chosen_tpks") or []
            allocated = tpk and any(t.get("machine_name") == tpk.get("machine_name")
                                    for t in chosen if isinstance(t, dict))
            if not allocated:
                sent = True
                body = self.client._post_form(CHOOSECONTENT_URL,
                    build_choosecontent_body(order["gamekey"], [identifier]), referer=url)
                problem = response_problem(body)
                if problem:
                    return ModernResult(problem, self.fresh(order))
                if not isinstance(body, dict) or body.get("success") is not True:
                    raise ValueError("Choice allocation was not confirmed")
                order = self.fresh(order)
                tpk = matching_tpk(order, title)
                if tpk and tpk.get("redeemed_key_val"):
                    return ModernResult("success", order, tpk["redeemed_key_val"], True)
            if tpk is None:
                raise ValueError("Allocated key parameters are unavailable")
            index = tpk.get("keyindex", tpk.get("key_index"))
            if type(index) is not int or index < 0 or not tpk.get("machine_name"):
                raise ValueError("Allocated key parameters are incomplete")
            sent = True
            body = self.client._post_form(REDEEMKEY_URL, build_redeemkey_body(
                order["gamekey"], tpk["machine_name"], index), referer=url)
            key = extract_revealed_key(body)
            if key:
                tpk["redeemed_key_val"] = key
                return ModernResult("success", order, key, True)
            problem = response_problem(body)
            if problem:
                return ModernResult(problem, self.fresh(order))
        except Exception:
            if not sent:
                raise
        # A request may have succeeded despite a timeout. Only read to recover; never retry choose.
        try:
            order = self.fresh(order)
            tpk = matching_tpk(order, title)
            if tpk and tpk.get("redeemed_key_val"):
                return ModernResult("success", order, tpk["redeemed_key_val"], True)
            if tpk and tpk.get("is_expired"):
                return ModernResult("expired", order)
        except Exception:
            pass
        return ModernResult("uncertain", order)
