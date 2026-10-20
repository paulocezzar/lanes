"""Context shared by every view of a board (Board, Calendar, Table, Timeline,
Dashboard): header, share dialog, menu drawer and the card dialog."""
from django.shortcuts import get_object_or_404

from . import permissions
from .models import Board, BoardMember, Label
from .services import board_people, cards_qs

VIEW_TABS = [
    ("board", "Board", "board"),
    ("calendar", "Calendar", "board_calendar"),
    ("table", "Table", "board_table"),
    ("timeline", "Timeline", "board_timeline"),
    ("dashboard", "Dashboard", "board_dashboard"),
]


def board_context(request, board_id, view):
    board = get_object_or_404(Board.objects.select_related("workspace"), pk=board_id, is_archived=False)
    permissions.require(permissions.can_view, board, request.user)
    return board, {
        "board": board,
        "view": view,
        "view_tabs": VIEW_TABS,
        "people": board_people(board),
        "labels": board.labels.all(),
        "me": BoardMember.objects.filter(board=board, user=request.user).first(),
        "can_edit": permissions.can_edit(board, request.user),
        "can_manage": permissions.can_manage(board, request.user),
        "roles": BoardMember.ROLES,
        "label_colours": Label.COLOURS,
        "archived_cards": cards_qs().filter(list__board=board, is_archived=True)[:50],
        "activity": board.activity.select_related("actor", "card")[:40],
        "open_card": request.GET.get("card"),
    }


def open_cards(board):
    """Live cards on live lists, with everything tiles need."""
    return cards_qs().filter(list__board=board, is_archived=False, list__is_archived=False)
