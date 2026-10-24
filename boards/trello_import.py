"""Import a board from Trello's JSON export (Trello: board menu → Print, export and share → Export as JSON).

Brings across lists, cards, labels, descriptions, dates, completion, checklists (with item due dates and
people), custom fields, comments (credited to the original author in the text), link attachments and
archived cards and lists. People are matched to Lanes accounts by name or email/username in the
department; files uploaded to Trello stay on Trello, so they come across as links.
"""
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import positions
from .models import (Attachment, Board, BoardMember, Card, CardFieldValue, Checklist, ChecklistItem, Comment,
                     CustomField, Label, List, WorkspaceMember)

COLOURS = {"green": "green", "yellow": "yellow", "orange": "orange", "red": "red", "purple": "purple",
           "blue": "blue", "sky": "sky", "lime": "green", "pink": "pink", "black": "grey"}
MAX_BYTES = 30 * 1024 * 1024


class ImportError_(ValueError):
    pass


def _colour(c):
    c = (c or "").split("_")[0]          # green_dark / green_light → green
    return COLOURS.get(c, "grey")


def _dt(v):
    if not v:
        return None
    d = parse_datetime(v) if isinstance(v, str) else None
    if d and timezone.is_naive(d):
        d = timezone.make_aware(d)
    return d


def _people(workspace):
    """Lanes accounts in the department, findable by full name, first name, email or email name."""
    index = {}
    for m in WorkspaceMember.objects.filter(workspace=workspace).select_related("user"):
        u = m.user
        keys = {u.display_name.strip().lower(), u.email.lower(), u.email.split("@")[0].lower(),
                f"{u.first_name} {u.last_name}".strip().lower()}
        for k in keys:
            if k:
                index.setdefault(k, u)
    return index


def check(data):
    if not isinstance(data, dict) or "lists" not in data or "cards" not in data:
        raise ImportError_("That isn't a Trello board export. In Trello open the board → ⋯ menu → "
                           "Print, export and share → Export as JSON, and upload that file.")


