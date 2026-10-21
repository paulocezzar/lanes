from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .models import User


@login_required
@require_POST
def set_theme(request):
    theme = request.POST.get("theme")
    if theme in dict(User.THEMES):
        request.user.theme = theme
        request.user.save(update_fields=["theme"])
    return JsonResponse({"theme": request.user.theme})


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


@login_required
def email_prefs(request):
    user = request.user
    if request.method == "POST":
        choice = request.POST.get("email_notifications")
        if choice in dict(User.EMAIL_PREFS):
            user.email_notifications = choice
            user.save(update_fields=["email_notifications"])
            messages.success(request, "Saved.")
        return redirect("email_prefs")
    return render(request, "accounts/email_prefs.html", {"prefs": User.EMAIL_PREFS,
                                                         "email_enabled": settings.EMAIL_ENABLED})
