from django.conf import settings


def notifications(request):
    ctx = {"email_enabled": settings.EMAIL_ENABLED}
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return ctx
    from .models import Notification
    ctx["unread_count"] = Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()
    return ctx
