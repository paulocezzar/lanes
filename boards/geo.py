"""Turning a location field ("BA4 6NT", "Pilton, Somerset", "51.16,-2.58")
into map coordinates. UK postcodes go to postcodes.io; anything else to
OpenStreetMap's Nominatim. Results are stored on the field value so each
address is only looked up once."""
import json
import logging
import re
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
LATLNG = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
UA = "Lanes/1.0 (internal team boards)"


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def lookup(text, timeout=3):
    """(lat, lng) or None. Never raises."""
    text = (text or "").strip()
    if not text:
        return None
    m = LATLNG.match(text)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None
    try:
        pc = POSTCODE.search(text)
        if pc:
            code = f"{pc.group(1)}{pc.group(2)}".upper()
            data = _get(f"https://api.postcodes.io/postcodes/{urllib.parse.quote(code)}", timeout)
            res = data.get("result") or {}
            if res.get("latitude") is not None:
                return float(res["latitude"]), float(res["longitude"])
        q = urllib.parse.urlencode({"q": text, "format": "json", "limit": 1, "countrycodes": "gb"})
        data = _get(f"https://nominatim.openstreetmap.org/search?{q}", timeout)
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:  # offline, rate-limited, bad reply — just leave it unplaced
        log.warning("Geocoding %r failed: %s", text, e)
    return None


def place(value, budget_until=None):
    """Geocode a location CardFieldValue if its text changed since last time.
    Returns True if it now has coordinates."""
    text = str(value.value or "")
    if value.geocoded_for == text:
        return value.lat is not None
    if budget_until is not None and time.monotonic() > budget_until:
        return False
    coords = lookup(text)
    value.lat, value.lng = coords if coords else (None, None)
    value.geocoded_for = text
    value.save(update_fields=["lat", "lng", "geocoded_for"])
    return coords is not None
