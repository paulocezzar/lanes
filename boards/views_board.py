"""The premium views: Calendar, Table, Timeline and Dashboard.

Each works on one board (/b/<id>/calendar/) or on every board you can see
(/all/calendar/). Clicking a card anywhere opens the same card dialog.
"""
import calendar as pycal
from collections import Counter, OrderedDict
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from . import permissions
from .board_context import all_context, board_context, open_cards
from .services import cards_qs


def _scope(request, board_id, view):
    """(board or None, ctx, cards queryset)."""
    if board_id is None:
        ctx = all_context(request, view)
        boards = ctx["scope_boards"]
        cards = cards_qs().filter(list__board__in=boards, list__is_archived=False, is_archived=False,
                                  is_template=False)
        return None, ctx, cards
    board, ctx = board_context(request, board_id, view)
    ctx["base_template"] = "boards/board_base.html"
    return board, ctx, open_cards(board)


def _local_date(dt):
    return timezone.localtime(dt).date() if dt else None


def _parse_month(value, today):
    try:
        y, m = (int(x) for x in value.split("-"))
        return date(y, m, 1)
    except (AttributeError, ValueError):
        return today.replace(day=1)


def _shift_month(d, months):
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


@login_required
def board_calendar(request, board_id=None):
    board, ctx, qs = _scope(request, board_id, "calendar")
    today = timezone.localdate()
    month = _parse_month(request.GET.get("m"), today)
    mode = "week" if request.GET.get("v") == "week" else "month"
    if mode == "week":
        try:
            anchor = date.fromisoformat(request.GET.get("d", ""))
        except ValueError:
            anchor = today if request.GET.get("m") in (None, "") else month
        monday = anchor - timedelta(days=anchor.weekday())
        weeks = [[monday + timedelta(days=i) for i in range(7)]]
        month = monday.replace(day=1) if monday.month == (monday + timedelta(days=6)).month else (monday + timedelta(days=6)).replace(day=1)
    else:
        weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)
    first, last = weeks[0][0], weeks[-1][-1]

    by_day, unscheduled = {}, []
    for c in qs:
        d = _local_date(c.due_at)
        if d is None:
            unscheduled.append(c)
        elif first <= d <= last:
            by_day.setdefault(d, []).append(c)
    for day_cards in by_day.values():
        day_cards.sort(key=lambda c: (c.is_complete, c.due_at))

    ctx.update({
        "month": month,
        "prev_month": _shift_month(month, -1).strftime("%Y-%m"),
        "next_month": _shift_month(month, 1).strftime("%Y-%m"),
        "this_month": today.strftime("%Y-%m"),
        "today": today,
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "weeks": [[{"date": d, "cards": by_day.get(d, []), "in_month": mode == "week" or d.month == month.month}
                   for d in w] for w in weeks],
        "mode": mode,
        "week_start": weeks[0][0] if mode == "week" else None,
        "prev_week": (weeks[0][0] - timedelta(days=7)).isoformat() if mode == "week" else "",
        "next_week": (weeks[0][0] + timedelta(days=7)).isoformat() if mode == "week" else "",
        "month_choices": [(_shift_month(today.replace(day=1), i).strftime("%Y-%m"), _shift_month(today.replace(day=1), i))
                          for i in range(-12, 13)],
        "add_lists": board.lists.filter(is_archived=False) if board else [],
        "ical_url": _ical_url(request, board),
        "unscheduled": unscheduled[:200],
        "first_list": board.lists.filter(is_archived=False).first() if board else None,
    })
    return render(request, "boards/view_calendar.html", ctx)


@login_required
def board_table(request, board_id=None):
    board, ctx, qs = _scope(request, board_id, "table")
    cards = list(qs.order_by("list__board__title", "list__position", "position")[:2000])
    fields = list(board.custom_fields.all()) if board else []
    rows = []
    for c in cards:
        done, total = c.checklist_counts()
        values = {v.field_id: v for v in c.field_values.all()}
        rows.append({"card": c, "done": done, "total": total,
                     "pct": round(100 * done / total) if total else None,
                     "fields": [values.get(f.pk) for f in fields]})
    ctx.update({
        "rows": rows,
        "lists": board.lists.filter(is_archived=False) if board else [],
        "filter_boards": sorted({c.list.board for c in cards}, key=lambda b: b.title) if not board else [],
        "fields": fields,
    })
    return render(request, "boards/view_table.html", ctx)


