"""Shared helpers for views: activity logging, query shapes, rendering."""
from django.conf import settings
from django.db.models import Prefetch
from django.template.loader import render_to_string

from . import ai, permissions
from django.contrib.auth import get_user_model

from .models import (COLOURS, STICKERS, Activity, BoardMember, Card, CardFieldValue, CardMirror, Checklist,
                     ChecklistItem, CustomField, Label)

User = get_user_model()


def log(board, actor, verb, card=None, **data):
    return Activity.objects.create(board=board, card=card, actor=actor, verb=verb, data=data)


def cards_qs():
    """Everything a card tile needs, in a fixed number of queries."""
    return Card.objects.select_related("list__board", "cover").prefetch_related(
        "attachments",
        "labels",
        "members",
        Prefetch("checklists", queryset=Checklist.objects.prefetch_related(
            Prefetch("items", queryset=ChecklistItem.objects.select_related("assignee")))),
        Prefetch("field_values", queryset=CardFieldValue.objects.select_related("field")),
        "comments",
    )


def lists_with_entries(board):
    """A board's live lists with cards and mirrored cards prefetched for tiles."""
    return board.lists.filter(is_archived=False).prefetch_related(
        Prefetch("cards", queryset=cards_qs().filter(is_archived=False)),
        Prefetch("mirrors", queryset=CardMirror.objects.prefetch_related(Prefetch("card", queryset=cards_qs()))),
    )


def board_people(board):
    return [m.user for m in BoardMember.objects.filter(board=board).select_related("user").order_by("user__first_name")]


def render_tile(card, request, mirror=None):
    return render_to_string("boards/_card_tile.html", {"card": card, "mirror": mirror}, request=request)


def render_modal(card, request):
    board = card.list.board
    user = request.user
    boards = permissions.card_boards(card)
    people = list(User.objects.filter(board_memberships__board__in=boards, is_active=True).distinct()
                  .order_by("first_name", "last_name"))
    can_edit = permissions.can_edit_card(card, user)
    mirrors = list(card.mirrors.select_related("list__board"))
    from .views_planner import editable_lists
    taken = {card.list_id} | {m.list_id for m in mirrors}
    mirror_targets = [(b, [l for l in ls if l.pk not in taken]) for b, ls in editable_lists(user)] if can_edit else []
    ctx = {
        "card": card,
        "board": board,
        "lists": board.lists.filter(is_archived=False),
        "people": people,
        "can_edit": can_edit,
        "can_move": permissions.can_edit(board, user),
        "can_comment": permissions.can_comment_card(card, user),
        "can_see_source": permissions.can_view(board, user),
        "mirrors": mirrors,
        "mirror_targets": [(b, ls) for b, ls in mirror_targets if ls],
        "feed": _feed(card),
        "label_colours": Label.COLOURS,
        "fields": _field_rows(card, board),
        "attachments_persistent": settings.ATTACHMENTS_PERSISTENT,
        "max_mb": settings.ATTACHMENT_MAX_MB,
        "field_kinds": CustomField.KINDS,
        "colours": COLOURS,
        "ai_enabled": ai.enabled(),
        "sticker_choices": STICKERS,
    }
    return render_to_string("boards/_card_modal.html", ctx, request=request)


def _field_rows(card, board):
    values = {v.field_id: v.value for v in card.field_values.all()}
    return [{"field": f, "value": values.get(f.pk)} for f in board.custom_fields.all()]


def _feed(card):
    """Comments and activity merged newest first, like Trello's right panel."""
    comments = [{"kind": "comment", "at": c.created_at, "obj": c} for c in card.comments.select_related("author")]
    acts = [{"kind": "activity", "at": a.created_at, "obj": a} for a in card.activity.select_related("actor")]
    return sorted(comments + acts, key=lambda x: x["at"], reverse=True)


def card_response(card_id, request):
    card = cards_qs().get(pk=card_id)
    return {"card_id": card.pk, "list_id": card.list_id, "archived": card.is_archived,
            "tile": render_tile(card, request), "modal": render_modal(card, request)}
