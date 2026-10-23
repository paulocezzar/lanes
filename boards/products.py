"""Type a product and what it needs; get a card that tracks how much is done.

    AF Christmas Cake — 430 cakes          ← product, planned quantity, unit
    Packaging:                             ← a line ending in ':' starts a checklist
    1 x Printed Product Label              ← per product: 430 needed
    217.417 kg Fruit Soak                  ← a total (scales if Planned changes)
    [x] 387 kg Christmas Cake Batter       ← already done
    Printed Product Label artwork          ← no number: a plain tick

Lines may start with '-', '•' or '*'. A checklist called 'What it needs' is used until
the first heading.
"""
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from . import positions
from .models import Card, CardFieldValue, Checklist, ChecklistItem, CustomField

NUM = r"(\d[\d,]*(?:\.\d+)?)"
UNITS = {"kg", "g", "l", "ml", "each", "pcs", "pc", "units", "unit", "trays", "tray", "sheets", "boxes", "packs",
         "cases", "labels", "litres", "liters", "dozen", "batches", "batch", "loaves", "cakes", "pies", "tarts"}
HEAD = re.compile(r"^(.*?)\s*(?:[—–:]|\s-\s|\s+)\s*" + NUM + r"(?:\s+([^\d\s][\w ]{0,19}))?\s*$")
PER = re.compile(r"^" + NUM + r"\s*[x×]\s+(.+)$", re.I)
TOTAL = re.compile(r"^" + NUM + r"\s*([a-z]+)\s+(.+)$", re.I)
TRAIL = re.compile(r"^(.+?)\s+(?:[—–-]|[x×])\s*" + NUM + r"\s*([a-z]+)?$", re.I)
DONE = re.compile(r"^\[(x|✓)\]\s*|^✓\s*", re.I)


def _d(s):
    try:
        return Decimal(s.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def parse(text):
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise ValueError("Type the product name on the first line.")
    first = lines[0]
    title, planned, unit = first, None, ""
    m = HEAD.match(first)
    if m and m.group(1).strip():
        title, planned, unit = m.group(1).strip(" —–-:"), _d(m.group(2)), (m.group(3) or "").strip()
    sections, current = [], None
    for raw in lines[1:]:
        line = re.sub(r"^[-•*]\s+", "", raw).strip()
        if not line:
            continue
        if line.endswith(":") and len(line) <= 80:
            current = {"title": line[:-1].strip() or "Checklist", "items": []}
            sections.append(current)
            continue
        if current is None:
            current = {"title": "What it needs", "items": []}
            sections.append(current)
        done = bool(DONE.match(line))
        line = DONE.sub("", line).strip()
        item = {"text": line, "per_unit": None, "target": None, "unit": "", "done": done}
        m = PER.match(line)
        if m:
            item.update(text=m.group(2).strip(), per_unit=_d(m.group(1)))
        else:
            m = TOTAL.match(line)
            if m and m.group(2).lower() in UNITS:
                item.update(text=m.group(3).strip(), target=_d(m.group(1)), unit=m.group(2))
            else:
                m = TRAIL.match(line)
                if m:
                    item.update(text=m.group(1).strip(" —–-"), target=_d(m.group(2)), unit=(m.group(3) or ""))
        if item["per_unit"] is not None:
            item["unit"] = item["unit"] or "each"
            if planned:
                item["target"] = planned * item["per_unit"]
        elif item["target"] is not None and planned:
            item["per_unit"] = (item["target"] / planned).quantize(Decimal("0.0001"))
        current["items"].append(item)
    return {"title": title[:250], "planned": planned, "unit": unit[:20], "sections": sections}


def _field(board, name, kind, show, aliases=()):
    names = {name.lower(), *aliases}
    for f in board.custom_fields.all():
        if f.name.strip().lower() in names and f.kind == kind:
            return f
    top = (board.custom_fields.order_by("-position").first() or CustomField(position=0)).position
    return CustomField.objects.create(board=board, name=name, kind=kind, show_on_card=show, position=top + 1)


def create(lst, text, user, *, position=None, extra_fields=None):
    """Make the card. Returns it."""
    p = parse(text)
    board = lst.board
    with transaction.atomic():
        card = Card.objects.create(list=lst, title=p["title"], created_by=user,
                                   position=position if position is not None else positions.end_position(lst.cards.all()))
        if p["planned"] is not None:
            f = _field(board, "Planned", CustomField.NUMBER, True, aliases=("quantity", "target"))
            v = p["planned"]
            CardFieldValue.objects.create(card=card, field=f, value=int(v) if v == v.to_integral() else float(v))
        if p["unit"]:
            CardFieldValue.objects.create(card=card, field=_field(board, "Unit", CustomField.TEXT, True), value=p["unit"])
        for i, sec in enumerate(p["sections"], start=1):
            cl = Checklist.objects.create(card=card, title=sec["title"][:120], position=i * positions.GAP)
            for k, it in enumerate(sec["items"], start=1):
                tgt = it["target"].quantize(Decimal("0.001")) if it["target"] is not None else None
                done = bool(it["done"])
                ChecklistItem.objects.create(
                    checklist=cl, text=it["text"][:250], position=k * positions.GAP,
                    qty_target=tgt, qty_unit=it["unit"][:20], per_unit=it["per_unit"],
                    qty_done=tgt if (done and tgt) else 0, is_done=done,
                    done_by=user if done else None, done_at=timezone.now() if done else None)
        for name, (kind, show, value) in (extra_fields or {}).items():
            CardFieldValue.objects.create(card=card, field=_field(board, name, kind, show), value=value)
    return card


EXAMPLE = """AF Christmas Cake — 430 cakes
Packaging:
Printed Product Label artwork
1 x Printed Product Label
1 x 5mm Black Round Matt Masonite Board (8")
1 x Cake/Biscuit Tin
Make:
217.417 kg Fruit Soak revised (Christmas Cake) 2026
1 x AF Christmas Cake (Baked)
Finish:
1 x Decorate
1 x Pack"""
