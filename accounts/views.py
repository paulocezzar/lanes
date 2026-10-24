from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import User


@login_required
@require_POST
def set_theme(request):
    """Small per-person display preferences: theme, colour-blind labels, only-unread notifications."""
    u, changed = request.user, []
    theme = request.POST.get("theme")
    if theme in dict(User.THEMES):
        u.theme = theme
        changed.append("theme")
    for flag in ("colourblind", "only_unread", "welcomed"):
        if flag in request.POST:
            setattr(u, flag, request.POST[flag] in ("1", "true", "on"))
            changed.append(flag)
    if changed:
        u.save(update_fields=changed)
    return JsonResponse({"theme": u.theme, "colourblind": u.colourblind, "only_unread": u.only_unread})


# ---------- forgot password / set password from an email link ----------

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.http import Http404
from django.shortcuts import redirect, render


class ForgotPasswordView(auth_views.PasswordResetView):
    template_name = "accounts/password_forgot.html"
    email_template_name = "emails/password_reset.txt"
    html_email_template_name = "emails/password_reset.html"
    subject_template_name = "emails/password_reset_subject.txt"
    success_url = "/password/forgot/sent/"

    def dispatch(self, request, *args, **kwargs):
        if not settings.EMAIL_ENABLED:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    extra_email_context = {"site_url": settings.SITE_URL}

    def form_valid(self, form):
        from boards import audit
        audit.record(self.request, "password_reset_requested", email=form.cleaned_data["email"][:120])
        try:
            return super().form_valid(form)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Password reset email failed")
            messages.error(self.request, "Lanes couldn't send the email just now. Try again in a few minutes, "
                                         "or ask an admin to reset your password.")
            return redirect("password_reset")


class SetPasswordView(auth_views.PasswordResetConfirmView):
    """Used both for 'forgot password' and for invite emails."""
    template_name = "accounts/password_set.html"
    success_url = "/password/set/done/"
    post_reset_login = False


# Kinds of email people can switch off. Being assigned, mentioned, asked to sign off and join
# requests always come through — someone is waiting on you.
EMAIL_KINDS = [
    ("commented", "Comments", "New comments on cards you're on"),
    ("due_soon", "Due dates", "Cards you're on are due soon"),
    ("item_due_soon", "Checklist due dates", "Checklist items given to you are due soon"),
    ("watched", "Things you watch", "Changes on cards and boards you're watching"),
    ("automation", "Board rules", "Messages sent to you by a board's rules"),
    ("risk", "Early warnings", "A card you're on may miss its date"),
    ("form", "New requests", "Someone sent a request through a form on your board"),
    ("added_to_board", "Added to a board", "Someone adds you to a board"),
    ("budget", "Budget warnings", "A project you own is close to or over budget"),
]


@login_required
def email_prefs(request):
    user = request.user
    if request.method == "POST":
        choice = request.POST.get("email_notifications")
        if choice in dict(User.EMAIL_PREFS):
            user.email_notifications = choice
        on = set(request.POST.getlist("kind"))
        user.email_muted = [k for k, _, _ in EMAIL_KINDS if k not in on]
        user.save(update_fields=["email_notifications", "email_muted"])
        messages.success(request, "Saved.")
        return redirect("email_prefs")
    return render(request, "accounts/email_prefs.html", {
        "prefs": User.EMAIL_PREFS, "email_enabled": settings.EMAIL_ENABLED,
        "kinds": [{"key": k, "name": n, "help": h, "on": k not in (user.email_muted or [])} for k, n, h in EMAIL_KINDS]})
