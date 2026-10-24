"""Type something and what it needs; get a card that tracks how much is done.

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
FIELD = re.compile(r"^([A-Za-z][\w ]{0,40}):\s+(\S.*)$")
# common fields also read without the colon: "Deadline 15 November", "Forecast 407"
KNOWN_FIELD = re.compile(r"^(deadline|due|due date|launch|start|start date|date|forecast|budget|owner|notes?|description)"
                         r"\s+(\S.*)$", re.I)
# "Bake 500 Christmas Cakes" → Bake, 0 / 500 Christmas Cakes
MID = re.compile(r"^([A-Za-z][^\d]*?)\s+" + NUM + r"\s*([A-Za-z][\w &'/-]{0,30})$")
DONE = re.compile(r"^\[(x|✓)\]\s*|^✓\s*", re.I)
# "430 Christmas Cake (Baked)" → 0 / 430 Christmas Cake (Baked); not "2027 plan"
LEAD = re.compile(r"^" + NUM + r"\s+(\S.*)$")


def _d(s):
    try:
        return Decimal(s.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def parse_item(line, planned=None):
    """One line → {text, per_unit, target, unit, done}.
    '1 x Chair' (per one of the card's amount), '20 kg Salad' (a total), '[x] …' (done), else a plain tick."""
    line = re.sub(r"^[-•*]\s+", "", (line or "").strip()).strip()
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
            else:
                m = MID.match(line)
                if m:
                    item.update(text=m.group(1).strip(), target=_d(m.group(2)), unit=m.group(3).strip()[:20])
                else:
                    m = LEAD.match(line)
                    n = _d(m.group(1)) if m else None
                    if n and not (n == n.to_integral() and 1900 <= n <= 2100 and "," not in m.group(1)):
                        item.update(text=m.group(2).strip(), target=n)
    item["kind"] = "per" if item["per_unit"] is not None else "total" if item["target"] is not None else "tick"
    if item["per_unit"] is not None:
        item["unit"] = item["unit"] or "each"
        if planned:
            item["target"] = planned * item["per_unit"]
    elif item["target"] is not None and planned:
        item["per_unit"] = (item["target"] / planned).quantize(Decimal("0.0001"))
    return item


def make_item(cl, it, user, position):
    tgt = it["target"].quantize(Decimal("0.001")) if it["target"] is not None else None
    done = bool(it["done"])
    return ChecklistItem.objects.create(
        checklist=cl, text=it["text"][:250] or "Item", position=position,
        qty_target=tgt, qty_unit=it["unit"][:20], per_unit=it["per_unit"],
        qty_done=tgt if (done and tgt) else 0, is_done=done,
        done_by=user if done else None, done_at=timezone.now() if done else None)


def set_amount(card, amount, unit=None):
    """Give a card an amount ('430 cakes', '120 guests') — stored in its board's Planned/Unit fields."""
    board = card.list.board
    f = _field(board, "Planned", CustomField.NUMBER, True, aliases=("quantity", "target"))
    if amount is None:
        CardFieldValue.objects.filter(card=card, field=f).delete()
    else:
        CardFieldValue.objects.update_or_create(card=card, field=f, defaults={
            "value": int(amount) if amount == amount.to_integral() else float(amount)})
    if unit is not None:
        fu = _field(board, "Unit", CustomField.TEXT, True)
        if unit:
            CardFieldValue.objects.update_or_create(card=card, field=fu, defaults={"value": unit[:20]})
        else:
            CardFieldValue.objects.filter(card=card, field=fu).delete()


def parse(text):
    lines = [ln.strip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        raise ValueError("Type what it's for on the first line — e.g. “Summer fair — 200 guests”.")
    first = lines[0]
    title, planned, unit = first, None, ""
    m = HEAD.match(first)
    if m and m.group(1).strip():
        title, planned, unit = m.group(1).strip(" —–-:"), _d(m.group(2)), (m.group(3) or "").strip()
    sections, current, fields, notes = [], None, [], []
    for raw in lines[1:]:
        line = re.sub(r"^[-•*]\s+", "", raw).strip()
        if not line:
            continue
        # before the first section: "Launch: 6 Oct 2026", "Forecast: 407", "Notes: …" fill the card's fields
        fm = (FIELD.match(line) if current is None else None) or _known_field(line)
        if fm:
            key, val = fm.group(1).strip(), fm.group(2).strip()
            if key.lower() in ("notes", "note", "description"):
                notes.append(val)
            else:
                fields.append((key, val))
            continue
        if line.endswith(":") and len(line) <= 80:
            current = {"title": line[:-1].strip() or "Checklist", "items": []}
            sections.append(current)
            continue
        if current is None:
            current = {"title": "What it needs", "items": []}
            sections.append(current)
        current["items"].append(parse_item(line, planned))
    return {"title": title[:250], "planned": planned, "unit": unit[:20], "sections": sections,
            "fields": fields, "notes": "\n".join(notes)}


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
                make_item(cl, it, user, k * positions.GAP)
        _typed_fields(card, board, p)
        if p["notes"]:
            card.description = p["notes"]
            card.save(update_fields=["description"])
        for name, (kind, show, value) in (extra_fields or {}).items():
            CardFieldValue.objects.create(card=card, field=_field(board, name, kind, show), value=value)
    return card


def _parse_date(v):
    from datetime import datetime
    from django.utils.dateparse import parse_date
    d = parse_date(v)
    if d:
        return d
    today = timezone.localdate()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y", "%d %b", "%d %B"):
        try:
            d = datetime.strptime(v.strip(), fmt).date()
            return d.replace(year=today.year) if "%Y" not in fmt and "%y" not in fmt else d
        except ValueError:
            continue
    return None


def _typed_fields(card, board, p):
    """'Key: value' lines → the board's fields of that name (made if missing). Forecast + Planned → Variance."""
    existing = {f.name.strip().lower(): f for f in board.custom_fields.all()}
    vals = {}
    for key, val in p["fields"]:
        f = existing.get(key.lower())
        if not f:
            kind = (CustomField.DATE if _parse_date(val) and not _d(val)
                    else CustomField.NUMBER if _d(val) is not None else CustomField.TEXT)
            f = _field(board, key[:60], kind, kind == CustomField.DATE)
            existing[key.lower()] = f
        if f.kind == CustomField.DATE:
            d = _parse_date(val)
            if not d:
                continue
            value = d.isoformat()
        elif f.kind == CustomField.NUMBER:
            n = _d(val)
            if n is None:
                continue
            value = int(n) if n == n.to_integral() else float(n)
        else:
            value = val[:250]
        CardFieldValue.objects.update_or_create(card=card, field=f, defaults={"value": value})
        vals[f.name.strip().lower()] = value
    fc, plan = vals.get("forecast"), p["planned"]
    if fc and plan and "variance" not in vals:
        var = float(plan) - float(fc)
        f = existing.get("variance") or _field(board, "Variance", CustomField.TEXT, False)
        CardFieldValue.objects.update_or_create(card=card, field=f, defaults={
            "value": f"{var:+,.0f} ({var / float(fc) * 100:+.1f}%)" if var else "—"})


def explain_item(it, amount=None, where="card"):
    """Plain words for how a line is read, e.g. 'counts to 300 kg'."""
    from .models import ChecklistItem
    f = ChecklistItem.fmt_qty
    name = it["text"] or "this"
    kind = it.get("kind") or ("per" if it["per_unit"] is not None else "total" if it["target"] is not None else "tick")
    if kind == "per":
        per = f"{f(it['per_unit'])} for each"
        if it["target"] is not None and amount:
            return f"{per} of {amount['amount']} {amount['unit']}".rstrip() + f" — counts to {f(it['target'])}"
        if where == "setup":
            return f"{per} (needs an amount on the first line)"
        return f"{per} — set the card's ＃ Amount to work out the total"
    if kind == "total":
        done = " · already done" if it["done"] else ""
        return f"counts to {f(it['target'])} {it['unit']}".rstrip() + done
    return "a tick box" + (" · already done" if it["done"] else "")


def _known_field(line):
    """'Deadline 15 November' / 'Forecast 407' without a colon — only when the value fits the word,
    so 'Start the oven' stays a line to tick."""
    m = KNOWN_FIELD.match(line)
    if not m:
        return None
    key, val = m.group(1).lower(), m.group(2).strip()
    if key in ("deadline", "due", "due date", "launch", "start", "start date", "date"):
        return m if _parse_date(val) else None
    if key in ("forecast", "budget"):
        return m if _d(val.lstrip("£$€")) is not None else None
    return m


def _starts_card(block):
    """Does a block (after a blank line) look like a new card, rather than more of the one above?
    Yes when its first line gives an amount after a dash ('Winter fair — 120 guests') or it has a
    section ('Setup:') or a field line ('Deadline: 4 Jul')."""
    first = block[0].strip()
    if first.endswith(":") or PER.match(first) or DONE.match(first) or \
            (TOTAL.match(first) and TOTAL.match(first).group(2).lower() in UNITS):
        return False                     # a section or an item carries on the card above
    if re.search(r"\s[—–]\s*\d|\s-\s+\d|:\s*\d[\d,.]*\s*[A-Za-z ]*$", first) and not first.endswith(":"):
        return True
    return any(ln.strip().endswith(":") and len(ln.strip()) <= 80 for ln in block[1:]) or \
        any(FIELD.match(ln.strip()) or _known_field(ln.strip()) for ln in block[1:2])


def split_cards(text):
    """Several cards in one box. '---' always starts the next card; after a blank line a new card starts
    only when the next lines look like one (see _starts_card) — otherwise blank lines are just spacing.
    A line '# Name' puts the cards after it into that list. Returns [(list name or None, card text)]."""
    out, blocks, lst = [], [], None        # blocks: [(list, [lines], hard_break)]
    cur, hard = [], True

    def push():
        nonlocal cur, hard
        if any(ln.strip() for ln in cur):
            blocks.append((lst, cur, hard))
        cur, hard = [], False

    for raw in (text or "").splitlines():
        line = raw.rstrip()
        st = line.strip()
        if st.startswith("# ") or (st.startswith("#") and st[1:2] == "#"):
            push(); hard = True
            lst = st.lstrip("#").strip()[:120] or None
        elif st and set(st) <= {"-", "—", "=", "_"} and len(st) >= 3:
            push(); hard = True
        elif not st:
            if cur:
                push()
        else:
            cur.append(line)
    push()
    for lst_name, lines, hard_break in blocks:
        if out and not hard_break and out[-1][0] == lst_name and not _starts_card(lines):
            out[-1] = (lst_name, out[-1][1] + "\n" + "\n".join(lines))
        else:
            out.append((lst_name, "\n".join(lines).strip()))
    return out


def parse_many(text):
    """[{list, title, planned, unit, sections, fields, notes}] — raises ValueError on an empty box."""
    blocks = split_cards(text)
    if not blocks:
        raise ValueError("Type at least one card — what it's for on the first line, then what it needs.")
    cards = []
    for lst, block in blocks[:60]:
        p = parse(block)
        p["list"] = lst
        p["text"] = block
        cards.append(p)
    return cards


def create_many(board, text, user, default_list):
    """Make every card in the box. Lists named with '# Name' are found (or made). Returns the cards."""
    from .models import List
    cards = []
    with transaction.atomic():
        lists = {lst.title.strip().lower(): lst for lst in board.lists.filter(is_archived=False)}
        for p in parse_many(text):
            target = default_list
            if p["list"]:
                key = p["list"].strip().lower()
                if key not in lists:
                    lists[key] = List.objects.create(board=board, title=p["list"],
                                                     position=positions.end_position(board.lists.all()))
                target = lists[key]
            cards.append(create(target, p["text"], user))
    return cards


EXAMPLE = """Summer fair — 200 guests
Forecast: 180
Notes: numbers from last year plus 10%
Setup:
1 x Chair
0.125 x Table
Food:
30 kg Salad
1 x Plate
Before the day:
Book the band"""
