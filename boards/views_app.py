"""The installable app: manifest, service worker and the offline page."""
import hashlib
from pathlib import Path

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.cache import never_cache

SW_FILE = Path(settings.BASE_DIR) / "static" / "lanes" / "sw.js"


@never_cache
def service_worker(request):
    src = SW_FILE.read_text()
    version = hashlib.sha1((src + static("lanes/app.css") + static("lanes/board.js")).encode()).hexdigest()[:10]
    resp = HttpResponse(src.replace("__VERSION__", version), content_type="application/javascript")
    resp["Service-Worker-Allowed"] = "/"
    return resp


def manifest(request):
    return JsonResponse({
        "name": "Lanes", "short_name": "Lanes", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#1d2125", "theme_color": "#ff8c50",
        "description": "Boards, cards and checklists for your team.",
        "icons": [
            {"src": static("lanes/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("lanes/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": static("lanes/icon-maskable-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
            {"src": static("lanes/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }, content_type="application/manifest+json")


def offline(request):
    return render(request, "offline.html")
