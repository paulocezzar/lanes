"""Context shared by every view of a board (Board, Calendar, Table, Timeline,
Dashboard): header, share dialog, menu drawer and the card dialog."""
from django.shortcuts import get_object_or_404

from . import ai, permissions
from .models import Board, BoardMember, CustomField, Label
from .services import board_people, cards_qs

VIEW_TABS = [
    ("board", "Board", "board"),
    ("calendar", "Calendar", "board_calendar"),
    ("table", "Table", "board_table"),
    ("timeline", "Timeline", "board_timeline"),
    ("dashboard", "Dashboard", "board_dashboard"),
    ("map", "Map", "board_map"),
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
        "field_kinds": CustomField.KINDS,
        "archived_cards": cards_qs().filter(list__board=board, is_archived=True)[:50],
        "activity": board.activity.select_related("actor", "card")[:40],
        "open_card": request.GET.get("card"),
        "my_workspaces": _my_workspaces(request.user),
        "ai_enabled": ai.enabled(),
        "watching_board": board.watches.filter(user=request.user, card__isnull=True).exists(),
    }


def _my_workspaces(user):
    from .models import Workspace
    if user.is_superuser:
        return Workspace.objects.all()
    return Workspace.objects.filter(memberships__user=user).distinct()


def open_cards(board):
    """Live cards on live lists — including cards mirrored onto this board."""
    from django.db.models import Q
    return cards_qs().filter(
        Q(list__board=board, list__is_archived=False)
        | Q(mirrors__list__board=board, mirrors__list__is_archived=False),
        is_archived=False,
    ).distinct()


ALL_TABS = [
    ("calendar", "Calendar", "all_calendar"),
    ("table", "Table", "all_table"),
    ("timeline", "Timeline", "all_timeline"),
    ("dashboard", "Dashboard", "all_dashboard"),
    ("map", "Map", "all_map"),
]


def all_context(request, view):
    """Context for a view across every board the user can see."""
    from .models import Collection
    user = request.user
    boards = permissions.visible_boards(user).filter(is_template=False).select_related("workspace")
    ws = request.GET.get("dept")
    col = request.GET.get("collection")
    if ws:
        boards = boards.filter(workspace_id=ws)
    if col:
        boards = boards.filter(collection_id=col)
    return {
        "board": None,
        "view": view,
        "all_tabs": ALL_TABS,
        "base_template": "boards/all_base.html",
        "scope_boards": boards,
        "can_edit": True,          # per-card permissions are checked on every change
        "people": [],
        "labels": [],
        "dept_options": _my_workspaces(user),
        "collection_options": Collection.objects.filter(workspace__in=_my_workspaces(user)).select_related("workspace"),
        "dept": ws or "",
        "collection": col or "",
        "query_suffix": "&".join(f"{k}={v}" for k, v in (("dept", ws), ("collection", col)) if v),
        "ai_enabled": ai.enabled(),
    }
