"""Join requests: someone who can't see a private board (or isn't in a department) asks to be let in,
and the board's or department's admins approve or decline from one page."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import audit, notify, permissions
from .models import Board, BoardMember, JoinRequest, Workspace, WorkspaceMember

User = get_user_model()


class NoAccess(Exception):
    """Raised for a board the person can't see; the middleware shows the "Ask to join" page."""
    def __init__(self, board):
        super().__init__(board.pk)
        self.board = board


class NoAccessMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if isinstance(exception, NoAccess) and request.user.is_authenticated:
            board = exception.board
            pending = JoinRequest.objects.filter(user=request.user, board=board, status="pending").exists()
            return render(request, "boards/no_access.html", {"board_id": board.pk, "pending": pending}, status=403)
        return None


def deciders(jr):
    if jr.board_id:
        people = {m.user for m in BoardMember.objects.filter(board=jr.board, role="admin").select_related("user")}
        people |= {m.user for m in WorkspaceMember.objects.filter(workspace=jr.board.workspace, role="admin")
                   .select_related("user")}
    else:
        people = {m.user for m in WorkspaceMember.objects.filter(workspace=jr.workspace, role="admin")
                  .select_related("user")}
    if not people:
        people = set(User.objects.filter(is_superuser=True, is_active=True))
    return [u for u in people if u.is_active]


def can_decide(jr, user):
    return user.is_superuser or user in deciders(jr)


@login_required
@require_POST
def join_request_create(request):
    user, message = request.user, (request.POST.get("message") or "").strip()[:300]
    if request.POST.get("board"):
        board = get_object_or_404(Board, pk=request.POST["board"], is_archived=False)
        if permissions.can_view(board, user):
            return redirect("board", board_id=board.pk)
        jr, made = JoinRequest.objects.get_or_create(user=user, board=board, status="pending",
                                                     defaults={"message": message})
        what = "this board"
    else:
        ws = get_object_or_404(Workspace, pk=request.POST.get("workspace"))
        if permissions.is_workspace_member(ws, user):
            return redirect("home")
        jr, made = JoinRequest.objects.get_or_create(user=user, workspace=ws, status="pending",
                                                     defaults={"message": message})
        what = ws.name
    if made:
        for u in deciders(jr):
            notify.send(u, "join", actor=user, text=f"asked to join {jr.board.title if jr.board_id else jr.workspace.name}",
                        excerpt=message, dedupe_key=f"join:{jr.pk}")
    messages.success(request, f"Asked to join {what}. You'll be notified when someone answers.")
    return redirect(request.POST.get("next") or "home")


@login_required
def join_requests_page(request):
    mine = JoinRequest.objects.filter(user=request.user).select_related("board", "workspace", "decided_by")[:20]
    pending = [jr for jr in JoinRequest.objects.filter(status="pending").select_related("user", "board__workspace", "workspace")
               if can_decide(jr, request.user)]
    return render(request, "boards/join_requests.html", {"pending": pending, "mine": mine})


@login_required
@require_POST
def join_request_decide(request, pk):
    jr = get_object_or_404(JoinRequest.objects.select_related("user", "board", "workspace"), pk=pk, status="pending")
    if not can_decide(jr, request.user):
        raise Http404
    approve = request.POST.get("action") == "approve"
    where = jr.board.title if jr.board_id else jr.workspace.name
    if approve:
        if jr.board_id:
            BoardMember.objects.get_or_create(board=jr.board, user=jr.user, defaults={"role": BoardMember.MEMBER})
        else:
            WorkspaceMember.objects.get_or_create(workspace=jr.workspace, user=jr.user)
        audit.record(request, "join_approved", board=jr.board, workspace=jr.workspace, target=jr.user, where=where)
    jr.status = JoinRequest.APPROVED if approve else JoinRequest.DECLINED
    jr.decided_by, jr.decided_at = request.user, timezone.now()
    jr.save()
    notify.send(jr.user, "join", actor=request.user, board=jr.board if approve and jr.board_id else None,
                text=f"{'let you into' if approve else 'declined your request to join'} {where}",
                dedupe_key=f"joindone:{jr.pk}")
    messages.success(request, f"{jr.user.display_name} {'can now use' if approve else 'was not added to'} {where}.")
    return redirect("join_requests")
