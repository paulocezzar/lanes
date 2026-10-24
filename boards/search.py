"""Card search across every board you can see.

Plain words match titles, descriptions, checklist items, comments, attachment names, label names,
list names and custom field values. Everyday phrases work too: "my overdue cards", "due next week",
"waiting for sign-off", "at risk", "done" — they're turned into the operators below first.
Operators (combine freely):
  @me              cards assigned to me          @charlie   assigned to a person
  label:urgent     has that label (by name)      board:xmas board title contains
  list:done        list title contains           is:overdue / is:due-week / is:no-date
  is:complete      is:open                       is:mine (same as @me)
  "exact phrase"   quoted text matches as a whole
"""
import re
import shlex
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from . import permissions
from .services import cards_qs

User = get_user_model()


# Everyday phrases → operators. Longest first so "due next week" wins over "next week".
PHRASES = [
    (r"\b(assigned to me|for me|my cards|mine|my)\b", "@me"),
    (r"\b(overdue|late|past due|behind)\b", "is:overdue"),
    (r"\bdue today\b|\btoday\b", "is:due-today"),
    (r"\bdue tomorrow\b|\btomorrow\b", "is:due-tomorrow"),
    (r"\bdue next week\b|\bnext week\b", "is:due-next-week"),
    (r"\bdue this week\b|\bthis week\b|\bdue soon\b", "is:due-week"),
    (r"\b(no|without a?) (due )?dates?\b|\bundated\b", "is:no-date"),
    (r"\b(waiting for|needs?|awaiting) sign[- ]?offs?\b|\bto sign off\b|\bsign[- ]?offs?\b", "is:signoff"),
    (r"\bat risk\b|\bslipping\b", "is:risk"),
    (r"\bnot done\b|\bunfinished\b|\bstill open\b|\bopen\b|\bto do\b", "is:open"),
    (r"\b(done|completed?|finished)\b", "is:complete"),
    (r"\b(nobody|no one|unassigned)\b", "is:unassigned"),
]
STOP = {"cards", "card", "about", "with", "the", "for", "show", "me", "find", "all", "that", "are", "is", "in", "a", "an",
        "to", "of", "which", "what", "any", "and", "on", "please", "list", "things", "stuff", "tasks", "jobs", "due", "where"}


def from_words(query):
    """'my overdue cards about packaging' → '@me is:overdue packaging'. Leaves operator queries alone."""
    q = (query or "").strip()
    if not q or re.search(r"\b(label|board|list|is|member):|@\w", q) or '"' in q:
        return q
    low, ops = q.lower(), []
    for pat, op in PHRASES:
        if re.search(pat, low):
            low = re.sub(pat, " ", low)
            if op not in ops:
                ops.append(op)
    if not ops:
        return q
    words = [w for w in re.findall(r"[\w'’&-]+", low) if w not in STOP]
    return " ".join(ops + words)


def where_matched(card, terms):
    """A short hint of where a plain word was found, e.g. 'in a checklist item: “Order flour”'."""
    if not terms:
        return ""
    t = terms[0].lower()
    if t in card.title.lower():
        return ""
    for cl in card.checklists.all():
        for i in cl.items.all():
            if t in i.text.lower():
                return f"checklist: “{i.text[:60]}”"
    for c in card.comments.all():
        if t in c.body.lower():
            return f"comment: “{c.body[:60]}”"
    for a in card.attachments.all():
        if t in a.name.lower():
            return f"attachment: {a.name[:60]}"
    if t in (card.description or "").lower():
        return "description"
    for v in card.field_values.all():
        if t in str(v.value).lower():
            return f"{v.field.name}: {str(v.value)[:40]}"
    return ""


def parse(query):
    try:
        parts = shlex.split(query or "")
    except ValueError:
        parts = (query or "").split()
    terms, ops = [], []
    for p in parts:
        m = re.match(r"^(label|board|list|is|member):(.+)$", p, re.I)
        if m:
            ops.append((m.group(1).lower(), m.group(2)))
        elif p.startswith("@") and len(p) > 1:
            ops.append(("member", p[1:]))
        else:
            terms.append(p)
    return terms, ops


def run(user, query, limit=200):
    risky = False
    terms, ops = parse(query)
    if not terms and not ops:
        return [], terms, ops
    boards = permissions.visible_boards(user).filter(is_template=False)
    qs = cards_qs().filter(list__board__in=boards, list__is_archived=False, is_archived=False, is_template=False)
    for t in terms:
        qs = qs.filter(Q(title__icontains=t) | Q(description__icontains=t) | Q(checklists__items__text__icontains=t)
                       | Q(comments__body__icontains=t) | Q(attachments__name__icontains=t)
                       | Q(labels__name__icontains=t) | Q(field_values__value__icontains=t))
    now = timezone.now()
    for op, val in ops:
        v = val.lower()
        if op == "label":
            qs = qs.filter(labels__name__icontains=val)
        elif op == "board":
            qs = qs.filter(list__board__title__icontains=val)
        elif op == "list":
            qs = qs.filter(list__title__icontains=val)
        elif op == "member" or (op == "is" and v == "mine"):
            if op == "is" or v == "me":
                qs = qs.filter(members=user)
            else:
                qs = qs.filter(Q(members__first_name__iexact=val) | Q(members__email__istartswith=val + "@")
                               | Q(members__first_name__icontains=val))
        elif op == "is":
            if v == "overdue":
                qs = qs.filter(due_at__lt=now, is_complete=False)
            elif v in ("due-week", "dueweek", "week"):
                qs = qs.filter(due_at__gte=now, due_at__lte=now + timedelta(days=7), is_complete=False)
            elif v in ("no-date", "nodate", "undated"):
                qs = qs.filter(due_at__isnull=True)
            elif v in ("due-today", "today"):
                end = timezone.localtime(now).replace(hour=23, minute=59, second=59)
                qs = qs.filter(due_at__lte=end, due_at__gte=end - timedelta(days=1), is_complete=False)
            elif v in ("due-tomorrow", "tomorrow"):
                end = timezone.localtime(now).replace(hour=23, minute=59, second=59) + timedelta(days=1)
                qs = qs.filter(due_at__lte=end, due_at__gt=end - timedelta(days=1), is_complete=False)
            elif v in ("due-next-week", "next-week"):
                today = timezone.localdate()
                start = today + timedelta(days=7 - today.weekday())
                qs = qs.filter(due_at__date__gte=start, due_at__date__lt=start + timedelta(days=7))
            elif v in ("signoff", "sign-off", "waiting"):
                qs = qs.filter(signoffs__status__in=["pending", "rejected"])
            elif v in ("unassigned", "nobody"):
                qs = qs.filter(members__isnull=True)
            elif v in ("risk", "at-risk"):
                from .risk import WINDOW_DAYS
                qs = qs.filter(due_at__gt=now, due_at__lte=now + timedelta(days=WINDOW_DAYS), is_complete=False)
                risky = True
            elif v in ("complete", "done"):
                qs = qs.filter(is_complete=True)
            elif v in ("open", "incomplete"):
                qs = qs.filter(is_complete=False)
    cards = list(qs.distinct().order_by("is_complete", "due_at", "-updated_at")[:limit])
    if risky:
        from .risk import reason
        cards = [c for c in cards if reason(c, now)]
    return cards, terms, ops
