"""Sign in with Microsoft (Entra ID / Microsoft 365), OpenID Connect code flow
with PKCE.

The ID token comes straight from Microsoft's token endpoint over HTTPS (we
send it the code + client secret), so — as OpenID Connect allows for tokens
received this way — we trust its contents after checking the audience,
issuer/tenant, expiry and nonce, rather than verifying the signature.

Accounts are matched by Microsoft object id once linked, otherwise by email.
"""
import base64
import hashlib
import json
import secrets
import time
import urllib.parse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from . import microsoft

User = get_user_model()


def mode():
    from boards.models import CompanySettings
    return CompanySettings.get().microsoft_login if microsoft.configured() else "off"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _redirect_uri(request):
    return request.build_absolute_uri(reverse("ms_callback"))


def start(request):
    if mode() == "off":
        raise Http404
    verifier = _b64(secrets.token_bytes(48))
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    nxt = request.GET.get("next") or "/"
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        nxt = "/"
    request.session["ms_auth"] = {"state": state, "nonce": nonce, "verifier": verifier, "next": nxt,
                                  "at": time.time()}
    params = {
        "client_id": settings.MS_CLIENT_ID, "response_type": "code", "response_mode": "query",
        "redirect_uri": _redirect_uri(request), "scope": "openid profile email", "state": state, "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return redirect(f"{microsoft.authority()}/oauth2/v2.0/authorize?{urllib.parse.urlencode(params)}")


def _claims(id_token):
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        raise microsoft.MicrosoftError("Microsoft's reply couldn't be read.")


def _fail(request, text):
    messages.error(request, text)
    return redirect("login")


def callback(request):
    if mode() == "off":
        raise Http404
    flow = request.session.pop("ms_auth", None)
    if request.GET.get("error"):
        desc = (request.GET.get("error_description") or "").split("\r\n")[0][:200]
        return _fail(request, f"Microsoft sign-in didn't finish{': ' + desc if desc else '.'}")
    if not flow or request.GET.get("state") != flow["state"] or time.time() - flow["at"] > 900:
        return _fail(request, "That sign-in link expired. Try again.")
    try:
        tokens = microsoft.post_form(f"{microsoft.authority()}/oauth2/v2.0/token", {
            "client_id": settings.MS_CLIENT_ID, "client_secret": settings.MS_CLIENT_SECRET,
            "grant_type": "authorization_code", "code": request.GET.get("code", ""),
            "redirect_uri": _redirect_uri(request), "code_verifier": flow["verifier"],
            "scope": "openid profile email"})
        c = _claims(tokens.get("id_token", ""))
    except microsoft.MicrosoftError as e:
        return _fail(request, f"Microsoft sign-in failed: {e}")
    tenant = settings.MS_TENANT_ID.lower()
    if (c.get("aud") != settings.MS_CLIENT_ID or str(c.get("tid", "")).lower() != tenant
            or not str(c.get("iss", "")).lower().startswith(f"https://login.microsoftonline.com/{tenant}/")
            or c.get("nonce") != flow["nonce"] or float(c.get("exp", 0)) < time.time() - 60):
        return _fail(request, "Microsoft sign-in couldn't be verified. Try again.")
    oid = str(c.get("oid") or "")
    emails = {str(c.get(k) or "").strip().lower() for k in ("email", "preferred_username", "upn")} - {""}
    user = User.objects.filter(ms_oid=oid).first() if oid else None
    if user is None and emails:
        user = User.objects.filter(email__in=emails).first()
        if user and user.ms_oid and user.ms_oid != oid:
            return _fail(request, "That Lanes account is linked to a different Microsoft account.")
    from boards import audit
    from boards.models import CompanySettings
    if user is None:
        email = str(c.get("email") or c.get("preferred_username") or "").strip().lower()
        if not CompanySettings.get().microsoft_auto_create or "@" not in email:
            return _fail(request, f"There's no Lanes account for {email or 'that Microsoft account'} yet. "
                                  "Ask your manager to add you.")
        first = str(c.get("given_name") or "").strip() or (str(c.get("name") or "").split(" ")[0])
        last = str(c.get("family_name") or "").strip() or " ".join(str(c.get("name") or "").split(" ")[1:])
        user = User.objects.create_user(email=email, password=None, first_name=first[:150], last_name=last[:150])
        user.set_unusable_password()
        user.ms_oid = oid
        user.save()
        audit.record(request, "ms_account_created", actor=user, target=user)
    if not user.is_active:
        return _fail(request, "Your Lanes account is switched off. Ask a company admin.")
    if oid and user.ms_oid != oid:
        user.ms_oid = oid
        user.save(update_fields=["ms_oid"])
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session["auth_via"] = "microsoft"         # Microsoft did the two-step check
    return redirect(flow.get("next") or "/")
