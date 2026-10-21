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
