"""Lanes settings. Everything environment-specific comes from env vars so the
same file runs locally (SQLite) and on Render (PostgreSQL)."""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-not-secret")
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = [h for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])
CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1")]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "boards",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "accounts.twofactor.EnforceTwoStepMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "boards.context_processors.notifications",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
    )
}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = "Europe/London"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
if DEBUG:
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
ATTACHMENT_MAX_MB = int(os.environ.get("ATTACHMENT_MAX_MB", "25"))

# Attachments go to S3-compatible object storage (e.g. Cloudflare R2) when
# configured. Without it they're saved on the server's disk, which Render
# wipes on every deploy — fine for testing only.
if os.environ.get("AWS_STORAGE_BUCKET_NAME"):
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": os.environ["AWS_STORAGE_BUCKET_NAME"],
            "endpoint_url": os.environ.get("AWS_S3_ENDPOINT_URL") or None,
            "region_name": os.environ.get("AWS_S3_REGION_NAME", "auto"),
            "access_key": os.environ.get("AWS_ACCESS_KEY_ID"),
            "secret_key": os.environ.get("AWS_SECRET_ACCESS_KEY"),
            "default_acl": None,
            "querystring_auth": True,      # private bucket; links are signed and expire
            "querystring_expire": 3600,
            "file_overwrite": False,
        },
    }
    ATTACHMENTS_PERSISTENT = True
else:
    ATTACHMENTS_PERSISTENT = DEBUG

DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# AI features (inbox capture, suggest steps). Off unless a key is set.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-5-5")
ANTHROPIC_DRAFT_MODEL = os.environ.get("ANTHROPIC_DRAFT_MODEL", "claude-sonnet-5-5")   # setting up boards

# Shared secret the Render cron job sends to /internal/run-automations/.
CRON_TOKEN = os.environ.get("CRON_TOKEN", "")

# Public address of Lanes, used for links in emails (the cron job has no request to read it from).
SITE_URL = (os.environ.get("SITE_URL") or (f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}"
            if os.environ.get("RENDER_EXTERNAL_HOSTNAME") else "http://localhost:8000")).rstrip("/")

# Microsoft Entra ID (Microsoft 365) app registration — used for "Sign in with
# Microsoft" and, if MS_MAIL_SENDER is set, for sending email through Microsoft 365.
MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "")
MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID", "")
MS_CLIENT_SECRET = os.environ.get("MS_CLIENT_SECRET", "")
MS_MAIL_SENDER = os.environ.get("MS_MAIL_SENDER", "")      # e.g. lanes@thenewt.co.uk

# Email. Either a normal SMTP service (EMAIL_HOST…) or Microsoft 365 via Graph
# (MS_MAIL_SENDER + the app registration above). Without either, nothing is sent.
if os.environ.get("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
    EMAIL_TIMEOUT = 20
    EMAIL_ENABLED, EMAIL_VIA = True, f"SMTP ({EMAIL_HOST})"
elif MS_MAIL_SENDER and MS_TENANT_ID and MS_CLIENT_ID and MS_CLIENT_SECRET:
    EMAIL_BACKEND = "accounts.graph_mail.GraphEmailBackend"
    EMAIL_ENABLED, EMAIL_VIA = True, "Microsoft 365"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend" if DEBUG else \
        "django.core.mail.backends.dummy.EmailBackend"
    EMAIL_ENABLED, EMAIL_VIA = False, ""
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL") or (f"Lanes <{MS_MAIL_SENDER}>" if MS_MAIL_SENDER
                                                               else "Lanes <lanes@localhost>")
PASSWORD_RESET_TIMEOUT = 7 * 24 * 3600     # invite / reset links last a week

# Send errors (with tracebacks) to the console so they appear in Render's logs.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"},
                 "alert": {"class": "boards.errors.AdminAlertHandler", "level": "ERROR"}},
    "root": {"handlers": ["console", "alert"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["console", "alert"], "level": "ERROR", "propagate": False},
        "botocore": {"level": "WARNING"},
        "boto3": {"level": "WARNING"},
    },
}

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
