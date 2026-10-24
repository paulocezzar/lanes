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
    """Trello-style timeline: zoom (day/week/month/quarter), grouped by list, member or nothing,
    with each group's undated cards one click away."""
    board, ctx, qs = _scope(request, board_id, "timeline")
    today = timezone.localdate()
    zooms = {"day": (14, 120), "week": (28, 64), "month": (56, 30), "quarter": (91, 16)}
    zoom = request.GET.get("z") if request.GET.get("z") in zooms else "week"
    span, col_w = zooms[zoom]
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        start = today - timedelta(days=3 if zoom == "day" else 7)
    if zoom != "day":
        start -= timedelta(days=start.weekday())          # begin on a Monday
    end = start + timedelta(days=span - 1)
    group_by = request.GET.get("g") if request.GET.get("g") in ("list", "member", "none") else ("list" if board else "board")

    groups = OrderedDict()
    if board and group_by == "list":
        for lst in board.lists.filter(is_archived=False):
            groups[("l", lst.pk)] = {"title": lst.title, "list_id": lst.pk, "rows": [], "undated": []}
    elif group_by == "member":
        for u in ctx.get("people", []):
            groups[("u", u.pk)] = {"title": u.display_name, "list_id": None, "rows": [], "undated": []}
        groups[("u", 0)] = {"title": "Unassigned", "list_id": None, "rows": [], "undated": []}
    elif group_by == "none":
        groups[("n", 0)] = {"title": "All cards", "list_id": None, "rows": [], "undated": []}

    def keys_for(c):
        if group_by == "list" and board:
            if c.list.board_id == board.pk:
                return [(("l", c.list_id), c.list.title)]
            return [(("m", 0), "🔗 Mirrored from other boards")]
        if group_by == "member":
            ms = list(c.members.all())
            return [(("u", u.pk), u.display_name) for u in ms] or [(("u", 0), "Unassigned")]
        if group_by == "none":
            return [(("n", 0), "All cards")]
        return [(("b", c.list.board_id), c.list.board.title)]

    for c in qs.order_by("list__board__title", "list__position", "due_at", "position"):
        due = _local_date(c.due_at)
        for key, title in keys_for(c):
            g = groups.setdefault(key, {"title": title, "list_id": None, "rows": [], "undated": []})
            if not due:
                g["undated"].append(c)
                continue
            begin = min(c.start_date or due, due)
            if due < start or begin > end:
                continue
            col_start = max((begin - start).days, 0)
            col_end = min((due - start).days, span - 1)
            g["rows"].append({"card": c, "col": col_start + 1, "span": col_end - col_start + 1,
                              "clipped_left": begin < start, "clipped_right": due > end})
    step = {"day": 7, "week": 14, "month": 28, "quarter": 63}[zoom]
    days = [start + timedelta(days=i) for i in range(span)]
    ctx.update({
        "start": start, "end": end,
        "days": days,
        "day_count": span,
        "col_w": col_w,
        "zoom": zoom, "zooms": [("day", "Day"), ("week", "Week"), ("month", "Month"), ("quarter", "Quarter")],
        "group_by": group_by,
        "today": today,
        "today_col": (today - start).days + 1 if start <= today <= end else None,
        "groups": [g for g in groups.values() if g["rows"] or g["undated"] or g.get("list_id")],
        "prev": (start - timedelta(days=step)).isoformat(),
        "next": (start + timedelta(days=step)).isoformat(),
        "month_choices": [(_shift_month(today.replace(day=1), i).strftime("%Y-%m-01"), _shift_month(today.replace(day=1), i))
                          for i in range(-6, 13)],
        "add_lists": board.lists.filter(is_archived=False) if board else [],
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


@login_required
def board_gantt(request, board_id):
    """Gantt chart: task table + bars, finish-to-start arrows, milestones, critical path."""
    from .models import Card, Dependency
    from .schedule import analyse, board_cards, span
    board, ctx = board_context(request, board_id, "gantt")
    today = timezone.localdate()
    zooms = {"day": 34, "week": 16, "month": 6}
    zoom = request.GET.get("z") if request.GET.get("z") in zooms else "week"
    col_w = zooms[zoom]
    show_items = request.GET.get("items") == "1"
    cards = list(board_cards(board).prefetch_related("children").order_by("list__position", "position"))
    ids = {c.pk for c in cards}
    # parts that live on other boards still show under their parent here
    extra = list(Card.objects.filter(parent__in=cards, is_archived=False, is_template=False).exclude(pk__in=ids)
                 .select_related("list__board").prefetch_related("members", "checklists__items", "children"))
    allc = cards + extra
    all_ids = {c.pk for c in allc}
    deps = list(Dependency.objects.filter(card__in=allc).values_list("card_id", "depends_on_id", "lag_days"))
    info = analyse(allc, deps)
    kids = {}
    for c in allc:
        if c.parent_id in all_ids and c.parent_id != c.pk:
            kids.setdefault(c.parent_id, []).append(c)

    def whole_span(c, seen=()):
        """Own dates plus every part's — the summary bar of a card with parts."""
        out = [(info[c.pk]["start"], info[c.pk]["end"])] if c.pk in info else []
        for k in kids.get(c.pk, []):
            if k.pk not in seen:
                sp = whole_span(k, seen + (c.pk,))
                if sp:
                    out.append(sp)
        return (min(s for s, _ in out), max(e for _, e in out)) if out else None

    dated = [v for v in info.values()]
    if dated:
        lo = min(v["start"] for v in dated) - timedelta(days=3)
        hi = max(v["end"] for v in dated) + timedelta(days=10)
    else:
        lo, hi = today - timedelta(days=3), today + timedelta(days=60)
    lo, hi = min(lo, today - timedelta(days=3)), max(hi, today + timedelta(days=14))
    try:
        lo = date.fromisoformat(request.GET["from"])
    except (KeyError, ValueError):
        pass
    if zoom != "day":
        lo -= timedelta(days=lo.weekday())
    span_days = max((hi - lo).days + 1, 42 if zoom != "month" else 120)
    days = [lo + timedelta(days=i) for i in range(span_days)]
    ROW = 36

    rows, pos = [], {}
    y = 0

    def add_card(c, level, seen):
        nonlocal y
        i = info.get(c.pk)
        children = sorted(kids.get(c.pk, []), key=lambda k: (info[k.pk]["start"] if k.pk in info else date.max, k.position))
        summary = whole_span(c) if children else None
        start, end = (summary if summary else ((i["start"], i["end"]) if i else (None, None)))
        row = {"card": c, "y": y, "pct": c.work_pct(), "owners": list(c.members.all())[:3], "level": level,
               "start": start, "end": end, "days": ((end - start).days + 1) if start else None,
               "critical": bool(i and i["critical"]) and not children, "late": bool(i and i["late"]),
               "float": i["float"] if i else None, "milestone": c.is_milestone and not children,
               "summary": bool(children), "other_board": c.list.board_id != board.pk}
        if start:
            x = (start - lo).days * col_w
            w = ((end - start).days + 1) * col_w
            row.update(x=x, w=w, visible=end >= lo and start <= days[-1])
            if i and not children:
                pos[c.pk] = (x, w, y + ROW // 2)
        rows.append(row)
        y += ROW
        kid_ids = {k.pk for k in children}
        if show_items:
            for cl in c.checklists.all():
                for it in cl.items.all():
                    if it.linked_card_id in kid_ids:
                        continue
                    irow = {"item": it, "card_id": c.pk, "y": y, "level": level + 1, "pct": round(100 * it.fraction),
                            "owners": [it.assignee] if it.assignee_id else [], "end": timezone.localdate(it.due_at) if it.due_at else None}
                    if irow["end"] and lo <= irow["end"] <= days[-1]:
                        irow["x"] = (irow["end"] - lo).days * col_w
                    rows.append(irow)
                    y += ROW
        for k in children:
            if k.pk not in seen:
                add_card(k, level + 1, seen | {c.pk})

    by_list = OrderedDict()
    for c in cards:
        if c.parent_id in all_ids:
            continue                      # shown under its parent
        by_list.setdefault(c.list, []).append(c)
    for lst, items in by_list.items():
        rows.append({"group": lst.title, "y": y})
        y += ROW
        items.sort(key=lambda c: ((whole_span(c) or (date.max,))[0], c.position))
        for c in items:
            add_card(c, 0, frozenset())
    arrows = []
    for cid, pid, lag in deps:
        if cid in pos and pid in pos:
            px, pw, py = pos[pid]
            sx, _, sy = pos[cid]
            x1, x2 = px + pw, sx
            if x2 - x1 >= 16:
                mid = x1 + 8
                d = f"M{x1},{py} H{mid} V{sy} H{x2 - 2}"
            else:
                drop = sy - (ROW // 2) if sy > py else sy + (ROW // 2)
                d = f"M{x1},{py} H{x1 + 8} V{drop} H{x2 - 10} V{sy} H{x2 - 2}"
            crit = info[cid]["critical"] and info[pid]["critical"]
            arrows.append({"d": d, "critical": crit, "late": info[cid]["late"], "card": cid, "dep": pid})
    months = []
    for d in days:
        if not months or months[-1]["key"] != (d.year, d.month):
            months.append({"key": (d.year, d.month), "label": d.strftime("%b %Y"), "n": 0})
        months[-1]["n"] += 1
    for m in months:          # a short label when only a few days of the month are showing
        w = m["n"] * col_w
        y_, mo = m["key"]
        if w < 40:
            m["label"] = ""
        elif w < 80:
            m["label"] = date(y_, mo, 1).strftime("%b")
    crit_days = sum(r.get("days") or 0 for r in rows if r.get("critical"))
    ctx.update({
        "zoom": zoom, "zooms": [("day", "Days"), ("week", "Weeks"), ("month", "Months")], "col_w": col_w,
        "days": days, "months": months, "rows": rows, "arrows": arrows, "row_h": ROW,
        "chart_w": len(days) * col_w, "chart_h": y, "lo": lo,
        "today_x": (today - lo).days * col_w + col_w // 2 if lo <= today <= days[-1] else None,
        "critical_count": sum(1 for r in rows if r.get("critical")),
        "late_count": sum(1 for r in rows if r.get("late")),
        "project_end": max((v["end"] for v in dated), default=None),
        "undated": sum(1 for r in rows if "card" in r and not r["start"]),
        "show_items": show_items, "q_items": "&items=1" if show_items else "",
        "dep_count": len(deps), "crit_days": crit_days,
        "prev": (lo - timedelta(days=28 if zoom != "month" else 90)).isoformat(),
        "next": (lo + timedelta(days=28 if zoom != "month" else 90)).isoformat(),
    })
    return render(request, "boards/view_gantt.html", ctx)


@login_required
def board_swimlanes(request, board_id):
    """The board split into rows — one per person or per label — so you can see who has what across the lists.
    Drag a card sideways to move it to another list, or down to give it to someone else (or another label)."""
    board, ctx = board_context(request, board_id, "swimlanes")
    by = request.GET.get("by") if request.GET.get("by") in ("member", "label") else "member"
    lists = list(board.lists.filter(is_archived=False).order_by("position", "pk"))
    cards = list(cards_qs().filter(list__board=board, list__is_archived=False, is_archived=False, is_template=False)
                 .order_by("position", "pk"))
    if by == "member":
        lanes = [(str(u.pk), u.display_name, u) for u in ctx["people"]] + [("none", "Nobody yet", None)]
        key = lambda c: [str(u.pk) for u in c.members.all()] or ["none"]  # noqa: E731
    else:
        lanes = [(str(lab.pk), lab.name or lab.get_colour_display(), lab) for lab in board.labels.all()] + \
                [("none", "No label", None)]
        key = lambda c: [str(lab.pk) for lab in c.labels.all()] or ["none"]  # noqa: E731
    cells = {}
    for c in cards:
        for k in key(c):
            cells.setdefault((k, c.list_id), []).append(c)
    rows = []
    for k, name, obj in lanes:
        row = [{"list": l, "cards": cells.get((k, l.pk), [])} for l in lists]
        n = sum(len(x["cards"]) for x in row)
        rows.append({"key": k, "name": name, "obj": obj, "cells": row, "count": n})
    hide_empty = request.GET.get("all") != "1"
    shown = [r for r in rows if r["count"] or not hide_empty]
    ctx.update({"by": by, "lists": lists, "rows": shown, "hidden_lanes": len(rows) - len(shown),
                "base_template": "boards/board_base.html"})
    return render(request, "boards/view_swimlanes.html", ctx)
