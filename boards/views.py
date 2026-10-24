"""Page views: home (all boards by department), board page, card modal, create board."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import automation, permissions, positions
from .models import Board, BoardMember, ChecklistItem, JoinRequest, Label, List, Workspace
from .services import board_people, cards_qs, lists_with_entries, log, render_modal
from .board_context import board_context

DEFAULT_LABELS = ["green", "yellow", "orange", "red", "purple", "blue"]


@login_required
def home(request):
    automation.maybe_run_schedules()
    user = request.user
    boards = permissions.visible_boards(user).select_related("workspace", "collection").order_by(
        "collection__name", "title")
    starred = set(BoardMember.objects.filter(user=user, starred=True).values_list("board_id", flat=True))
    if user.is_superuser:
        workspaces = Workspace.objects.all()
    else:
        workspaces = Workspace.objects.filter(memberships__user=user).distinct()
    groups = []
    templates = [b for b in boards if b.is_template]
    boards = [b for b in boards if not b.is_template]
    for ws in workspaces:
        ws_boards = [b for b in boards if b.workspace_id == ws.pk]
        loose = [b for b in ws_boards if not b.collection_id]
        cols = {}
        for b in ws_boards:
            if b.collection_id:
                cols.setdefault(b.collection, []).append(b)
        groups.append({"workspace": ws, "boards": loose, "can_create": permissions.can_create_board(ws, user),
                       "collections": [{"collection": c, "boards": bs} for c, bs in cols.items()]})
    # Boards the user was invited to as a guest from another department
    ws_ids = {ws.pk for ws in workspaces}
    guest = [b for b in boards if b.workspace_id not in ws_ids]
    archived = Board.objects.filter(is_archived=True).select_related("workspace").order_by("title")
    if not user.is_superuser:
        archived = [b for b in archived.filter(Q(memberships__user=user, memberships__role=BoardMember.ADMIN)
                                               | Q(workspace__memberships__user=user,
                                                   workspace__memberships__role="admin")).distinct()]
    return render(request, "boards/home.html", {
        "archived_boards": archived,
        "groups": groups,
        "guest_boards": guest,
        "starred": [b for b in boards if b.pk in starred],
        "starred_ids": starred,
        "backgrounds": Board.BACKGROUNDS,
        "create_workspaces": [ws for ws in workspaces if permissions.can_create_board(ws, user)],
        "templates": templates,
        "other_departments": [] if user.is_superuser or user.is_guest else
        Workspace.objects.exclude(pk__in=[w.pk for w in workspaces]).order_by("name"),
        "asked_departments": set(JoinRequest.objects.filter(user=user, status="pending", workspace__isnull=False)
                                 .values_list("workspace_id", flat=True)),
    })


@login_required
@require_POST
def board_create(request):
    ws = get_object_or_404(Workspace, pk=request.POST.get("workspace"))
    if not (request.user.is_superuser or permissions.is_workspace_member(ws, request.user)):
        messages.error(request, "You're not a member of that department.")
        return redirect("home")
    if not permissions.can_create_board(ws, request.user):
        messages.error(request, f"Only {permissions.who_can('create_boards')} can create boards in {ws.name}.")
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
    if board.visibility == Board.PRIVATE and not permissions.can_make_private(board, request.user):
        board.visibility = Board.WORKSPACE      # creator is board admin; company rule may still say no
        board.save(update_fields=["visibility"])
    BoardMember.objects.create(board=board, user=request.user, role=BoardMember.ADMIN)
    Label.objects.bulk_create([Label(board=board, colour=c) for c in DEFAULT_LABELS])
    for i, t in enumerate(["To do", "Doing", "Done"], start=1):
        List.objects.create(board=board, title=t, position=i * positions.GAP)
    log(board, request.user, "board_created")
    from . import audit, shared_rules
    audit.record(request, "board_created", board=board)
    shared_rules.apply_to_new_board(board)      # the department's shared rules (where its lists match)
    return redirect("board", board_id=board.pk)


@login_required
def board_page(request, board_id):
    board, ctx = board_context(request, board_id, "board")
    ctx["lists"] = lists_with_entries(board)
    return render(request, "boards/board.html", ctx)


@login_required
def card_modal(request, card_id):
    card = get_object_or_404(cards_qs(), pk=card_id)
    if not permissions.can_view_card(card, request.user):
        raise Http404
    return HttpResponse(render_modal(card, request))


@login_required
def my_cards(request):
    """Every open card assigned to me, across all boards I can see."""
    boards = permissions.visible_boards(request.user)
    cards = (cards_qs().filter(members=request.user, is_archived=False, list__board__in=boards,
                               list__is_archived=False)
             .order_by("list__board__title", "list__board_id", "is_complete", "due_at"))
    items = (ChecklistItem.objects.filter(assignee=request.user, is_done=False,
                                          checklist__card__is_archived=False,
                                          checklist__card__list__is_archived=False,
                                          checklist__card__list__board__in=boards)
             .select_related("checklist__card__list__board").order_by("due_at", "id"))
    from . import ai
    return render(request, "boards/my_cards.html", {"ai_enabled": ai.enabled(), "cards": cards, "items": items})


@login_required
def board_state(request, board_id):
    """Polled by board.js every few seconds. Returns just the version unless
    it differs from the caller's, in which case fresh lists HTML comes too."""
    automation.maybe_run_schedules()
    board = get_object_or_404(Board, pk=board_id)
    permissions.require(permissions.can_view, board, request.user)
    data = {"version": board.version, "archived": board.is_archived}
    if request.GET.get("v") != str(board.version) and not request.GET.get("lite"):
        lists = lists_with_entries(board)
        can_edit = permissions.can_edit(board, request.user)
        data["lists_html"] = "".join(
            render_to_string("boards/_list.html", {"list": lst, "can_edit": can_edit}, request=request)
            for lst in lists)
    return JsonResponse(data)


