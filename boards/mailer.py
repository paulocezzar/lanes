"""Sending email. Everything goes through the OutgoingEmail queue, which the
cron job empties every few minutes (single invites are also tried at once).

- Notification emails: people who chose "as they happen" get one email per
  few minutes listing new unread notifications (only ones still unread after
  2 minutes, so nobody is emailed about something they've just seen).
- Daily summary at 7am for people who chose "once a day".
- Invites: a link to set a password (valid a week).
"""
import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import Card, Notification, OutgoingEmail

log = logging.getLogger(__name__)
User = get_user_model()


def enabled():
    return settings.EMAIL_ENABLED


def url(path):
    return settings.SITE_URL + path


def render(name, ctx):
    ctx = {"site_url": settings.SITE_URL, **ctx}
    text = render_to_string(f"emails/{name}.txt", ctx).strip()
    html = render_to_string(f"emails/{name}.html", ctx)
    return text, html


def queue(to, subject, text, html="", kind=""):
    return OutgoingEmail.objects.create(to=to, subject=subject[:200], text=text, html=html, kind=kind)


def send_one(e):
    e.attempts += 1
    try:
        msg = EmailMultiAlternatives(e.subject, e.text, settings.DEFAULT_FROM_EMAIL, [e.to])
        if e.html:
            msg.attach_alternative(e.html, "text/html")
        msg.send()
        e.sent_at, e.error = timezone.now(), ""
    except Exception as exc:          # keep it queued; tried again next run (up to 5 times)
        log.warning("Email to %s failed: %s", e.to, exc)
        e.error = str(exc)[:300]
    e.save(update_fields=["attempts", "sent_at", "error"])
    return e.sent_at is not None


def flush(limit=200, budget=None):
    """Send queued email, stopping after `limit` emails or `budget` seconds."""
    import time
    if not enabled():
        return 0
    stop = time.monotonic() + budget if budget else None
    sent = 0
    for e in OutgoingEmail.objects.filter(sent_at__isnull=True, attempts__lt=5)[:limit]:
        if stop and time.monotonic() > stop:
            break
        sent += send_one(e)
    return sent


# ---------- invites ----------

def set_password_link(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return url(reverse("password_reset_confirm", kwargs={"uidb64": uid, "token": token}))


def invite(user, inviter=None, where="", now=True):
    """Email someone a link to set their password and sign in."""
    if not enabled():
        return None
    from .models import CompanySettings
    ms = CompanySettings.get().microsoft_login != "off" and not user.is_guest
    text, html = render("invite", {"user": user, "inviter": inviter, "where": where,
                                   "link": set_password_link(user), "login": url("/login/"), "microsoft": ms})
    who = inviter.display_name if inviter else "Your company"
    e = queue(user.email, f"{who} invited you to Lanes", text, html, "invite")
    if now:
        send_one(e)
    return e


# ---------- notifications ----------

def notification_emails(now=None):
    """One email per person with their new unread notifications."""
    if not enabled():
        return 0
    now = now or timezone.now()
    pending = (Notification.objects.filter(emailed_at__isnull=True, read_at__isnull=True,
                                           created_at__lte=now - timedelta(minutes=2),
                                           created_at__gte=now - timedelta(days=2),
                                           recipient__is_active=True, recipient__email_notifications="instant")
               .select_related("recipient", "actor", "card", "board").order_by("created_at"))
    by_user = defaultdict(list)
    for n in pending[:2000]:
        by_user[n.recipient].append(n)
    count = 0
    for user, items in by_user.items():
        first = items[0]
        if len(items) == 1:
            who = first.actor.display_name if first.actor else "Lanes"
            subject = f"{who} {first.text}"
        else:
            subject = f"{len(items)} new updates on Lanes"
        text, html = render("notifications", {"user": user, "items": [_item(n) for n in items],
                                              "prefs": url("/account/email/")})
        queue(user.email, subject, text, html, "notification")
        Notification.objects.filter(pk__in=[n.pk for n in items]).update(emailed_at=now)
        count += 1
    # people who don't want emails: mark as handled so they're never sent later
    Notification.objects.filter(emailed_at__isnull=True, created_at__lte=now - timedelta(minutes=2)) \
        .exclude(recipient__email_notifications="instant").update(emailed_at=now)
    return count


def _item(n):
    return {"who": n.actor.display_name if n.actor else "Lanes", "text": n.text, "excerpt": n.excerpt,
            "where": n.board.title if n.board else "", "link": url(reverse("notification_open", args=[n.pk])),
            "when": timezone.localtime(n.created_at).strftime("%a %-d %b, %H:%M")}


def daily_digests(now=None, hour=7):
    """A 7am summary for people who chose 'once a day'."""
    if not enabled():
        return 0
    now = now or timezone.now()
    local = timezone.localtime(now)
    if local.hour < hour:
        return 0
    today = local.date()
    count = 0
    for user in User.objects.filter(is_active=True, email_notifications="daily").exclude(last_digest_on=today):
        unread = list(Notification.objects.filter(recipient=user, read_at__isnull=True,
                                                  created_at__gte=now - timedelta(days=1))
                      .select_related("actor", "board", "card")[:30])
        due = list(Card.objects.filter(members=user, is_complete=False, is_archived=False, is_template=False,
                                       list__is_archived=False, list__board__is_archived=False,
                                       due_at__lte=now + timedelta(days=1)).select_related("list__board")
                   .order_by("due_at")[:20])
        user.last_digest_on = today
        user.save(update_fields=["last_digest_on"])
        if not unread and not due:
            continue
        text, html = render("digest", {
            "user": user, "items": [_item(n) for n in unread], "prefs": url("/account/email/"),
            "due": [{"title": c.title, "where": f"{c.list.board.title} › {c.list.title}",
                     "when": timezone.localtime(c.due_at).strftime("%a %-d %b, %H:%M"),
                     "overdue": c.due_at < now, "link": url(f"/b/{c.list.board_id}/?card={c.pk}")} for c in due],
        })
        queue(user.email, f"Your Lanes summary for {local:%A %-d %B}", text, html, "digest")
        count += 1
    return count


def weekly_status_reports(now=None, hour=8):
    """Monday from 08:00: each project board's status report to its owner, admins and watchers."""
    from .models import Activity, Board, BoardMember, Watch
    from .portfolio import health
    if not enabled():
        return 0
    now = now or timezone.now()
    local = timezone.localtime(now)
    if local.weekday() != 0 or local.hour < hour:
        return 0
    week = local.date().isoformat()
    sent = 0
    for board in Board.objects.filter(is_project=True, is_archived=False).select_related("project_owner", "workspace"):
        if Activity.objects.filter(board=board, verb="report_sent", data__week=week).exists():
            continue
        people = {m.user for m in BoardMember.objects.filter(board=board, role="admin").select_related("user")}
        people |= {w.user for w in Watch.objects.filter(board=board).select_related("user")}
        if board.project_owner:
            people.add(board.project_owner)
        h = health(board, now)
        text, html = render("status_report", {"h": h, "board": board, "link": url(f"/b/{board.pk}/report/"),
                                              "date": local.strftime("%A %-d %B")})
        for u in people:
            if u.is_active and u.email:
                queue(u.email, f"{board.title}: {h['rag_label']} — weekly status", text, html, "status_report")
        Activity.objects.create(board=board, actor=None, verb="report_sent", data={"to": len(people), "week": week})
        sent += 1
    return sent


def run(now=None):
    """Called by the cron job."""
    n = notification_emails(now)
    d = daily_digests(now)
    weekly_status_reports(now)
    s = flush()
    return n, d, s
