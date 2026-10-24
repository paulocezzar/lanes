"""Shared helpers for views: activity logging, query shapes, rendering."""
from django.conf import settings
from django.utils import timezone
from django.db.models import Prefetch
from django.template.loader import render_to_string

from . import ai, permissions
from django.contrib.auth import get_user_model

from .models import (COLOURS, REACTIONS, STICKERS, Activity, BoardMember, Card, CardFieldValue, CardMirror, Checklist,
                     ChecklistItem, CustomField, Dependency, Label, List, SignOff)

User = get_user_model()


def log(board, actor, verb, card=None, **data):
    activity = Activity.objects.create(board=board, card=card, actor=actor, verb=verb, data=data)
    from . import notify
    notify.on_activity(activity)
    return activity


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
        "votes",
        "children",
        Prefetch("signoffs", queryset=SignOff.objects.select_related("approver")),
        Prefetch("dependencies", queryset=Dependency.objects.select_related("depends_on")),
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


def _move_tree(card, board, user):
    """Boards → lists (with how many cards each holds) for the Move card picker; this board first."""
    import json

    from django.db.models import Count, Q
    from .views_planner import editable_lists
    groups = editable_lists(user)
    groups.sort(key=lambda g: (g[0].pk != board.pk, g[0].title.lower()))
    counts = dict(List.objects.filter(pk__in=[l.pk for _, ls in groups for l in ls])
                  .annotate(n=Count("cards", filter=Q(cards__is_archived=False))).values_list("pk", "n"))
    order = list(card.list.cards.filter(is_archived=False).order_by("position", "pk").values_list("pk", flat=True))
    return json.dumps({
        "board": board.pk, "list": card.list_id,
        "position": order.index(card.pk) + 1 if card.pk in order else 1,
        "boards": [{"id": b.pk, "title": b.title,
                    "lists": [{"id": l.pk, "title": l.title, "n": counts.get(l.pk, 0)} for l in ls]}
                   for b, ls in groups],
    }).replace("</", "<\\/")


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
        "feed": _feed(card, user),
        "label_colours": Label.COLOURS,
        "fields": _field_rows(card, board),
        "fields_used": any(r["value"] not in (None, "", False) for r in _field_rows(card, board)),
        "attachments_persistent": settings.ATTACHMENTS_PERSISTENT,
        "max_mb": settings.ATTACHMENT_MAX_MB,
        "field_kinds": CustomField.KINDS,
        "colours": COLOURS,
        "ai_enabled": ai.enabled(),
        "sticker_choices": STICKERS,
        "card_buttons": [b for b in board.buttons.all() if b.kind == "card" and b.is_enabled],
        "reaction_choices": REACTIONS,
        "voted": any(v.user_id == user.pk for v in card.votes.all()),
        "voters": [v.user.display_name for v in card.votes.select_related("user")],
        "watching": card.watches.filter(user=user).exists(),
        "watching_board": board.watches.filter(user=user, card__isnull=True).exists(),
        "move_tree": _move_tree(card, board, user) if permissions.can_edit(board, user) else None,
        "recent_board_links": [{"url": f"{settings.SITE_URL}/b/{b.pk}/", "title": b.title} for b in
                               permissions.visible_boards(user).filter(pk__in=(user.recent_boards or [])[:6])],
        **_time_ctx(card, user),
        "waits_for": card.dependencies.select_related("depends_on").filter(depends_on__is_archived=False),
        "needed_by": card.dependants.select_related("card").filter(card__is_archived=False),
        "dep_choices": (Card.objects.filter(list__board=board, is_archived=False, is_template=False, list__is_archived=False)
                        .exclude(pk=card.pk).order_by("list__position", "position").select_related("list")[:300]
                        if can_edit else []),
        **_parts_ctx(card, user, can_edit),
        "copyable_checklists": (Checklist.objects.filter(card__list__board=board, card__is_archived=False)
                                .exclude(card=card).select_related("card").order_by("card__title", "title")[:60]
                                if can_edit else []),
    }
    return render_to_string("boards/_card_modal.html", ctx, request=request)


def _parts_ctx(card, user, can_edit):
    """Sign-offs, the card this one is part of, and its own parts."""
    from .api_more import approver_choices
    from .views_planner import editable_lists
    signoffs = list(card.signoffs.select_related("approver", "requested_by"))
    children = list(card.children.filter(is_archived=False).select_related("list__board")
                    .order_by("is_complete", "list__board__title", "position"))
    parent = Card.objects.select_related("list__board").filter(pk=card.parent_id).first() if card.parent_id else None
    return {
        "signoffs": signoffs,
        "my_signoff": next((s for s in signoffs if s.approver_id == user.pk and s.status == "pending"), None),
        "signoff_block": any(s.status != "approved" for s in signoffs),
        "approvers": approver_choices(card) if can_edit else [],
        "children": children,
        "child_progress": card.child_progress() if children else None,
        "parent_card": parent,
        "part_lists": editable_lists(user) if can_edit else [],
    }


def _time_ctx(card, user):
    from .models import TimeEntry
    entries = list(TimeEntry.objects.filter(card=card).select_related("user")[:200])
    total = sum(e.minutes for e in entries)
    mine = next((e for e in entries if e.user_id == user.pk and e.started_at), None)
    est = float(card.estimate_hours or 0) * 60
    return {"time_entries": [e for e in entries if e.minutes][:20], "time_total": total, "my_timer": mine,
            "time_more": max(0, len([e for e in entries if e.minutes]) - 20),
            "time_pct": round(100 * total / est) if est else None, "time_today": timezone.localdate()}


def _field_rows(card, board):
    values = {v.field_id: v.value for v in card.field_values.all()}
    return [{"field": f, "value": values.get(f.pk)} for f in board.custom_fields.all()]


def _feed(card, user):
    """Comments and activity merged newest first, like Trello's right panel."""
    comments = [{"kind": "comment", "at": c.created_at, "obj": c, "reactions": c.reaction_groups(user)}
                for c in card.comments.select_related("author").prefetch_related("reactions__user")]
    acts = [{"kind": "activity", "at": a.created_at, "obj": a} for a in card.activity.select_related("actor")]
    return sorted(comments + acts, key=lambda x: x["at"], reverse=True)


def card_response(card_id, request):
    card = cards_qs().get(pk=card_id)
    return {"card_id": card.pk, "list_id": card.list_id, "archived": card.is_archived,
            "tile": render_tile(card, request), "modal": render_modal(card, request)}
