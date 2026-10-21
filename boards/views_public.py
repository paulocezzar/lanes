"""Read-only public boards: anyone with the secret link can look, nobody can
change anything. Comments, attachments, members and mirrored cards from other
boards are never shown."""
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, render

from .models import Board, CardMirror
from .services import cards_qs


def _board(token):
    if not token or len(token) < 16:
        raise Http404
    return get_object_or_404(Board.objects.select_related("workspace"), public_token=token, is_archived=False)


def public_board(request, token):
    board = _board(token)
    lists = board.lists.filter(is_archived=False).prefetch_related(
        Prefetch("cards", queryset=cards_qs().filter(is_archived=False, is_template=False)),
        Prefetch("mirrors", queryset=CardMirror.objects.none()),
    )
    return render(request, "boards/public_board.html", {"board": board, "lists": lists, "public": True,
                                                        "can_edit": False, "token": token})


def public_card(request, token, card_id):
    board = _board(token)
    card = get_object_or_404(cards_qs(), pk=card_id, list__board=board, is_archived=False, is_template=False)
    values = {v.field_id: v for v in card.field_values.all()}
    fields = [(f, values[f.pk]) for f in board.custom_fields.all() if f.pk in values]
    return render(request, "boards/public_card.html", {"board": board, "card": card, "fields": fields,
                                                       "public": True, "token": token})
