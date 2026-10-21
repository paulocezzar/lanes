"""Who can see and change what.

- View: board member, or any member of the department when the board is
  workspace-visible, or a superuser.
- Edit: board Admin/Member (Observers read and comment only), department
  admins, superusers.
- Manage (share, roles, settings): board Admin, department admin, superuser.
"""
from django.db.models import Q
from django.http import Http404

from .models import Board, BoardMember, WorkspaceMember


def board_role(board, user):
    return BoardMember.objects.filter(board=board, user=user).values_list("role", flat=True).first()


def is_workspace_admin(workspace, user):
    return WorkspaceMember.objects.filter(workspace=workspace, user=user, role=WorkspaceMember.ADMIN).exists()


def is_workspace_member(workspace, user):
    return WorkspaceMember.objects.filter(workspace=workspace, user=user).exists()


def can_view(board, user):
    if user.is_superuser or board_role(board, user):
        return True
    return board.visibility == Board.WORKSPACE and is_workspace_member(board.workspace, user)


def can_edit(board, user):
    if user.is_superuser or is_workspace_admin(board.workspace, user):
        return True
    return board_role(board, user) in (BoardMember.ADMIN, BoardMember.MEMBER)


def can_comment(board, user):
    return user.is_superuser or board_role(board, user) is not None or is_workspace_admin(board.workspace, user)


def can_manage(board, user):
    if user.is_superuser or is_workspace_admin(board.workspace, user):
        return True
    return board_role(board, user) == BoardMember.ADMIN


def visible_boards(user):
    qs = Board.objects.filter(is_archived=False)
    if user.is_superuser:
        return qs
    return qs.filter(
        Q(memberships__user=user)
        | Q(visibility=Board.WORKSPACE, workspace__memberships__user=user)
    ).distinct()


def require(check, board, user):
    """404 rather than 403 so private boards don't reveal they exist."""
    if not check(board, user):
        raise Http404


# ---------- card-level checks (a card can be reached through its mirrors) ----------

def card_boards(card):
    """The card's own board first, then every board it's mirrored onto."""
    from .models import Board
    boards = [card.list.board]
    boards += [b for b in Board.objects.filter(lists__mirrors__card=card, is_archived=False).distinct()
               if b.pk != boards[0].pk]
    return boards


def can_view_card(card, user):
    return any(can_view(b, user) for b in card_boards(card))


def can_edit_card(card, user):
    return any(can_edit(b, user) for b in card_boards(card))


def can_comment_card(card, user):
    return any(can_comment(b, user) for b in card_boards(card))


# ---------- company-wide settings (Company → Settings) ----------

def _settings():
    from .models import CompanySettings
    return CompanySettings.get()


def _allowed(level, workspace, user, board=None):
    if user.is_superuser:
        return level != "nobody"
    if level == "nobody" or level == "company_admins":
        return False
    if level == "dept_admins":
        return is_workspace_admin(workspace, user)
    if level == "board_admins":
        return board is not None and can_manage(board, user)
    if level == "everyone":
        return is_workspace_member(workspace, user)
    return False


def can_public_link(board, user):
    return _allowed(_settings().public_links, board.workspace, user, board)


def public_links_off():
    return _settings().public_links == "nobody"


def can_invite_guests(board, user):
    return _allowed(_settings().invite_guests, board.workspace, user, board)


def can_make_private(board, user):
    return _allowed(_settings().private_boards, board.workspace, user, board)


def can_create_board(workspace, user):
    return _allowed(_settings().create_boards, workspace, user)


def who_can(setting):
    """Plain words for an error message, e.g. 'department admins'."""
    return {"board_admins": "board admins", "dept_admins": "department admins", "company_admins": "company admins",
            "everyone": "everyone in the department", "nobody": "nobody"}[getattr(_settings(), setting)]
