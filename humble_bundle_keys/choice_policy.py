"""Choice changed to all-games delivery from February 2022."""
import re

MONTHS = {name: i for i, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), 1)}


def choice_policy(product: dict, url: str = "") -> str:
    machine = product.get("machine_name", "").lower()
    name = product.get("human_name", "").lower()
    if machine.endswith("_monthly"):
        return "unknown"
    if "choice" not in machine and "humble choice" not in name:
        return "unknown"
    match = re.search(r"([a-z]+)[_-](\d{4})", machine or url.rsplit("/", 1)[-1])
    if not match or match[1] not in MONTHS:
        return "unknown"
    return "all_games" if (int(match[2]), MONTHS[match[1]]) >= (2022, 2) else "limited"


def upgrade_membership(month: dict) -> dict:
    policy = choice_policy({"human_name": month.get("bundle_name", "")}, month.get("url", ""))
    month["choice_policy"] = policy
    if policy == "all_games":
        month["choices_remaining"] = None
        if month.get("state") == "exhausted":
            month["state"] = "pending" if month.get("unclaimed_titles") else "complete"
    return month
