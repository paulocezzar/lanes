"""Two-step sign-in with an authenticator app (Microsoft Authenticator, Google
Authenticator, 1Password…) using standard 6-digit time-based codes (RFC 6238).

Flow: password as usual → if the account has two-step on, a second page asks
for the code (or a one-time recovery code) before the person is signed in.
Company admins can require it (Company → Settings); people who need it but
haven't set it up are sent to the setup page until they do.
"""
import base64
import hashlib
import hmac
import io
import secrets
import struct
import time

from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

User = get_user_model()
ISSUER = "Lanes"
STEP = 30
MAX_TRIES = 5


# ---------- codes ----------

def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def check_code(secret, code, last_step=0, now=None):
    """Return the matching time step (allowing ±30 s of clock drift) or None.
    A step at or before last_step is refused so a code can't be replayed."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6 or not secret:
        return None
    current = int((now or time.time()) // STEP)
    for step in (current - 1, current, current + 1):
        if step > last_step and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def _hash(code):
    return hashlib.sha256(code.replace("-", "").lower().encode()).hexdigest()


def new_recovery_codes(n=8):
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(4)) + "-" +
             "".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(n)]
    return codes, [_hash(c) for c in codes]


def use_recovery_code(user, code):
    h = _hash(code or "")
    if h in (user.recovery_codes or []):
        user.recovery_codes = [x for x in user.recovery_codes if x != h]
        user.save(update_fields=["recovery_codes"])
        return True
    return False


def verify(user, code):
    """Accept an authenticator code or a recovery code."""
    step = check_code(user.totp_secret, code, user.totp_last_step)
    if step is not None:
        user.totp_last_step = step
        user.save(update_fields=["totp_last_step"])
        return True
    return use_recovery_code(user, code)


def otpauth_uri(user, secret):
    from urllib.parse import quote
    label = quote(f"{ISSUER}:{user.email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits=6&period={STEP}"


def qr_svg(data):
    import segno
    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=5, border=2, dark="#172b4d", light="#ffffff",
                                     xmldecl=False, svgns=True)
    return buf.getvalue().decode()


# ---------- who must use it ----------

def required_for(user):
    if not user.is_authenticated:
        return False
    from boards.models import CompanySettings, WorkspaceMember
    rule = CompanySettings.get().require_two_step
    if rule == "everyone":
        return True
    if rule == "staff":
        return not user.is_guest
    if rule == "admins":
        return user.is_superuser or WorkspaceMember.objects.filter(user=user, role=WorkspaceMember.ADMIN).exists()
    return False


class EnforceTwoStepMiddleware:
    """Send people who must use two-step sign-in, but haven't set it up, to the setup page."""
    ALLOWED = ("/account/two-step/", "/logout/", "/static/", "/favicon.ico", "/login/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (user is not None and user.is_authenticated and not user.totp_enabled
                and not request.path.startswith(self.ALLOWED) and required_for(user)):
            if request.path.startswith("/api/"):
                from django.http import JsonResponse
                return JsonResponse({"error": "Set up two-step sign-in first."}, status=403)
            return redirect("two_step_setup")
        return self.get_response(request)


# ---------- sign-in ----------

class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"

    def form_valid(self, form):
        user = form.get_user()
        if not user.totp_enabled:
            return super().form_valid(form)
        s = self.request.session
        s["2s_uid"], s["2s_backend"] = user.pk, user.backend
        s["2s_next"], s["2s_tries"] = self.get_success_url(), 0
        return redirect("login_code")


