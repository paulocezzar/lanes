"""Shared helpers for views: activity logging, query shapes, rendering."""
from django.db.models import Prefetch
from django.template.loader import render_to_string

from . import permissions
from .models import Activity, BoardMember, Card, Checklist, ChecklistItem, Label


def log(board, actor, verb, card=None, **data):
    return Activity.objects.create(board=board, card=card, actor=actor, verb=verb, data=data)


def cards_qs():
    """Everything a card tile needs, in a fixed number of queries."""
    return Card.objects.select_related("list__board").prefetch_related(
        "labels",
        "members",
        Prefetch("checklists", queryset=Checklist.objects.prefetch_related(
            Prefetch("items", queryset=ChecklistItem.objects.all()))),
        "comments",
    )


def board_people(board):
    return [m.user for m in BoardMember.objects.filter(board=board).select_related("user").order_by("user__first_name")]


def render_tile(card, request):
    return render_to_string("boards/_card_tile.html", {"card": card}, request=request)


def render_modal(card, request):
    board = card.list.board
    user = request.user
    ctx = {
        "card": card,
        "board": board,
        "lists": board.lists.filter(is_archived=False),
        "people": board_people(board),
        "can_edit": permissions.can_edit(board, user),
        "can_comment": permissions.can_comment(board, user),
        "feed": _feed(card),
        "label_colours": Label.COLOURS,
    }
    return render_to_string("boards/_card_modal.html", ctx, request=request)


def _feed(card):
    """Comments and activity merged newest first, like Trello's right panel."""
    comments = [{"kind": "comment", "at": c.created_at, "obj": c} for c in card.comments.select_related("author")]
    acts = [{"kind": "activity", "at": a.created_at, "obj": a} for a in card.activity.select_related("actor")]
    return sorted(comments + acts, key=lambda x: x["at"], reverse=True)


def card_response(card_id, request):
    card = cards_qs().get(pk=card_id)
    return {"card_id": card.pk, "list_id": card.list_id, "archived": card.is_archived,
            "tile": render_tile(card, request), "modal": render_modal(card, request)}
