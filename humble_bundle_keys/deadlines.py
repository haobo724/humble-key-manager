"""Conservative deadline labels: a missing or unparseable date stays unknown."""
import re
from datetime import datetime, timedelta, timezone

MONTHS = {name.lower(): i for i, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}
MONTHS.update({name[:3]: number for name, number in list(MONTHS.items())})


def deadline_info(text: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    text = text.strip()
    result = {"has_deadline": bool(text), "deadline_state": "unknown", "deadline_at": None}
    if not text:
        return result
    if text.lower() == "expired":
        return {"has_deadline": True, "deadline_state": "expired", "deadline_at": None}
    iso = re.search(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                    r"(?:Z|[+-]\d{2}:?\d{2})?)?", text)
    deadline = None
    try:
        if iso:
            deadline = datetime.fromisoformat(iso[0].replace("Z", "+00:00"))
        else:
            match = re.search(r"([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})",
                              text, re.IGNORECASE)
            if match and match[1].lower() in MONTHS:
                deadline = datetime(int(match[3]), MONTHS[match[1].lower()], int(match[2]))
        if deadline is None:
            return result
        result["deadline_at"] = deadline.replace(tzinfo=timezone.utc).isoformat() \
            if deadline.tzinfo is None else deadline.isoformat()
        if deadline.tzinfo is None:
            # No known timezone: only label dates earlier than today's UTC date as expired.
            cutoff = deadline.replace(hour=0, minute=0, second=0, microsecond=0,
                                      tzinfo=timezone.utc) + timedelta(days=1)
        else:
            cutoff = deadline
        result["deadline_state"] = "expired" if now >= cutoff else "not_expired"
    except ValueError:
        pass
    return result


def annotate_rows(rows: list[dict]) -> list[dict]:
    for row in rows:
        row.update(deadline_info(row.get("redemption_deadline", "")))
    return rows
