"""Workload: who has how many hours of planned work in each week (or day), against their capacity.

Each open card with an estimate spreads its *remaining* hours (estimate minus time already
logged) evenly over the working days from today (or its start) to its due date, shared
equally between its members. Overdue cards land on today. Cards with no members show
as "Not assigned"; cards with an estimate but no dates are counted under "No dates".
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from . import permissions, timetrack
from .models import Card, TimeEntry, WorkspaceMember
from .schedule import span

User = get_user_model()
WEEKS, DAYS = 8, 10


def periods(by, today):
    monday = today - timedelta(days=today.weekday())
    if by == "day":
        out, d = [], monday
        while len(out) < DAYS:
            if d.weekday() < 5:
                out.append({"start": d, "end": d, "label": d.strftime("%a"), "sub": d.strftime("%-d %b"),
                            "now": d == today, "past": d < today})
            d += timedelta(days=1)
        return out
    return [{"start": monday + timedelta(weeks=i), "end": monday + timedelta(weeks=i, days=6),
             "label": (monday + timedelta(weeks=i)).strftime("%-d %b"), "sub": "this week" if i == 0 else
             ("next week" if i == 1 else ""), "now": i == 0, "past": False} for i in range(WEEKS)]


def _workdays(s, e):
    out, d = [], s
    while d <= e:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out or [e]


def level(pct):
    if pct <= 0:
        return "none"
    if pct < 60:
        return "low"
    if pct <= 90:
        return "ok"
    if pct <= 100:
        return "full"
    return "over"


def build(viewer, *, dept=None, by="week", now=None):
    now = now or timezone.now()
    today = timezone.localdate(now)
    ps = periods(by, today)
    first, last = ps[0]["start"], ps[-1]["end"]

    def idx_of(d):
        for i, p in enumerate(ps):
            if p["start"] <= d <= p["end"]:
                return i
        return None

    boards = permissions.visible_boards(viewer)
    if dept:
        boards = boards.filter(workspace_id=dept)
    cards = list(Card.objects.filter(list__board__in=boards, is_complete=False, is_archived=False, is_template=False,
                                     list__is_archived=False, estimate_hours__gt=0)
                 .select_related("list__board").prefetch_related("members"))
    logged = timetrack.logged_by_card([c.pk for c in cards])
    hours = defaultdict(lambda: defaultdict(float))     # uid (0 = nobody) -> period -> hours
    items = defaultdict(lambda: defaultdict(float))     # (uid, period) -> card -> hours
    undated = defaultdict(float)
    cards_by_id = {c.pk: c for c in cards}
    for c in cards:
        remaining = float(c.estimate_hours) - logged.get(c.pk, 0) / 60
        if remaining <= 0:
            continue
        members = [u.pk for u in c.members.all()] or [0]
        sp = span(c)
        if not sp:
            for m in members:
                undated[m] += remaining / len(members)
            continue
        s, e = max(sp[0], today), max(sp[1], today)
        days = _workdays(s, e)
        each = remaining / len(days) / len(members)
        for d in days:
            i = idx_of(d)
            if i is None:
                continue
            for m in members:
                hours[m][i] += each
                items[(m, i)][c.pk] += each

    if dept:
        people = list(User.objects.filter(workspace_memberships__workspace_id=dept, is_active=True).distinct())
    else:
        ids = {u for u in hours if u} | {u for u in undated if u} | {viewer.pk}
        people = list(User.objects.filter(pk__in=ids, is_active=True))

    week0 = timetrack.week_start(today)
    logged_week = defaultdict(int)
    for uid, mins in (TimeEntry.objects.filter(user__in=people, day__gte=week0, day__lte=week0 + timedelta(days=6))
                      .values_list("user_id", "minutes")):
        logged_week[uid] += mins

    rows = []
    for u in people:
        cap = float(u.capacity_hours or 0) / (5 if by == "day" else 1)
        cells = []
        for i, p in enumerate(ps):
            h = hours[u.pk].get(i, 0.0)
            pct = round(100 * h / cap) if cap else (999 if h else 0)
            cells.append({"i": i, "hours": h, "pct": pct, "level": level(pct), "past": p["past"]})
        rows.append({"user": u, "cap": cap, "weekly": u.capacity_hours, "cells": cells,
                     "peak": max((c["pct"] for c in cells), default=0), "undated": undated.get(u.pk, 0),
                     "logged_week": logged_week.get(u.pk, 0), "total": sum(c["hours"] for c in cells),
                     "can_rate": _can_set(viewer, u)})
        rows[-1]["can_set"] = u.pk == viewer.pk or rows[-1]["can_rate"]
    rows.sort(key=lambda r: (-r["peak"], r["user"].display_name.lower()))
    nobody = None
    if hours.get(0) or undated.get(0):
        nobody = {"cells": [{"i": i, "hours": hours[0].get(i, 0.0), "past": p["past"]} for i, p in enumerate(ps)],
                  "undated": undated.get(0, 0)}

    def detail(uid, i):
        got = items.get((uid, i), {})
        return sorted(((cards_by_id[k], v) for k, v in got.items()), key=lambda kv: -kv[1])

    return {"periods": ps, "rows": rows, "nobody": nobody, "detail": detail, "by": by,
            "first": first, "last": last}


def _can_set(viewer, person):
    if viewer.is_superuser:
        return True
    mine = WorkspaceMember.objects.filter(user=viewer, role="admin").values_list("workspace_id", flat=True)
    return WorkspaceMember.objects.filter(user=person, workspace_id__in=list(mine)).exists()


def money(v):
    return Decimal(v).quantize(Decimal("0.01"))
