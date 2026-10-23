"""Production planning on a board.

Job cards carry number fields "Target", "Made so far" and "Per day" (plus an
optional text "Unit"). `plan_days` turns what's left into one card per working
day — "Mon 26 Oct — production" with a checklist of "<job> — <qty> <unit>"
lines — in the order the jobs sit in their list, a few jobs a day. Ticking a
line adds its quantity to that job's "Made so far" (unticking takes it off).
"""
import re
from datetime import datetime, time, timedelta

from django.db import transaction
from django.utils import timezone

from .models import Card, CardFieldValue, Checklist, ChecklistItem, CustomField, Label

PLAN_LABEL = "Day plan"
LINE = re.compile(r"^(?P<job>.+?) — (?P<qty>[\d,]+(?:\.\d+)?)(?: (?P<unit>.*))?$")
NAMES = {"target": "target", "made": "made so far", "per_day": "per day", "unit": "unit"}


def _fields(board):
    by = {f.name.strip().lower(): f for f in CustomField.objects.filter(board=board)}
    return {k: by.get(v) for k, v in NAMES.items()}


def _num(v):
    try:
        n = float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0
    return int(n) if n == int(n) else n


def job_numbers(card, fields=None):
    fields = fields or _fields(card.list.board)
    vals = {v.field_id: v.value for v in CardFieldValue.objects.filter(card=card)}
    get = lambda k: vals.get(fields[k].pk) if fields.get(k) else None
    return {"target": _num(get("target")), "made": _num(get("made")), "per_day": _num(get("per_day")),
            "unit": (get("unit") or "").strip()}


def progress(card):
    """(made, target, percent) for a card with Target / Made so far fields, else None. Uses prefetched values."""
    vals = {v.field.name.strip().lower(): v.value for v in card.field_values.all()}
    target = _num(vals.get("target"))
    if not target:
        return None
    made = _num(vals.get("made so far"))
    return made, target, max(0, min(100, round(100 * made / target)))


def _fmt(n):
    return f"{n:,}" if isinstance(n, int) else f"{n:,.1f}"


def _label(board):
    lab = Label.objects.filter(board=board, name=PLAN_LABEL).first()
    return lab or Label.objects.create(board=board, name=PLAN_LABEL, colour="sky")


def plan_days(board, jobs_list, days_list, *, jobs_per_day=3, weekdays=(0, 1, 2, 3, 4), start=None, user=None):
    """(Re)build the day cards. Future day cards nobody has ticked yet are replaced;
    started ones are kept and their unticked lines count as already planned."""
    fields = _fields(board)
    if not (fields["target"] and fields["per_day"]):
        return 0
    label = _label(board)
    today = timezone.localdate()
    start = start or today + timedelta(days=1)
    with transaction.atomic():
        planned = {}
        kept_dates = set()
        for c in Card.objects.filter(list=days_list, labels=label, is_archived=False, start_date__gte=start):
            items = list(ChecklistItem.objects.filter(checklist__card=c))
            if not any(i.is_done for i in items):
                c.delete()
                continue
            kept_dates.add(c.start_date)
            for i in items:
                m = LINE.match(i.text)
                if m and not i.is_done:
                    planned[m["job"]] = planned.get(m["job"], 0) + _num(m["qty"])
        jobs = []
        for card in jobs_list.cards.filter(is_archived=False, is_template=False, is_complete=False).order_by("position"):
            n = job_numbers(card, fields)
            left = n["target"] - n["made"] - planned.get(card.title, 0)
            if left > 0 and n["per_day"] > 0:
                jobs.append({"card": card, "left": left, "per_day": n["per_day"], "unit": n["unit"]})
        made, day = 0, start
        while any(j["left"] > 0 for j in jobs) and made < 120:
            if day.weekday() in weekdays and day not in kept_dates:
                picks = [j for j in jobs if j["left"] > 0][:max(1, jobs_per_day)]
                c = Card.objects.create(
                    list=days_list, title=f"{day:%a %-d %b} — production", start_date=day,
                    due_at=timezone.make_aware(datetime.combine(day, time(17, 0))), created_by=user,
                    description=("Planned from the **Jobs** list. Tick a line when it's made — the amount is added "
                                 "to that job's *Made so far*. Press **📅 Re-plan days** after changing targets."),
                    position=(days_list.cards.count() + 1) * 1024)
                c.labels.add(label)
                cl = Checklist.objects.create(card=c, title="Today's production")
                for k, j in enumerate(picks, start=1):
                    q = min(j["per_day"], j["left"])
                    j["left"] -= q
                    text = f"{j['card'].title} — {_fmt(q)}{' ' + j['unit'] if j['unit'] else ''}"
                    ChecklistItem.objects.create(checklist=cl, text=text[:250], position=k * 1024)
                made += 1
            day += timedelta(days=1)
        # keep the day cards in date order
        for k, c in enumerate(days_list.cards.filter(is_archived=False).order_by("is_template", "start_date", "pk"), start=1):
            Card.objects.filter(pk=c.pk).update(position=k * 1024)
    from .signals import bump
    bump(board.pk)
    return made


def on_item_toggle(item, done):
    """A day-plan line was ticked/unticked: move its quantity onto the job's Made so far."""
    card = item.checklist.card
    board = card.list.board
    if not card.labels.filter(name=PLAN_LABEL).exists():
        return None
    m = LINE.match(item.text)
    if not m:
        return None
    job = Card.objects.filter(list__board=board, title=m["job"], is_archived=False).exclude(pk=card.pk).first()
    fields = _fields(board)
    if not job or not fields["made"]:
        return None
    qty = _num(m["qty"])
    cfv = CardFieldValue.objects.filter(card=job, field=fields["made"]).first()
    now = _num(cfv.value) if cfv else 0
    new = max(0, now + (qty if done else -qty))
    CardFieldValue.objects.update_or_create(card=job, field=fields["made"], defaults={"value": new})
    from .signals import bump
    bump(board.pk)
    return job