@transaction.atomic
def import_board(data, *, workspace, user, title=None, visibility=Board.WORKSPACE):
    check(data)
    board = Board.objects.create(workspace=workspace, title=(title or data.get("name") or "Imported board")[:120],
                                 created_by=user, description=(data.get("desc") or "")[:5000], visibility=visibility)
    BoardMember.objects.create(board=board, user=user, role=BoardMember.ADMIN)
    people = _people(workspace)
    members = {}
    unmatched = set()
    for m in data.get("members") or []:
        u = None
        for key in (m.get("fullName"), m.get("username")):
            if key and key.strip().lower() in people:
                u = people[key.strip().lower()]
                break
        if u:
            members[m["id"]] = u
            if u != user:
                BoardMember.objects.get_or_create(board=board, user=u, defaults={"role": BoardMember.MEMBER})
        else:
            unmatched.add(m.get("fullName") or m.get("username") or "someone")
    names = {m["id"]: (m.get("fullName") or m.get("username") or "Someone") for m in data.get("members") or []}

    labels = {}
    for lab in data.get("labels") or []:
        labels[lab["id"]] = Label.objects.create(board=board, name=(lab.get("name") or "")[:40], colour=_colour(lab.get("color")))

    fields, options = {}, {}
    kinds = {"text": "text", "number": "number", "date": "date", "checkbox": "checkbox", "list": "dropdown"}
    for i, f in enumerate(data.get("customFields") or [], start=1):
        kind = kinds.get(f.get("type"), "text")
        opts = [(o.get("value") or {}).get("text", "") for o in f.get("options") or []]
        for o in f.get("options") or []:
            options[o["id"]] = (o.get("value") or {}).get("text", "")
        fields[f["id"]] = CustomField.objects.create(board=board, name=(f.get("name") or "Field")[:60], kind=kind,
                                                     options=opts if kind == "dropdown" else [], position=i)

    lists = {}
    for i, lst in enumerate(sorted(data.get("lists") or [], key=lambda x: x.get("pos") or 0), start=1):
        lists[lst["id"]] = List.objects.create(board=board, title=(lst.get("name") or "List")[:120],
                                               position=i * positions.GAP, is_archived=bool(lst.get("closed")))

    checklists = {}
    for cl in data.get("checklists") or []:
        checklists.setdefault(cl.get("idCard"), []).append(cl)

    comments = {}
    for a in data.get("actions") or []:
        if a.get("type") == "commentCard":
            cid = ((a.get("data") or {}).get("card") or {}).get("id")
            if cid:
                comments.setdefault(cid, []).append(a)

    count = {"cards": 0, "checklists": 0, "comments": 0, "links": 0, "archived": 0}
    for i, c in enumerate(sorted(data.get("cards") or [], key=lambda x: x.get("pos") or 0), start=1):
        lst = lists.get(c.get("idList"))
        if not lst:
            continue
        due = _dt(c.get("due"))
        start = _dt(c.get("start"))
        card = Card.objects.create(
            list=lst, title=(c.get("name") or "Untitled")[:250], description=c.get("desc") or "",
            position=i * positions.GAP, due_at=due, start_date=start.date() if start else None,
            is_complete=bool(c.get("dueComplete")), is_archived=bool(c.get("closed")), created_by=user,
            reminder_minutes=-1 if not due else 1440)
        card.labels.set([labels[x] for x in c.get("idLabels") or [] if x in labels])
        card.members.set([members[x] for x in c.get("idMembers") or [] if x in members])
        count["cards"] += 1
        count["archived"] += bool(c.get("closed"))
        for k, cl in enumerate(sorted(checklists.get(c["id"], []), key=lambda x: x.get("pos") or 0), start=1):
            ncl = Checklist.objects.create(card=card, title=(cl.get("name") or "Checklist")[:120], position=k * positions.GAP)
            for j, it in enumerate(sorted(cl.get("checkItems") or [], key=lambda x: x.get("pos") or 0), start=1):
                done = it.get("state") == "complete"
                ChecklistItem.objects.create(
                    checklist=ncl, text=(it.get("name") or "")[:250] or "Item", position=j * positions.GAP,
                    is_done=done, done_at=timezone.now() if done else None, due_at=_dt(it.get("due")),
                    assignee=members.get(it.get("idMember")), reminder_minutes=-1)
            count["checklists"] += 1
        for item in c.get("customFieldItems") or []:
            f = fields.get(item.get("idCustomField"))
            if not f:
                continue
            v = item.get("value") or {}
            if f.kind == "dropdown":
                value = options.get(item.get("idValue"))
            elif f.kind == "number":
                try:
                    value = float(v.get("number"))
                    value = int(value) if value.is_integer() else value
                except (TypeError, ValueError):
                    value = None
            elif f.kind == "date":
                d = _dt(v.get("date"))
                value = d.date().isoformat() if d else None
            elif f.kind == "checkbox":
                value = True if v.get("checked") == "true" else None
            else:
                value = (v.get("text") or "")[:500] or None
            if value not in (None, ""):
                CardFieldValue.objects.create(card=card, field=f, value=value)
        for a in c.get("attachments") or []:
            url = a.get("url") or ""
            if url.startswith(("http://", "https://")):
                Attachment.objects.create(card=card, url=url[:1000], name=(a.get("name") or url)[:255], uploaded_by=user)
                count["links"] += 1
        for a in sorted(comments.get(c["id"], []), key=lambda x: x.get("date") or ""):
            who = (a.get("memberCreator") or {}).get("fullName") or names.get(a.get("idMemberCreator"), "Someone")
            when = _dt(a.get("date"))
            author = members.get(a.get("idMemberCreator"))
            prefix = "" if author else f"**{who}** (in Trello{', ' + timezone.localtime(when).strftime('%-d %b %Y') if when else ''}): "
            cm = Comment.objects.create(card=card, author=author or user, body=prefix + ((a.get("data") or {}).get("text") or ""))
            if when:
                Comment.objects.filter(pk=cm.pk).update(created_at=when)
            count["comments"] += 1
    return board, count, sorted(unmatched)
