from django.conf import settings


def notifications(request):
    ctx = {"email_enabled": settings.EMAIL_ENABLED}
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return ctx
    from .models import Notification
    from .timetrack import running
    ctx["running_timer"] = running(request.user)
    from .gallery import TEMPLATES
    ctx["create_templates"] = [(t["key"], t["title"], t["bg"]) for t in TEMPLATES[:6]]
    ctx["unread_count"] = Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()
    return ctx
