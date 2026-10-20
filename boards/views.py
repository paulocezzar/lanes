"""Page views: home (all boards by department), board page, card modal, create board."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import permissions, positions
from .models import Board, BoardMember, Label, List, Workspace
from .services import board_people, cards_qs, log, render_modal

DEFAULT_LABELS = ["green", "yellow", "orange", "red", "purple", "blue"]


@login_required
def home(request):
    user = request.user
    boards = permissions.visible_boards(user).select_related("workspace")
    starred = set(BoardMember.objects.filter(user=user, starred=True).values_list("board_id", flat=True))
    if user.is_superuser:
        workspaces = Workspace.objects.all()
    else:
        workspaces = Workspace.objects.filter(memberships__user=user).distinct()
    groups = []
    for ws in workspaces:
        groups.append({"workspace": ws, "boards": [b for b in boards if b.workspace_id == ws.pk]})
    # Boards the user was invited to as a guest from another department
    ws_ids = {ws.pk for ws in workspaces}
    guest = [b for b in boards if b.workspace_id not in ws_ids]
    return render(request, "boards/home.html", {
        "groups": groups,
        "guest_boards": guest,
        "starred": [b for b in boards if b.pk in starred],
        "starred_ids": starred,
        "backgrounds": Board.BACKGROUNDS,
        "create_workspaces": workspaces,
    })


@login_required
@require_POST
def board_create(request):
    ws = get_object_or_404(Workspace, pk=request.POST.get("workspace"))
    if not (request.user.is_superuser or permissions.is_workspace_member(ws, request.user)):
        messages.error(request, "You're not a member of that department.")
        return redirect("home")
    title = (request.POST.get("title") or "").strip()[:120]
    if not title:
        messages.error(request, "Give the board a title.")
        return redirect("home")
    bg = request.POST.get("background")
    board = Board.objects.create(
        workspace=ws, title=title, created_by=request.user,
        background=bg if bg in dict(Board.BACKGROUNDS) else "dusk",
        visibility=request.POST.get("visibility") if request.POST.get("visibility") in dict(Board.VISIBILITY) else Board.WORKSPACE,
    )
    BoardMember.objects.create(board=board, user=request.user, role=BoardMember.ADMIN)
    Label.objects.bulk_create([Label(board=board, colour=c) for c in DEFAULT_LABELS])
    for i, t in enumerate(["To do", "Doing", "Done"], start=1):
        List.objects.create(board=board, title=t, position=i * positions.GAP)
    log(board, request.user, "board_created")
    return redirect("board", board_id=board.pk)


@login_required
def board_page(request, board_id):
    board = get_object_or_404(Board.objects.select_related("workspace"), pk=board_id, is_archived=False)
    permissions.require(permissions.can_view, board, request.user)
    lists = board.lists.filter(is_archived=False).prefetch_related(
        Prefetch("cards", queryset=cards_qs().filter(is_archived=False)))
    me = BoardMember.objects.filter(board=board, user=request.user).first()
    return render(request, "boards/board.html", {
        "board": board,
        "lists": lists,
        "people": board_people(board),
        "labels": board.labels.all(),
        "me": me,
        "can_edit": permissions.can_edit(board, request.user),
        "can_manage": permissions.can_manage(board, request.user),
        "roles": BoardMember.ROLES,
        "label_colours": Label.COLOURS,
        "archived_cards": cards_qs().filter(list__board=board, is_archived=True)[:50],
        "activity": board.activity.select_related("actor", "card")[:40],
        "open_card": request.GET.get("card"),
    })


@login_required
def card_modal(request, card_id):
    card = get_object_or_404(cards_qs(), pk=card_id)
    permissions.require(permissions.can_view, card.list.board, request.user)
    return HttpResponse(render_modal(card, request))


@login_required
def my_cards(request):
    """Every open card assigned to me, across all boards I can see."""
    boards = permissions.visible_boards(request.user)
    cards = (cards_qs().filter(members=request.user, is_archived=False, list__board__in=boards,
                               list__is_archived=False)
             .order_by("list__board__title", "list__board_id", "is_complete", "due_at"))
    return render(request, "boards/my_cards.html", {"cards": cards})


@login_required
def board_state(request, board_id):
    """Polled by board.js every few seconds. Returns just the version unless
    it differs from the caller's, in which case fresh lists HTML comes too."""
    board = get_object_or_404(Board, pk=board_id)
    permissions.require(permissions.can_view, board, request.user)
    data = {"version": board.version, "archived": board.is_archived}
    if request.GET.get("v") != str(board.version):
        lists = board.lists.filter(is_archived=False).prefetch_related(
            Prefetch("cards", queryset=cards_qs().filter(is_archived=False)))
        can_edit = permissions.can_edit(board, request.user)
        data["lists_html"] = "".join(
            render_to_string("boards/_list.html", {"list": lst, "can_edit": can_edit}, request=request)
            for lst in lists)
    return JsonResponse(data)
