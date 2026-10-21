"""Spot a due date in a card title as you type, Trello-style:
"Order flour tomorrow", "Gemma starts next Monday", "Stocktake 12 June 3pm",
"Call Kingsmill in 3 days", "Deep clean Friday at 14:00", "Invoice 25/11".

Returns (due datetime in local time, the matched words) or (None, "").
Default time 17:00, like the calendar. Days of the week mean the next one
after today; "next Friday" means the Friday of next week if this week's
hasn't come yet... unless today is earlier in the week, where UK usage is
ambiguous — so we show the date we picked before the card is created.
"""
import re
from datetime import date, datetime, time, timedelta

from django.utils import timezone

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_ABBR = {d[:3]: i for i, d in enumerate(DAYS)} | {"tues": 1, "weds": 2, "thur": 3, "thurs": 3}
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december"]
MON_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DAY_RE = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday|s)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
TIME_RE = r"(?:\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?"


def _day_index(word):
    w = word.lower()
    for i, d in enumerate(DAYS):
        if w == d or w == d[:3]:
            return i
    return DAY_ABBR.get(w[:4], DAY_ABBR.get(w[:3]))


def _month_index(word):
    return next(i for i, m in enumerate(MONTHS) if m.startswith(word.lower()[:3])) + 1


def _time(h, m, ampm):
    if h is None:
        return time(17, 0)
    h, m = int(h), int(m or 0)
    if ampm:
        if ampm.lower() == "pm" and h < 12:
            h += 12
        if ampm.lower() == "am" and h == 12:
            h = 0
    elif m == 0 and h <= 7:          # "Friday 3" → 3pm, not 3am
        h += 12
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


def _upcoming(today, weekday, next_week=False):
    ahead = (weekday - today.weekday()) % 7 or 7
    if next_week and ahead < 7 - today.weekday():   # still this week → jump to next week's
        ahead += 7
    return today + timedelta(days=ahead)


PATTERNS = [
    # today / tonight / tomorrow
    (re.compile(r"\b(today|tonight|tomorrow|tmrw|tmw)" + TIME_RE + r"\b", re.I),
     lambda m, t: (t + timedelta(days=0 if m.group(1).lower() in ("today", "tonight") else 1), m.group(2, 3, 4))),
    # next week
    (re.compile(r"\bnext\s+week\b", re.I), lambda m, t: (_upcoming(t, 0), (None, None, None))),
    # in 3 days / in 2 weeks
    (re.compile(r"\bin\s+(\d{1,3})\s+(day|days|week|weeks)\b" + TIME_RE, re.I),
     lambda m, t: (t + timedelta(days=int(m.group(1)) * (7 if m.group(2).lower().startswith("week") else 1)),
                   m.group(3, 4, 5))),
    # (on|this|next) friday (at 3pm). Short forms ("sun", "sat") only count with on/this/next or a time,
    # so "sun-dried tomatoes" isn't Sunday.
    (re.compile(r"\b(?:(next|this|on)\s+)?" + DAY_RE + r"\b(?!-)" + TIME_RE, re.I),
     lambda m, t: ((_upcoming(t, _day_index(m.group(2)), (m.group(1) or "").lower() == "next")
                    if (m.group(1) or m.group(3) or m.group(2).lower() in DAYS) else None), m.group(3, 4, 5))),
    # 12 June / 12th June 2027
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + MON_RE + r"\b(?:\s+(\d{4}))?" + TIME_RE, re.I),
     lambda m, t: (_dated(t, int(m.group(1)), _month_index(m.group(2)), m.group(3)), m.group(4, 5, 6))),
    # June 12 / June 12th
    (re.compile(r"\b" + MON_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?" + TIME_RE, re.I),
     lambda m, t: (_dated(t, int(m.group(2)), _month_index(m.group(1)), m.group(3)), m.group(4, 5, 6))),
    # 25/11 or 25/11/2026 (UK day/month)
    (re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b(?!\s+of\b)" + TIME_RE),
     lambda m, t: (_dated(t, int(m.group(1)), int(m.group(2)), m.group(3)), m.group(4, 5, 6))),
]


def _dated(today, day, month, year):
    if year:
        y = int(year)
        y = y + 2000 if y < 100 else y
        try:
            return date(y, month, day)
        except ValueError:
            return None
    for y in (today.year, today.year + 1):        # no year: the next time that date comes round
        try:
            d = date(y, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def parse(text, now=None):
    now = timezone.localtime(now) if now else timezone.localtime()
    today = now.date()
    best = None
    for rx, fn in PATTERNS:
        for m in rx.finditer(text or ""):
            d, (h, mi, ap) = fn(m, today)
            if not d:
                continue
            t = _time(h, mi, ap)
            if t is None:
                continue
            if best is None or m.start() < best[2]:
                best = (d, t, m.start(), m.group(0).strip())
            break
    if not best:
        return None, ""
    d, t, _, words = best
    due = timezone.make_aware(datetime.combine(d, t), timezone.get_current_timezone())
    return due, words
