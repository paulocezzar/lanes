"""Export a board to CSV (opens in Excel) or JSON, or everything you can see to JSON."""
import csv
import json

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.text import slugify

from . import permissions
from .models import Board
from .services import cards_qs

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe(value):
    """Stop spreadsheet apps treating text as a formula (CSV injection)."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(FORMULA_START) else s


def _dt(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else ""


def _board_cards(board):
    return cards_qs().filter(list__board=board, list__is_archived=False).order_by("list__position", "position")


def _card_dict(c, request):
    return {
        "id": c.pk,
        "title": c.title,
        "list": c.list.title,
        "description": c.description,
        "labels": [{"name": l.name, "colour": l.colour} for l in c.labels.all()],
        "members": [{"name": u.display_name, "email": u.email} for u in c.members.all()],
        "start_date": c.start_date.isoformat() if c.start_date else None,
        "due_at": c.due_at.isoformat() if c.due_at else None,
        "complete": c.is_complete,
        "archived": c.is_archived,
        "checklists": [{"title": cl.title, "items": [
            {"text": i.text, "done": i.is_done, "assignee": i.assignee.display_name if i.assignee else None,
             "due_at": i.due_at.isoformat() if i.due_at else None} for i in cl.items.all()]}
            for cl in c.checklists.all()],
        "custom_fields": {v.field.name: v.value for v in c.field_values.all()},
        "comments": [{"author": cm.author.display_name if cm.author else "Automation", "body": cm.body,
                      "at": cm.created_at.isoformat()} for cm in c.comments.all()],
        "attachments": [a.name for a in c.attachments.all()],
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
        "url": request.build_absolute_uri(f"/b/{c.list.board_id}/?card={c.pk}"),
    }


def _board_dict(board, request):
    return {
        "id": board.pk,
        "title": board.title,
        "department": board.workspace.name,
        "collection": board.collection.name if board.collection_id else None,
        "lists": [l.title for l in board.lists.filter(is_archived=False)],
        "cards": [_card_dict(c, request) for c in _board_cards(board)],
    }


@login_required
def board_csv(request, board_id):
    board = get_object_or_404(Board.objects.select_related("workspace"), pk=board_id)
    permissions.require(permissions.can_view, board, request.user)
    fields = list(board.custom_fields.all())
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{slugify(board.title) or "board"}-{timezone.localdate()}.csv"'
    resp.write("﻿")  # BOM so Excel reads UTF-8 (£, é, emoji) correctly
    w = csv.writer(resp)
    w.writerow(["Board", "List", "Card", "Description", "Labels", "Members", "Start", "Due", "Complete", "Archived",
                "Checklist done", "Checklist total", "Checklist items", *[f.name for f in fields], "Comments",
                "Attachments", "Created", "Updated", "Link"])
    for c in _board_cards(board):
        items = [i for cl in c.checklists.all() for i in cl.items.all()]
        values = {v.field_id: v for v in c.field_values.all()}
        w.writerow([_safe(x) for x in [
            board.title, c.list.title, c.title, c.description,
            ", ".join(str(l) for l in c.labels.all()),
            ", ".join(u.display_name for u in c.members.all()),
            c.start_date.isoformat() if c.start_date else "", _dt(c.due_at),
            "Yes" if c.is_complete else "No", "Yes" if c.is_archived else "No",
            sum(1 for i in items if i.is_done), len(items),
            "; ".join(("✓ " if i.is_done else "☐ ") + i.text for i in items),
            *[(values[f.pk].value if f.pk in values else "") for f in fields],
            len(c.comments.all()), len(c.attachments.all()),
            _dt(c.created_at), _dt(c.updated_at),
            request.build_absolute_uri(f"/b/{board.pk}/?card={c.pk}"),
        ]])
    return resp


@login_required
def board_json(request, board_id):
    board = get_object_or_404(Board.objects.select_related("workspace", "collection"), pk=board_id)
    permissions.require(permissions.can_view, board, request.user)
    resp = JsonResponse(_board_dict(board, request), json_dumps_params={"indent": 2, "ensure_ascii": False})
    resp["Content-Disposition"] = f'attachment; filename="{slugify(board.title) or "board"}-{timezone.localdate()}.json"'
    return resp


@login_required
def export_all(request):
    boards = permissions.visible_boards(request.user).select_related("workspace", "collection")
    data = {"exported_at": timezone.now().isoformat(), "exported_by": request.user.email,
            "boards": [_board_dict(b, request) for b in boards]}
    resp = HttpResponse(json.dumps(data, indent=2, ensure_ascii=False), content_type="application/json")
    resp["Content-Disposition"] = f'attachment; filename="lanes-export-{timezone.localdate()}.json"'
    return resp
