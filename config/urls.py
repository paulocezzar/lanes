from django.templatetags.static import static
from django.shortcuts import redirect
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from accounts import twofactor
from accounts.views import set_theme

def _admin_login(request, extra_context=None):
    """The admin has its own sign-in form, which would skip two-step codes —
    send everyone through the normal Lanes sign-in instead."""
    from urllib.parse import quote
    from django.shortcuts import redirect
    return redirect("/login/?next=" + quote(request.GET.get("next") or "/admin/"))


admin.site.login = _admin_login
admin.site.site_header = "Lanes admin"
admin.site.site_title = "Lanes"

urlpatterns = [
    path("favicon.ico", lambda request: redirect(static("lanes/favicon.svg"), permanent=True)),
    path("admin/", admin.site.urls),
    path("login/", twofactor.LoginView.as_view(), name="login"),
    path("login/code/", twofactor.login_code, name="login_code"),
    path("account/two-step/", twofactor.setup, name="two_step_setup"),
    path("account/two-step/off/", twofactor.disable, name="two_step_disable"),
    path("account/two-step/new-codes/", twofactor.new_codes, name="two_step_new_codes"),
    path("account/two-step/reset/<int:user_id>/", twofactor.admin_reset, name="two_step_reset"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("account/theme/", set_theme, name="set_theme"),
    path("password/", auth_views.PasswordChangeView.as_view(
        template_name="accounts/password_change.html", success_url="/password/done/"), name="password_change"),
    path("password/done/", auth_views.PasswordChangeDoneView.as_view(
        template_name="accounts/password_done.html"), name="password_change_done"),
    path("", include("boards.urls")),
]
