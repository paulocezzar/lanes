"""Card search across every board you can see.

Plain words match titles, descriptions, checklist items and comments.
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
    terms, ops = parse(query)
    if not terms and not ops:
        return [], terms, ops
    boards = permissions.visible_boards(user).filter(is_template=False)
    qs = cards_qs().filter(list__board__in=boards, list__is_archived=False, is_archived=False, is_template=False)
    for t in terms:
        qs = qs.filter(Q(title__icontains=t) | Q(description__icontains=t) | Q(checklists__items__text__icontains=t)
                       | Q(comments__body__icontains=t))
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
            elif v in ("complete", "done"):
                qs = qs.filter(is_complete=True)
            elif v in ("open", "incomplete"):
                qs = qs.filter(is_complete=False)
    return list(qs.distinct().order_by("is_complete", "due_at", "-updated_at")[:limit]), terms, ops
