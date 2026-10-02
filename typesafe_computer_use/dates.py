"""Deterministic date handling.

The classifier does no calendar math, so dates found in screen text are parsed
here and handed over as offsets from today.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .models import Item, Screen

MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH = r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DASHES = "[-\u2013\u2014]"  # hyphen, en dash, em dash between the days of a range
DATE_RE = re.compile(
    rf"\b(?:(?P<mon>{_MONTH})[a-z]*\.?\s+(?P<day>\d{{1,2}})(?:\s*{_DASHES}\s*\d{{1,2}})?(?:(?:,\s*|\s+)(?P<year>\d{{4}}))?"
    rf"|(?P<day2>\d{{1,2}})\s+(?P<mon2>{_MONTH})[a-z]*\.?(?:(?:,\s*|\s+)(?P<year2>\d{{4}}))?"
    r"|(?P<iso>\d{4}-\d{2}-\d{2})"
    r"|(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}))\b",
    re.IGNORECASE,
)
NEAR_ROWS_PT = 60


def now_context() -> dict:
    now = datetime.now().astimezone()
    return {
        "local_time": now.strftime("%Y-%m-%d %H:%M %A"),
        "timezone": now.strftime("%Z"),
        "today": now.date().isoformat(),
    }


def first_date(text: str, today: date | None = None) -> date | None:
    """The first date mentioned in the text, or None. A missing year is assumed current or next."""
    today = today or date.today()
    m = DATE_RE.search(text)
    if not m:
        return None
    if m.group("mon2") and not m.group("year2"):
        # OCR can put a stray digit before a complete month-first date. The
        # second match starts inside "1 Apr" and extends to "Apr 30, 2026".
        month_first = DATE_RE.search(text, m.start("mon2"))
        if month_first and month_first.group("mon") and month_first.start() < m.end():
            m = month_first
    try:
        if m.group("iso"):
            return date.fromisoformat(m.group("iso"))
        if m.group("m"):
            return date(int(m.group("y")), int(m.group("m")), int(m.group("d")))
        mon = (m.group("mon") or m.group("mon2"))[:3].lower()
        day = int(m.group("day") or m.group("day2"))
        year = m.group("year") or m.group("year2")
        found = date(int(year) if year else today.year, MONTHS[mon], day)
        if not year and (today - found).days > 60:
            found = found.replace(year=today.year + 1)
        return found
    except ValueError:
        return None


def describe_offset(d: date, today: date | None = None) -> str:
    delta = (d - (today or date.today())).days
    if delta == 0:
        return f"{d.isoformat()} (today)"
    if delta > 0:
        return f"{d.isoformat()} (in {delta} days)"
    return f"{d.isoformat()} ({-delta} days ago)"


def date_hints(items: list[Item], screen: Screen, today: date | None = None) -> dict[int, str]:
    """Item index -> 'dated ...' for items containing a date, or 'near a line dated ...' for close neighbours.

    The menu bar strip takes no part. Its clock is a date, and it would date every tab and button
    under it as near a line dated today: noise, and in OSWorld's Chrome tasks 7% of every request.
    """
    items = [it for it in items if not screen.in_menu_bar(it)]
    dated = {it.index: d for it in items if (d := first_date(it.text, today)) is not None}
    hints = {i: f"dated {describe_offset(d, today)}" for i, d in dated.items()}
    if not dated:
        return hints
    by_index = {it.index: it for it in items}
    for it in items:
        if it.index in hints:
            continue
        cy = it.center[1]
        nearest = min(dated, key=lambda i: abs(by_index[i].center[1] - cy))
        if abs(by_index[nearest].center[1] - cy) < NEAR_ROWS_PT * screen.scale:
            hints[it.index] = f"near a line dated {describe_offset(dated[nearest], today)}"
    return hints
