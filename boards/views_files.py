"""Attachments (upload and permission-checked download) and board copying."""
import logging
import mimetypes

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from . import permissions
from .copying import copy_board
from .models import Attachment, Board, Card, Workspace
from .services import card_response, log

log_error = logging.getLogger(__name__)
BLOCKED_EXT = {"exe", "bat", "cmd", "com", "js", "msi", "scr", "ps1", "vbs", "sh", "html", "htm", "svg"}


@login_required
@require_POST
def attachment_upload(request, card_id):
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=card_id)
    board = card.list.board
    if not permissions.can_view_card(card, request.user):
        raise Http404
    if not permissions.can_edit_card(card, request.user):
        return JsonResponse({"error": "You can view this board but not change it."}, status=403)
    f = request.FILES.get("file")
    if not f:
        return JsonResponse({"error": "Choose a file to attach."}, status=400)
    if f.size > settings.ATTACHMENT_MAX_MB * 1024 * 1024:
        return JsonResponse({"error": f"Files can be up to {settings.ATTACHMENT_MAX_MB} MB."}, status=400)
    ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
    if ext in BLOCKED_EXT:
        return JsonResponse({"error": "That type of file can't be attached."}, status=400)
    ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
    att = Attachment(card=card, name=f.name[:255], size=f.size, content_type=ctype, uploaded_by=request.user)
    try:
        att.file.save(f.name, f, save=False)
    except Exception:
        log_error.exception("Attachment upload to storage failed (card %s)", card.pk)
        return JsonResponse({"error": "Couldn't save the file to storage. An admin should check the "
                                      "file-storage settings."}, status=502)
    att.save()
    if att.is_image and not card.cover_id:
        card.cover = att
        card.save(update_fields=["cover", "updated_at"])
    log(board, request.user, "attachment_added", card=card, name=att.name)
    from . import automation
    from .models import Rule
    automation.fire(card, Rule.ATTACHMENT, actor=request.user)
    from .signals import bump
    bump(board.pk)
    return JsonResponse(card_response(card.pk, request))


@login_required
def attachment_download(request, attachment_id):
    att = get_object_or_404(Attachment.objects.select_related("card__list__board"), pk=attachment_id)
    if not permissions.can_view_card(att.card, request.user):
        raise Http404
    if att.url:                                           # a link attachment
        return HttpResponseRedirect(att.url)
    inline = (att.is_image or att.content_type == "application/pdf") and request.GET.get("download") != "1"
    if settings.ATTACHMENTS_PERSISTENT and "s3" in settings.STORAGES["default"]["BACKEND"]:
        if not att.file.storage.exists(att.file.name):
            raise Http404("This file is no longer stored.")
        return HttpResponseRedirect(att.file.url)          # short-lived signed link
    try:
        fh = att.file.open("rb")
    except FileNotFoundError:
        raise Http404("This file is no longer stored on the server.")
    resp = FileResponse(fh, as_attachment=not inline, filename=att.name, content_type=att.content_type)
    resp["X-Content-Type-Options"] = "nosniff"
    return resp


@login_required
@require_POST
def board_copy(request, board_id):
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    if not permissions.can_view(board, request.user):
        raise Http404
    ws = get_object_or_404(Workspace, pk=request.POST.get("workspace") or board.workspace_id)
    if not (request.user.is_superuser or permissions.is_workspace_member(ws, request.user)):
        messages.error(request, "You can only create boards in your own departments.")
        return redirect("board", board_id=board.pk)
    title = (request.POST.get("title") or "").strip() or f"{board.title} (copy)"
    new = copy_board(board, workspace=ws, title=title, user=request.user,
                     keep_cards=request.POST.get("keep_cards") == "on")
    messages.success(request, f"Created {new.title}.")
    return redirect("board", board_id=new.pk)
