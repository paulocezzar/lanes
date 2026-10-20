"""The premium board views: Calendar, Table, Timeline and Dashboard.

All read from the same cards as the Kanban board; clicking a card anywhere
opens the same card dialog. Calendar supports drag-to-reschedule.
"""
import calendar as pycal
from collections import Counter, OrderedDict
from datetime import date, timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from .board_context import board_context, open_cards


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
def board_calendar(request, board_id):
    board, ctx = board_context(request, board_id, "calendar")
    today = timezone.localdate()
    month = _parse_month(request.GET.get("m"), today)
    weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)
    first, last = weeks[0][0], weeks[-1][-1]

    cards = list(open_cards(board))
    by_day = {}
    unscheduled = []
    for c in cards:
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
        "weeks": [[{"date": d, "cards": by_day.get(d, []), "in_month": d.month == month.month} for d in w]
                  for w in weeks],
        "unscheduled": unscheduled,
        "first_list": board.lists.filter(is_archived=False).first(),
    })
    return render(request, "boards/view_calendar.html", ctx)


@login_required
def board_table(request, board_id):
    board, ctx = board_context(request, board_id, "table")
    cards = list(open_cards(board).order_by("list__position", "position"))
    fields = list(board.custom_fields.all())
    rows = []
    for c in cards:
        done, total = c.checklist_counts()
        values = {v.field_id: v for v in c.field_values.all()}
        rows.append({"card": c, "done": done, "total": total,
                     "pct": round(100 * done / total) if total else None,
                     "fields": [values.get(f.pk) for f in fields]})
    ctx.update({"rows": rows, "lists": board.lists.filter(is_archived=False), "fields": fields})
    return render(request, "boards/view_table.html", ctx)


@login_required
def board_timeline(request, board_id):
    board, ctx = board_context(request, board_id, "timeline")
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET.get("start", ""))
    except ValueError:
        start = today - timedelta(days=7)
    start -= timedelta(days=start.weekday())          # always begin on a Monday
    days = 42                                         # six weeks
    end = start + timedelta(days=days - 1)

    groups = OrderedDict((lst.pk, {"list": lst, "rows": []}) for lst in board.lists.filter(is_archived=False))
    undated = 0
    for c in open_cards(board).order_by("list__position", "due_at"):
        due = _local_date(c.due_at)
        if not due:
            undated += 1
            continue
        begin = c.start_date or due
        if begin > due:
            begin = due
        if due < start or begin > end:
            continue
        col_start = max((begin - start).days, 0)
        col_end = min((due - start).days, days - 1)
        groups[c.list_id]["rows"].append({
            "card": c,
            "col": col_start + 1,                         # CSS grid lines are 1-based
            "span": col_end - col_start + 1,
            "clipped_left": begin < start,
            "clipped_right": due > end,
        })

    ctx.update({
        "start": start,
        "days": [start + timedelta(days=i) for i in range(days)],
        "day_count": days,
        "today": today,
        "today_col": (today - start).days + 1 if start <= today <= end else None,
        "groups": [g for g in groups.values()],
        "undated": undated,
        "prev": (start - timedelta(days=28)).isoformat(),
        "next": (start + timedelta(days=28)).isoformat(),
    })
    return render(request, "boards/view_timeline.html", ctx)


def _bars(counter, total=None):
    """[(label, count, pct_of_max)] sorted biggest first, for CSS bar charts."""
    items = counter.most_common()
    top = max((n for _, n in items), default=0) or 1
    return [{"label": k, "count": n, "pct": round(100 * n / top)} for k, n in items]


@login_required
def board_dashboard(request, board_id):
    board, ctx = board_context(request, board_id, "dashboard")
    cards = list(open_cards(board))
    today = timezone.localdate()
    week_end = today + timedelta(days=7)

    per_list = OrderedDict((lst.title, 0) for lst in board.lists.filter(is_archived=False))
    per_member, per_label, status = Counter(), Counter(), Counter()
    items_done = items_total = 0
    for c in cards:
        per_list[c.list.title] = per_list.get(c.list.title, 0) + 1
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

    top_list = max(per_list.values(), default=0) or 1
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
        "by_list": [{"label": k, "count": n, "pct": round(100 * n / top_list)} for k, n in per_list.items()],
        "by_member": _bars(per_member),
        "by_label": [{"label": k[0], "colour": k[1], "count": n, "pct": round(100 * n / top_label)}
                     for k, n in per_label.most_common()],
        "by_status": [{"label": k, "count": status[k], "pct": round(100 * status[k] / top_status),
                       "key": k.lower().replace(" ", "-")} for k in status_order],
    })
    return render(request, "boards/view_dashboard.html", ctx)
