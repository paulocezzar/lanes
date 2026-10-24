from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create(self, email, password, **extra):
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create(email, password, **extra)


class User(AbstractUser):
    """Signs in by email. Kept as a custom model from day one so Microsoft
    sign-in can be added later without a user-table migration headache."""

    username = None
    email = models.EmailField(unique=True)
    THEMES = [("system", "Match my device"), ("light", "Light"), ("dark", "Dark")]
    theme = models.CharField(max_length=10, choices=THEMES, default="system")
    # Guests (suppliers, contractors…) only ever see the boards they're added to.
    is_guest = models.BooleanField(default=False)
    # Two-step sign-in (authenticator app). Secret is only kept once confirmed.
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_enabled = models.BooleanField(default=False)
    totp_last_step = models.BigIntegerField(default=0)        # stops the same code being used twice
    recovery_codes = models.JSONField(default=list, blank=True)  # hashed, one-time
    EMAIL_PREFS = [("instant", "As they happen (grouped, every few minutes)"),
                   ("daily", "Once a day — a morning summary"), ("off", "Never — just the bell in Lanes")]
    ical_token = models.CharField(max_length=40, blank=True, db_index=True)   # private calendar-feed link
    recent_boards = models.JSONField(default=list, blank=True)   # board ids, most recent first
    capacity_hours = models.DecimalField(max_digits=5, decimal_places=2, default=37.5)   # working hours a week
    hourly_rate = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)  # for labour cost
    colourblind = models.BooleanField(default=False)       # patterns on labels as well as colour
    welcomed = models.BooleanField(default=False)          # has dismissed the first-time welcome
    only_unread = models.BooleanField(default=False)       # notifications panel: hide read ones
    email_notifications = models.CharField(max_length=10, choices=EMAIL_PREFS, default="daily")
    last_digest_on = models.DateField(null=True, blank=True)
    job_title = models.CharField(max_length=100, blank=True)
    bio = models.CharField(max_length=500, blank=True)
    # Notification kinds this person doesn't want emailed (they still show in the bell)
    email_muted = models.JSONField(default=list, blank=True)
    ms_oid = models.CharField(max_length=64, blank=True, db_index=True)   # Microsoft account id, once linked

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    objects = UserManager()

    class Meta:
        ordering = ["first_name", "last_name"]

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        return self.get_full_name() or self.email

    @property
    def initials(self):
        parts = [p for p in (self.first_name, self.last_name) if p]
        if parts:
            return "".join(p[0] for p in parts).upper()[:2]
        return self.email[:2].upper()

    @property
    def avatar_hue(self):
        """Stable colour per person for the initials bubble."""
        return sum(ord(c) for c in self.email) * 37 % 360
