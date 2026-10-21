def notifications(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    from .models import Notification
    return {"unread_count": Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()}
