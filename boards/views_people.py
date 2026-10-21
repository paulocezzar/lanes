"""Department (workspace) management from the front end, so managers never
need the Django admin for day-to-day setup.

- Company admins (superusers) create departments.
- Department admins (and company admins) add, promote and remove people.
- Adding an email with no account creates one with a temporary password,
  shown once on screen so the manager can pass it on.
"""
import secrets

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import audit, permissions
from .models import Workspace, WorkspaceMember

User = get_user_model()

# Unambiguous characters only: no 0/O, 1/l/I — people read these aloud.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def temporary_password():
    return "-".join("".join(secrets.choice(_ALPHABET) for _ in range(4)) for _ in range(3))


def _can_manage_workspace(ws, user):
    return user.is_superuser or permissions.is_workspace_admin(ws, user)


@login_required
@require_POST
def workspace_create(request):
    if not request.user.is_superuser:
        raise Http404
    name = (request.POST.get("name") or "").strip()[:80]
    if not name:
        messages.error(request, "Give the department a name.")
        return redirect("home")
    if Workspace.objects.filter(name__iexact=name).exists():
        messages.error(request, f"There's already a department called {name}.")
        return redirect("home")
    ws = Workspace.objects.create(name=name)
    WorkspaceMember.objects.create(workspace=ws, user=request.user, role=WorkspaceMember.ADMIN)
    audit.record(request, "dept_created", workspace=ws)
    messages.success(request, f"{name} created. Add its people below.")
    return redirect("workspace_members", workspace_id=ws.pk)


@login_required
def workspace_members(request, workspace_id):
    ws = get_object_or_404(Workspace, pk=workspace_id)
    if not (request.user.is_superuser or permissions.is_workspace_member(ws, request.user)):
        raise Http404
    can_manage = _can_manage_workspace(ws, request.user)
    new_account = request.session.pop("new_account", None)
    return render(request, "boards/workspace_members.html", {
        "workspace": ws,
        "memberships": ws.memberships.select_related("user").order_by("user__first_name", "user__email"),
        "can_manage": can_manage,
        "roles": WorkspaceMember.ROLES,
        "new_account": new_account if new_account and new_account.get("workspace") == ws.pk else None,
    })


@login_required
@require_POST
def workspace_member_add(request, workspace_id):
    ws = get_object_or_404(Workspace, pk=workspace_id)
    if not _can_manage_workspace(ws, request.user):
        raise Http404
    email = (request.POST.get("email") or "").strip().lower()
    first = (request.POST.get("first_name") or "").strip()[:150]
    last = (request.POST.get("last_name") or "").strip()[:150]
    role = request.POST.get("role") if request.POST.get("role") in dict(WorkspaceMember.ROLES) else WorkspaceMember.MEMBER
    back = redirect("workspace_members", workspace_id=ws.pk)
    if "@" not in email:
        messages.error(request, "Enter a valid email address.")
        return back

    person = User.objects.filter(email__iexact=email).first()
    if person is None:
        if not first:
            messages.error(request, f"No account for {email} yet — add their first name and it will be created.")
            return back
        password = temporary_password()
        person = User.objects.create_user(email=email, password=password, first_name=first, last_name=last)
        request.session["new_account"] = {"workspace": ws.pk, "name": person.display_name,
                                          "email": email, "password": password}
        audit.record(request, "dept_account_created", workspace=ws, target=person)
    elif not person.is_active:
        messages.error(request, f"{person.display_name}'s account is switched off. Ask a company admin to reactivate it.")
        return back

    _, created = WorkspaceMember.objects.get_or_create(workspace=ws, user=person, defaults={"role": role})
    if person.is_guest:            # joining a department makes them a normal member of staff
        person.is_guest = False
        person.save(update_fields=["is_guest"])
    if created:
        audit.record(request, "dept_member_added", workspace=ws, target=person, role=role)
        messages.success(request, f"{person.display_name} added to {ws.name}.")
    else:
        messages.info(request, f"{person.display_name} is already in {ws.name}.")
    return back


@login_required
@require_POST
def workspace_member_update(request, workspace_id, user_id):
    ws = get_object_or_404(Workspace, pk=workspace_id)
    membership = get_object_or_404(WorkspaceMember, workspace=ws, user_id=user_id)
    leaving = user_id == request.user.pk and request.POST.get("action") == "remove"
    if not (leaving or _can_manage_workspace(ws, request.user)):
        raise Http404
    back = redirect("workspace_members", workspace_id=ws.pk)
    is_last_admin = (membership.role == WorkspaceMember.ADMIN
                     and ws.memberships.filter(role=WorkspaceMember.ADMIN).count() == 1)
    if request.POST.get("action") == "remove":
        if is_last_admin and not request.user.is_superuser:
            messages.error(request, "A department needs at least one admin. Make someone else admin first.")
            return back
        membership.delete()
        audit.record(request, "dept_member_left" if leaving else "dept_member_removed", workspace=ws,
                     target=membership.user)
        messages.success(request, f"{membership.user.display_name} removed from {ws.name}.")
        return redirect("home") if leaving else back
    role = request.POST.get("role")
    if role in dict(WorkspaceMember.ROLES):
        if is_last_admin and role != WorkspaceMember.ADMIN and not request.user.is_superuser:
            messages.error(request, "A department needs at least one admin.")
            return back
        membership.role = role
        membership.save(update_fields=["role"])
        audit.record(request, "dept_role_changed", workspace=ws, target=membership.user,
                     role=membership.get_role_display().lower())
        messages.success(request, f"{membership.user.display_name} is now {membership.get_role_display().lower()}.")
    return back


@login_required
@require_POST
def workspace_member_reset_password(request, workspace_id, user_id):
    """Department admins can issue a new temporary password to someone in their
    department who's forgotten theirs. Company admins can't be reset this way."""
    ws = get_object_or_404(Workspace, pk=workspace_id)
    if not _can_manage_workspace(ws, request.user):
        raise Http404
    person = get_object_or_404(WorkspaceMember, workspace=ws, user_id=user_id).user
    if person.is_superuser and not request.user.is_superuser:
        messages.error(request, "Company admins reset their own passwords.")
        return redirect("workspace_members", workspace_id=ws.pk)
    password = temporary_password()
    person.set_password(password)
    person.save(update_fields=["password"])
    audit.record(request, "password_reset", workspace=ws, target=person)
    request.session["new_account"] = {"workspace": ws.pk, "name": person.display_name, "email": person.email,
                                      "password": password, "reset": True}
    return redirect("workspace_members", workspace_id=ws.pk)
