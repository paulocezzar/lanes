"""Time spent on cards: a start/stop timer (one running per person) and manual entries."""
import re
from datetime import timedelta

from django.db.models import Sum
from django.utils import timezone

from .models import TimeEntry

MAX_TIMER_MINUTES = 12 * 60     # a timer left running overnight counts as 12 hours at most


def running(user):
    if not user.is_authenticated:
        return None
    return (TimeEntry.objects.filter(user=user, started_at__isnull=False)
            .select_related("card", "card__list").first())


def stop(entry, now=None):
    now = now or timezone.now()
    mins = round((now - entry.started_at).total_seconds() / 60)
    entry.minutes += max(1, min(mins, MAX_TIMER_MINUTES))
    entry.started_at = None
    entry.save(update_fields=["minutes", "started_at"])
    return entry


def start(card, user, now=None):
    """Start a timer on card; any other running timer of this person stops first."""
    now = now or timezone.now()
    stopped = None
    for e in TimeEntry.objects.filter(user=user, started_at__isnull=False):
        if e.card_id == card.pk:
            return e, None
        stopped = stop(e, now)
    e = TimeEntry.objects.create(card=card, user=user, day=timezone.localdate(now), started_at=now)
    return e, stopped


def parse_minutes(text):
    """'1h 30m', '1:30', '1.5' (hours), '90m', '2h' → minutes. None if it can't be read."""
    s = (str(text or "")).strip().lower().replace(",", ".")
    if not s:
        return None
    m = re.fullmatch(r"(\d+):([0-5]?\d)", s)
    if m:
        return int(m[1]) * 60 + int(m[2])
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?", s)
    if m and (m[1] or m[2]):
        return round(float(m[1] or 0) * 60) + int(m[2] or 0)
    try:
        return round(float(s) * 60)
    except ValueError:
        return None


def fmt(minutes):
    minutes = int(minutes or 0)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def card_minutes(card):
    return TimeEntry.objects.filter(card=card).aggregate(s=Sum("minutes"))["s"] or 0


def logged_by_card(card_ids):
    rows = (TimeEntry.objects.filter(card_id__in=card_ids).values("card_id").annotate(s=Sum("minutes")))
    return {r["card_id"]: r["s"] or 0 for r in rows}


def week_start(d):
    return d - timedelta(days=d.weekday())
