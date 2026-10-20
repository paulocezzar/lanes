from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

admin.site.site_header = "Lanes admin"
admin.site.site_title = "Lanes"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="accounts/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/", auth_views.PasswordChangeView.as_view(
        template_name="accounts/password_change.html", success_url="/password/done/"), name="password_change"),
    path("password/done/", auth_views.PasswordChangeDoneView.as_view(
        template_name="accounts/password_done.html"), name="password_change_done"),
    path("", include("boards.urls")),
]
