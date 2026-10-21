"""Write AuditEvents. Never lets a logging problem break the real action."""
import logging

from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

log = logging.getLogger(__name__)


def _ip(request):
    if request is None:
        return None
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR")
    return ip or None


def record(request, verb, *, actor=None, workspace=None, board=None, target=None, **data):
    from .models import AuditEvent
    try:
        if actor is None and request is not None and getattr(request, "user", None) and request.user.is_authenticated:
            actor = request.user
        if workspace is None and board is not None:
            workspace = board.workspace
        if board is not None:
            data.setdefault("board", board.title)
        if workspace is not None:
            data.setdefault("dept", workspace.name)
        if target is not None:
            data.setdefault("name", target.display_name)
        AuditEvent.objects.create(actor=actor, verb=verb, workspace=workspace, board=board, target=target,
                                  data=data, ip=_ip(request))
    except Exception:
        log.exception("Couldn't write audit event %s", verb)


@receiver(user_logged_in)
def _logged_in(sender, request, user, **kwargs):
    record(request, "login", actor=user)


@receiver(user_login_failed)
def _login_failed(sender, credentials, request=None, **kwargs):
    record(request, "login_failed", actor=None, email=(credentials or {}).get("username", "")[:120])