@login_required
@require_POST
def board_restore(request, board_id):
    board = get_object_or_404(Board, pk=board_id, is_archived=True)
    if not permissions.can_manage(board, request.user):
        raise Http404
    if not permissions.can_archive_board(board, request.user):
        messages.error(request, f"Only {permissions.who_can('archive_boards')} can reopen closed boards.")
        return redirect("home")
    board.is_archived = False
    board.save(update_fields=["is_archived"])
    from . import audit
    audit.record(request, "board_restored", board=board)
    messages.success(request, f"{board.title} is back.")
    return redirect("board", board_id=board.pk)


@csrf_exempt
def run_automations_hook(request):
    """Called every 5 minutes by the Render cron job (with CRON_TOKEN), so
    recurring cards, reminders, due-date rules and emails run on time and only
    the web service needs the email/Microsoft settings."""
    import hmac
    import time

    from django.conf import settings
    from django.http import HttpResponseNotAllowed

    from . import mailer
    from .automation import run_due_rules, run_due_schedules
    from .notify import send_due_reminders
    if not settings.CRON_TOKEN:
        raise Http404
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, settings.CRON_TOKEN):
        raise Http404
    started = time.monotonic()
    result = {"recurring": run_due_schedules(), "rules": run_due_rules(), "reminders": send_due_reminders()}
    from .risk import warn
    result["at_risk"] = warn()
    result["notification_emails"] = mailer.notification_emails()
    result["digests"] = mailer.daily_digests()
    result["status_reports"] = mailer.weekly_status_reports()
    result["sent"] = mailer.flush(limit=80, budget=40 - (time.monotonic() - started))
    return JsonResponse(result)


@login_required
def parse_date(request):
    """Preview for typing a date into a card title."""
    from django.utils import timezone

    from .nl_dates import parse
    due, words = parse(request.GET.get("q", "")[:250])
    if not due:
        return JsonResponse({"due": None})
    local = timezone.localtime(due)
    return JsonResponse({"due": local.isoformat(), "words": words,
                         "label": local.strftime("%a %-d %b") + ("" if local.strftime("%H:%M") == "17:00" else local.strftime(", %H:%M"))})


@login_required
@require_POST
def board_import(request):
    """Create a board from a Trello JSON export."""
    import json as _json
    from . import audit, shared_rules, trello_import
    ws = get_object_or_404(Workspace, pk=request.POST.get("workspace"))
    if not permissions.can_create_board(ws, request.user):
        messages.error(request, "You can only create boards in your own departments.")
        return redirect("home")
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Choose the .json file you exported from Trello.")
        return redirect("home")
    if f.size > trello_import.MAX_BYTES:
        messages.error(request, "That file is too big (over 30 MB).")
        return redirect("home")
    try:
        data = _json.loads(f.read().decode("utf-8-sig"))
        visibility = request.POST.get("visibility") if request.POST.get("visibility") in dict(Board.VISIBILITY) else Board.WORKSPACE
        board, n, unmatched = trello_import.import_board(data, workspace=ws, user=request.user,
                                                         title=(request.POST.get("title") or "").strip() or None,
                                                         visibility=visibility)
    except (ValueError, UnicodeDecodeError) as e:
        messages.error(request, str(e) if isinstance(e, trello_import.ImportError_) else
                       "That file couldn't be read. Upload the .json file from Trello's “Export as JSON”.")
        return redirect("home")
    if board.visibility == Board.PRIVATE and not permissions.can_make_private(board, request.user):
        board.visibility = Board.WORKSPACE
        board.save(update_fields=["visibility"])
    log(board, request.user, "board_created")
    audit.record(request, "board_imported", board=board)
    shared_rules.apply_to_new_board(board)
    msg = (f"Imported “{board.title}”: {n['cards']} cards, {n['checklists']} checklists, {n['comments']} comments"
           f"{', ' + str(n['links']) + ' links' if n['links'] else ''}.")
    if n["archived"]:
        msg += f" {n['archived']} archived card{'s' if n['archived'] != 1 else ''} are under Menu → Archived."
    if unmatched:
        msg += (" Not found in " + ws.name + ": " + ", ".join(unmatched[:8]) + (" and others" if len(unmatched) > 8 else "")
                + " — add them to the department and share the board with them.")
    messages.success(request, msg)
    return redirect("board", board_id=board.pk)