@login_required
def board_timeline(request, board_id=None):
    board, ctx, qs = _scope(request, board_id, "timeline")
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        start = today - timedelta(days=7)
    start -= timedelta(days=start.weekday())          # always begin on a Monday
    days = 42                                         # six weeks
    end = start + timedelta(days=days - 1)

    # One board: group by its lists (+ a group for cards mirrored in). All boards: group by board.
    groups = OrderedDict()
    if board:
        for lst in board.lists.filter(is_archived=False):
            groups[("l", lst.pk)] = {"title": lst.title, "rows": []}
    undated = 0
    for c in qs.order_by("list__board__title", "list__position", "due_at"):
        due = _local_date(c.due_at)
        if not due:
            undated += 1
            continue
        begin = min(c.start_date or due, due)
        if due < start or begin > end:
            continue
        if board is None:
            key, title = ("b", c.list.board_id), c.list.board.title
        elif c.list.board_id == board.pk:
            key, title = ("l", c.list_id), c.list.title
        else:
            key, title = ("m", 0), "🔗 Mirrored from other boards"
        col_start = max((begin - start).days, 0)
        col_end = min((due - start).days, days - 1)
        groups.setdefault(key, {"title": title, "rows": []})["rows"].append({
            "card": c, "col": col_start + 1, "span": col_end - col_start + 1,
            "clipped_left": begin < start, "clipped_right": due > end,
        })

    ctx.update({
        "start": start,
        "days": [start + timedelta(days=i) for i in range(days)],
        "day_count": days,
        "today": today,
        "today_col": (today - start).days + 1 if start <= today <= end else None,
        "groups": [g for g in groups.values() if g["rows"] or board],
        "undated": undated,
        "prev": (start - timedelta(days=28)).isoformat(),
        "next": (start + timedelta(days=28)).isoformat(),
    })
    return render(request, "boards/view_timeline.html", ctx)


def _ical_url(request, board):
    """Private calendar-feed link for this person (made on first use)."""
    import secrets

    from django.conf import settings
    u = request.user
    if not u.ical_token:
        u.ical_token = secrets.token_urlsafe(24)
        u.save(update_fields=["ical_token"])
    path = f"/ical/{u.ical_token}/" + (f"b{board.pk}.ics" if board else "all.ics")
    return settings.SITE_URL + path


def ical_feed(request, token, scope):
    """An .ics feed Outlook / Google / Apple Calendar can subscribe to. Read-only;
    anyone holding the link sees the same cards its owner can see, so it's private."""
    import re
    from datetime import datetime
    from datetime import timezone as dt_tz

    from django.contrib.auth import get_user_model
    from django.http import Http404, HttpResponse
    from django.conf import settings
    u = get_user_model().objects.filter(ical_token=token, is_active=True).first() if token else None
    if not u or not (scope == "all" or re.fullmatch(r"b\d+", scope)):
        raise Http404
    boards = permissions.visible_boards(u).filter(is_template=False)
    if scope != "all":
        boards = boards.filter(pk=int(scope[1:]))
        if not boards.exists():
            raise Http404
    cards = (cards_qs().filter(list__board__in=boards, list__is_archived=False, is_archived=False, is_template=False,
                               due_at__isnull=False, due_at__gte=timezone.now() - timedelta(days=90))
             .select_related("list__board").order_by("due_at")[:2000])

    def esc(s):
        return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def fold(line):
        out = []
        while len(line.encode()) > 74:
            cut = 74
            while len(line[:cut].encode()) > 74:
                cut -= 1
            out.append(line[:cut]); line = " " + line[cut:]
        out.append(line)
        return "\r\n".join(out)

    fmt = lambda dt: dt.astimezone(dt_tz.utc).strftime("%Y%m%dT%H%M%SZ")
    now = fmt(datetime.now(dt_tz.utc))
    name = "Lanes" if scope == "all" else f"Lanes — {boards.first().title}"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Lanes//Calendar//EN", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{esc(name)}", "X-PUBLISHED-TTL:PT1H", "REFRESH-INTERVAL;VALUE=DURATION:PT1H"]
    for c in cards:
        url = f"{settings.SITE_URL}/b/{c.list.board_id}/?card={c.pk}"
        lines += ["BEGIN:VEVENT", f"UID:lanes-card-{c.pk}@lanes", f"DTSTAMP:{now}",
                  f"DTSTART:{fmt(c.due_at - timedelta(minutes=30))}", f"DTEND:{fmt(c.due_at)}",
                  f"SUMMARY:{esc(('✓ ' if c.is_complete else '') + c.title)}",
                  f"DESCRIPTION:{esc(f'{c.list.board.title} › {c.list.title}' + chr(10) + url)}", f"URL:{url}",
                  "END:VEVENT"]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(fold(l) for l in lines) + "\r\n"
    resp = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    resp["Content-Disposition"] = 'inline; filename="lanes.ics"'
    return resp