def login_code(request):
    s = request.session
    uid = s.get("2s_uid")
    if not uid:
        return redirect("login")
    user = User.objects.filter(pk=uid, is_active=True).first()
    if user is None:
        return redirect("login")
    error = ""
    if request.method == "POST":
        from boards import audit
        if verify(user, request.POST.get("code")):
            nxt = s.get("2s_next") or "/"
            backend = s.get("2s_backend")
            for k in ("2s_uid", "2s_backend", "2s_next", "2s_tries"):
                s.pop(k, None)
            login(request, user, backend=backend)
            if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                                   require_https=request.is_secure()):
                nxt = "/"
            if len(user.recovery_codes or []) <= 2:
                messages.warning(request, f"You have {len(user.recovery_codes or [])} recovery codes left. "
                                          "Make new ones from your profile menu → Two-step sign-in.")
            return redirect(nxt)
        s["2s_tries"] = s.get("2s_tries", 0) + 1
        audit.record(request, "two_step_failed", actor=user)
        if s["2s_tries"] >= MAX_TRIES:
            for k in ("2s_uid", "2s_backend", "2s_next", "2s_tries"):
                s.pop(k, None)
            messages.error(request, "Too many wrong codes. Sign in again.")
            return redirect("login")
        error = "That code didn't work. Check your authenticator app and try again."
    return render(request, "accounts/login_code.html", {"error": error, "email": user.email})


# ---------- set up / turn off ----------

@login_required
def setup(request):
    user = request.user
    from boards import audit
    fresh_codes = request.session.pop("2s_fresh_codes", None)
    if user.totp_enabled:
        return render(request, "accounts/two_step.html", {
            "enabled": True, "required": required_for(user), "codes": fresh_codes,
            "codes_left": len(user.recovery_codes or []),
        })
    secret = request.session.get("2s_setup_secret") or new_secret()
    request.session["2s_setup_secret"] = secret
    error = ""
    if request.method == "POST":
        step = check_code(secret, request.POST.get("code"))
        if step is not None:
            codes, hashes = new_recovery_codes()
            user.totp_secret, user.totp_enabled, user.totp_last_step = secret, True, step
            user.recovery_codes = hashes
            user.save(update_fields=["totp_secret", "totp_enabled", "totp_last_step", "recovery_codes"])
            request.session.pop("2s_setup_secret", None)
            request.session["2s_fresh_codes"] = codes
            audit.record(request, "two_step_on", target=user)
            messages.success(request, "Two-step sign-in is on.")
            return redirect("two_step_setup")
        error = "That code didn't match. Make sure your phone's clock is right and try the newest code."
    return render(request, "accounts/two_step.html", {
        "enabled": False, "required": required_for(user), "secret": secret,
        "secret_spaced": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)),
        "qr": qr_svg(otpauth_uri(user, secret)), "error": error,
    })


@login_required
@require_POST
def disable(request):
    user = request.user
    from boards import audit
    if required_for(user):
        messages.error(request, "Your company requires two-step sign-in, so it can't be turned off.")
    elif not verify(user, request.POST.get("code")):
        messages.error(request, "That code didn't work, so two-step sign-in is still on.")
    else:
        user.totp_enabled, user.totp_secret, user.recovery_codes = False, "", []
        user.save(update_fields=["totp_enabled", "totp_secret", "recovery_codes"])
        audit.record(request, "two_step_off", target=user)
        messages.success(request, "Two-step sign-in is off.")
    return redirect("two_step_setup")


@login_required
@require_POST
def new_codes(request):
    user = request.user
    if not user.totp_enabled:
        return redirect("two_step_setup")
    if not verify(user, request.POST.get("code")):
        messages.error(request, "That code didn't work.")
        return redirect("two_step_setup")
    codes, hashes = new_recovery_codes()
    user.recovery_codes = hashes
    user.save(update_fields=["recovery_codes"])
    request.session["2s_fresh_codes"] = codes
    messages.success(request, "New recovery codes made. The old ones no longer work.")
    return redirect("two_step_setup")


@login_required
@require_POST
def admin_reset(request, user_id):
    """Company admins can switch two-step off for someone who lost their phone.
    They set it up again next time they sign in (if it's required)."""
    if not request.user.is_superuser:
        raise Http404
    person = get_object_or_404(User, pk=user_id)
    person.totp_enabled, person.totp_secret, person.recovery_codes = False, "", []
    person.save(update_fields=["totp_enabled", "totp_secret", "recovery_codes"])
    from boards import audit
    audit.record(request, "two_step_reset", target=person)
    messages.success(request, f"Two-step sign-in reset for {person.display_name}. They'll set it up again at next sign-in.")
    return redirect(request.POST.get("next") or reverse("company_people"))
