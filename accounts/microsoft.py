"""Talking to Microsoft Entra ID (Azure AD) without extra libraries."""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings

log = logging.getLogger(__name__)
_app_token = {"value": "", "expires": 0.0}


class MicrosoftError(Exception):
    pass


def configured():
    return bool(settings.MS_TENANT_ID and settings.MS_CLIENT_ID and settings.MS_CLIENT_SECRET)


def authority():
    return f"https://login.microsoftonline.com/{settings.MS_TENANT_ID}"


def post_form(url, data, timeout=15):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:800]
        log.error("Microsoft token endpoint %s: %s", e.code, body)
        try:
            msg = json.loads(body).get("error_description", "").split("\r\n")[0]
        except ValueError:
            msg = ""
        raise MicrosoftError(msg or f"Microsoft returned an error ({e.code}).")
    except (urllib.error.URLError, TimeoutError) as e:
        raise MicrosoftError(f"Couldn't reach Microsoft: {e}")


def app_token():
    """Token for the app itself (client credentials) — used to send mail via Graph."""
    if _app_token["value"] and _app_token["expires"] > time.time() + 60:
        return _app_token["value"]
    data = post_form(f"{authority()}/oauth2/v2.0/token", {
        "client_id": settings.MS_CLIENT_ID, "client_secret": settings.MS_CLIENT_SECRET,
        "grant_type": "client_credentials", "scope": "https://graph.microsoft.com/.default"})
    _app_token.update(value=data["access_token"], expires=time.time() + int(data.get("expires_in", 3000)))
    return _app_token["value"]


def graph_post(path, payload, timeout=20):
    req = urllib.request.Request(f"https://graph.microsoft.com/v1.0{path}", data=json.dumps(payload).encode(),
                                 method="POST", headers={"Authorization": f"Bearer {app_token()}",
                                                         "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        body = e.read()[:800]
        log.error("Graph %s %s: %s", path, e.code, body)
        raise MicrosoftError(f"Microsoft Graph returned {e.code}.")
    except (urllib.error.URLError, TimeoutError) as e:
        raise MicrosoftError(f"Couldn't reach Microsoft Graph: {e}")
