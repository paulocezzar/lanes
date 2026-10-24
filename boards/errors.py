"""Error alerts: every server error is grouped by where it happened (exception type + the line in our code)
and Lanes admins get one notification per problem — again only if it keeps happening hours later.
They show on Company → Problems, and by email when email is set up."""
import logging
import threading
import traceback
from datetime import timedelta

_guard = threading.local()
REMIND_AFTER = timedelta(hours=6)


def _signature(record):
    exc = record.exc_info[1] if record.exc_info else None
    where = ""
    if exc is not None:
        for fr in traceback.extract_tb(exc.__traceback__):
            if "/boards/" in fr.filename or "/accounts/" in fr.filename or "/config/" in fr.filename:
                where = f"{fr.filename.rsplit('/', 2)[-2]}/{fr.filename.rsplit('/', 1)[-1]}:{fr.lineno}"
        return f"{type(exc).__name__} {where}"[:200], f"{type(exc).__name__}: {exc}"[:500]
    msg = record.getMessage()
    return f"{record.name} {msg[:120]}"[:200], msg[:500]


class AdminAlertHandler(logging.Handler):
    def emit(self, record):
        if getattr(_guard, "busy", False):
            return
        _guard.busy = True
        try:
            from django.db import transaction
            with transaction.atomic():      # a savepoint, so a failure here can't spoil the caller's transaction
                record_error(record)
        except Exception:
            pass                       # never let error reporting cause another error
        finally:
            _guard.busy = False


def record_error(record):
    from django.db import transaction
    from django.utils import timezone

    from .models import ErrorReport
    sig, message = _signature(record)
    req = getattr(record, "request", None)
    path = getattr(req, "path", "")[:300] if req is not None else ""
    detail = "".join(traceback.format_exception(*record.exc_info))[-8000:] if record.exc_info else message
    now = timezone.now()
    with transaction.atomic():
        rep, made = ErrorReport.objects.select_for_update().get_or_create(
            signature=sig, defaults={"message": message, "path": path, "detail": detail})
        if not made:
            rep.count += 1
            rep.last_seen, rep.message, rep.detail = now, message, detail
            rep.path = path or rep.path
            if rep.resolved_at:
                rep.resolved_at, rep.notified_at = None, None     # it came back
            rep.save()
    if rep.notified_at is None or now - rep.notified_at > REMIND_AFTER:
        ErrorReport.objects.filter(pk=rep.pk).update(notified_at=now)
        tell_admins(rep)


def tell_admins(rep):
    from django.contrib.auth import get_user_model

    from . import mailer
    from .models import Notification
    text = f"Something went wrong in Lanes{' on ' + rep.path if rep.path else ''} ({rep.count}×)"
    for u in get_user_model().objects.filter(is_superuser=True, is_active=True):
        Notification.objects.create(recipient=u, kind="system", text=text[:300], excerpt=rep.message[:300],
                                    dedupe_key=f"error:{rep.pk}:{rep.count}")
        if mailer.enabled() and u.email:
            mailer.queue(u.email, "Lanes: something went wrong",
                         f"{text}\n\n{rep.message}\n\nSee {mailer.url('/company/problems/')}", kind="system")