def _bars(counter):
    """[{label, count, pct_of_max}] biggest first, for CSS bar charts."""
    items = counter.most_common()
    top = max((n for _, n in items), default=0) or 1
    return [{"label": k, "count": n, "pct": round(100 * n / top)} for k, n in items]


@login_required
def board_dashboard(request, board_id=None):
    board, ctx, qs = _scope(request, board_id, "dashboard")
    cards = list(qs)
    today = timezone.localdate()
    week_end = today + timedelta(days=7)

    per_group = (OrderedDict((lst.title, 0) for lst in board.lists.filter(is_archived=False)) if board
                 else OrderedDict())
    per_member, per_label, status = Counter(), Counter(), Counter()
    items_done = items_total = 0
    for c in cards:
        if board is None:
            key = c.list.board.title
        elif c.list.board_id == board.pk:
            key = c.list.title
        else:
            key = "🔗 Mirrored in"
        per_group[key] = per_group.get(key, 0) + 1
        members = list(c.members.all())
        for u in members:
            per_member[u.display_name] += 1
        if not members:
            per_member["Unassigned"] += 1
        for lab in c.labels.all():
            per_label[(str(lab), lab.colour)] += 1
        due = _local_date(c.due_at)
        if c.is_complete:
            status["Complete"] += 1
        elif due is None:
            status["No due date"] += 1
        elif due < today:
            status["Overdue"] += 1
        elif due <= week_end:
            status["Due this week"] += 1
        else:
            status["Later"] += 1
        d, t = c.checklist_counts()
        items_done += d
        items_total += t

    if board is None:
        per_group = OrderedDict(sorted(per_group.items(), key=lambda kv: -kv[1]))
    top_group = max(per_group.values(), default=0) or 1
    status_order = ["Overdue", "Due this week", "Later", "No due date", "Complete"]
    top_status = max(status.values(), default=0) or 1
    top_label = max(per_label.values(), default=0) or 1
    ctx.update({
        "total": len(cards),
        "complete": status["Complete"],
        "overdue": status["Overdue"],
        "due_week": status["Due this week"],
        "items_pct": round(100 * items_done / items_total) if items_total else None,
        "items_done": items_done, "items_total": items_total,
        "group_title": "Cards per list" if board else "Cards per board",
        "by_list": [{"label": k, "count": n, "pct": round(100 * n / top_group)} for k, n in per_group.items()],
        "by_member": _bars(per_member)[:15],
        "by_label": [{"label": k[0], "colour": k[1], "count": n, "pct": round(100 * n / top_label)}
                     for k, n in per_label.most_common(15)],
        "by_status": [{"label": k, "count": status[k], "pct": round(100 * status[k] / top_status),
                       "key": k.lower().replace(" ", "-")} for k in status_order],
    })
    return render(request, "boards/view_dashboard.html", ctx)


def visible_scope_boards(user):
    return permissions.visible_boards(user).filter(is_template=False)


@login_required
def board_map(request, board_id=None):
    """Cards with a location field, as pins on a map."""
    import time

    from . import geo
    from .models import CardFieldValue, CustomField
    board, ctx, qs = _scope(request, board_id, "map")
    values = (CardFieldValue.objects.filter(card__in=qs, field__kind=CustomField.LOCATION)
              .select_related("field", "card__list__board").order_by("card__list__board__title", "card__title"))
    budget = time.monotonic() + 6          # look up a few new addresses per visit, never hang the page
    pins, unplaced = [], []
    for v in values[:1000]:
        geo.place(v, budget_until=budget)
        c = v.card
        row = {"card": c, "field": v.field.name, "where": v.value}
        if v.lat is not None:
            pins.append({**row, "lat": v.lat, "lng": v.lng})
        else:
            unplaced.append(row)
    ctx.update({
        "pins": pins,
        "pins_json": [{"id": p["card"].pk, "title": p["card"].title, "where": str(p["where"]), "field": p["field"],
                       "list": p["card"].list.title, "board": p["card"].list.board.title,
                       "done": p["card"].is_complete, "lat": p["lat"], "lng": p["lng"]} for p in pins],
        "unplaced": unplaced,
        "has_location_field": (board.custom_fields.filter(kind=CustomField.LOCATION).exists() if board else True),
        "location_field": board.custom_fields.filter(kind=CustomField.LOCATION).first() if board else None,
    })
    return render(request, "boards/view_map.html", ctx)
