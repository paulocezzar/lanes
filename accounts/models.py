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
