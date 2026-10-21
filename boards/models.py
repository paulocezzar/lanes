"""Lanes data model.

Workspace (a department) → Board → List → Card.
Ordering uses a float `position`: dropping between 1000 and 2000 stores 1500,
so a move writes one row. `positions.py` re-spaces a container when gaps get
too small.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

User = settings.AUTH_USER_MODEL


def empty_list():
    return []


STICKERS = "⭐ ✅ ⚠️ 🔥 ❤️ 👍 🎄 🎂 🥐 🍞 🚚 📦 💷 ❄️ 🧁 🍰 🎉 ⏰ 📌 💡".split()
COLOURS = ["green", "yellow", "orange", "red", "purple", "blue", "sky", "pink", "grey"]


class Workspace(models.Model):
    """One per department (Bakery, Butchery, Kitchen…)."""

    name = models.CharField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def initial(self):
        return self.name[:1].upper()


class WorkspaceMember(models.Model):
    ADMIN, MEMBER = "admin", "member"
    ROLES = [(ADMIN, "Admin"), (MEMBER, "Member")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="workspace_memberships")
    role = models.CharField(max_length=10, choices=ROLES, default=MEMBER)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["workspace", "user"], name="uniq_workspace_member")]


class Collection(models.Model):
    """A named group of boards inside a department, e.g. "Christmas 2026"."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="collections")
    name = models.CharField(max_length=60)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["workspace", "name"], name="uniq_collection_name")]

    def __str__(self):
        return self.name


class Board(models.Model):
    PRIVATE, WORKSPACE = "private", "workspace"
    VISIBILITY = [(PRIVATE, "Private — only people added to the board"), (WORKSPACE, "Department — everyone in it can see")]

    BACKGROUNDS = [
        ("dusk", "Dusk"),
        ("ocean", "Ocean"),
        ("forest", "Forest"),
        ("ember", "Ember"),
        ("slate", "Slate"),
        ("wheat", "Wheat"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="boards")
    title = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    background = models.CharField(max_length=20, choices=BACKGROUNDS, default="dusk")
    visibility = models.CharField(max_length=10, choices=VISIBILITY, default=WORKSPACE)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    is_archived = models.BooleanField(default=False)
    # Bumped on every change to anything on the board; browsers poll it to
    # know when to refresh (see signals.py and api.board_state).
    version = models.PositiveBigIntegerField(default=0)
    is_template = models.BooleanField(default=False)
    collection = models.ForeignKey(Collection, on_delete=models.SET_NULL, null=True, blank=True, related_name="boards")
    voting = models.BooleanField(default=False)       # 👍 votes on cards
    card_aging = models.BooleanField(default=False)   # untouched cards slowly fade
    public_token = models.CharField(max_length=40, blank=True, db_index=True)  # read-only share link when set

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title


class BoardMember(models.Model):
    ADMIN, MEMBER, OBSERVER = "admin", "member", "observer"
    ROLES = [(ADMIN, "Admin"), (MEMBER, "Member"), (OBSERVER, "Observer")]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="board_memberships")
    role = models.CharField(max_length=10, choices=ROLES, default=MEMBER)
    starred = models.BooleanField(default=False)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["added_at"]
        constraints = [models.UniqueConstraint(fields=["board", "user"], name="uniq_board_member")]


class Label(models.Model):
    COLOURS = [
        ("green", "Green"), ("yellow", "Yellow"), ("orange", "Orange"), ("red", "Red"),
        ("purple", "Purple"), ("blue", "Blue"), ("sky", "Sky"), ("pink", "Pink"), ("grey", "Grey"),
    ]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=40, blank=True)
    colour = models.CharField(max_length=10, choices=COLOURS, default="green")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name or self.get_colour_display()


class List(models.Model):
    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="lists")
    title = models.CharField(max_length=120)
    position = models.FloatField(default=1000)
    is_archived = models.BooleanField(default=False)
    colour = models.CharField(max_length=10, blank=True)            # one of Label.COLOURS keys, or ""
    card_limit = models.PositiveSmallIntegerField(null=True, blank=True)  # work-in-progress limit

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.title

    @property
    def over_limit(self):
        return bool(self.card_limit) and len(self.entries()) > self.card_limit

    def entries(self):
        """Cards and mirrored cards in this list, in order. Uses prefetched
        `cards` / `mirrors` when the caller set them up."""
        items = [(c.position, 0, c.pk, c, None) for c in self.cards.all()]
        items += [(m.position, 1, m.pk, m.card, m) for m in self.mirrors.all() if not m.card.is_archived]
        return [{"card": c, "mirror": m} for _, _, _, c, m in sorted(items, key=lambda x: x[:3])]


class Card(models.Model):
    list = models.ForeignKey(List, on_delete=models.CASCADE, related_name="cards")
    title = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    position = models.FloatField(default=1000)
    start_date = models.DateField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    is_complete = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    is_template = models.BooleanField(default=False)
    cover = models.ForeignKey("Attachment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cover_colour = models.CharField(max_length=10, blank=True)      # colour cover when there's no image
    stickers = models.JSONField(default=empty_list, blank=True)     # up to 6 emoji
    labels = models.ManyToManyField(Label, blank=True, related_name="cards")
    members = models.ManyToManyField(User, blank=True, related_name="assigned_cards")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.title

    @property
    def board(self):
        return self.list.board

    @property
    def due_state(self):
        """'complete', 'overdue', 'soon' (within 24h) or '' — drives the date chip colour."""
        if not self.due_at:
            return ""
        if self.is_complete:
            return "complete"
        now = timezone.now()
        if self.due_at < now:
            return "overdue"
        if (self.due_at - now).total_seconds() < 86400:
            return "soon"
        return ""

    @property
    def token(self):
        return f"c{self.pk}"

    def tile_fields(self):
        """Custom field values to show as badges on the card tile."""
        vals = [v for v in self.field_values.all() if v.field.show_on_card and v.display()]
        return sorted(vals, key=lambda v: (v.field.position, v.field_id))

    @property
    def age_class(self):
        """'aged-1/2/3' after 1, 2 and 4 weeks untouched, when the board has card ageing on."""
        if self.is_complete or not self.list.board.card_aging:
            return ""
        days = (timezone.now() - self.updated_at).days
        return "aged-3" if days >= 28 else "aged-2" if days >= 14 else "aged-1" if days >= 7 else ""

    def checklist_counts(self):
        items = [i for cl in self.checklists.all() for i in cl.items.all()]
        return sum(1 for i in items if i.is_done), len(items)


class Checklist(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="checklists")
    title = models.CharField(max_length=120, default="Checklist")
    position = models.FloatField(default=1000)
    hide_checked = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "id"]

    def progress(self):
        items = list(self.items.all())
        if not items:
            return 0
        return round(100 * sum(1 for i in items if i.is_done) / len(items))


class ChecklistItem(models.Model):
    checklist = models.ForeignKey(Checklist, on_delete=models.CASCADE, related_name="items")
    text = models.CharField(max_length=250)
    is_done = models.BooleanField(default=False)
    position = models.FloatField(default=1000)
    done_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    done_at = models.DateTimeField(null=True, blank=True)
    # Advanced checklists: an owner and a due date per item
    assignee = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="assigned_items")
    due_at = models.DateTimeField(null=True, blank=True)

    @property
    def due_state(self):
        if not self.due_at:
            return ""
        if self.is_done:
            return "complete"
        now = timezone.now()
        if self.due_at < now:
            return "overdue"
        return "soon" if (self.due_at - now).total_seconds() < 86400 else ""

    class Meta:
        ordering = ["position", "id"]


class CustomField(models.Model):
    """A board-level field every card can fill in: Quantity, Supplier, Site…"""

    TEXT, NUMBER, DATE, CHECKBOX, DROPDOWN, LOCATION = "text", "number", "date", "checkbox", "dropdown", "location"
    KINDS = [(TEXT, "Text"), (NUMBER, "Number"), (DATE, "Date"), (CHECKBOX, "Checkbox"), (DROPDOWN, "Dropdown"),
             (LOCATION, "Location")]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="custom_fields")
    name = models.CharField(max_length=60)
    kind = models.CharField(max_length=10, choices=KINDS, default=TEXT)
    options = models.JSONField(default=list, blank=True)     # dropdown choices
    show_on_card = models.BooleanField(default=True)          # badge on the card tile
    position = models.FloatField(default=1000)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name

    def clean_value(self, raw):
        """Normalise an incoming value; returns None to clear. Raises ValueError if invalid."""
        if raw in (None, "", []):
            return None
        if self.kind == self.NUMBER:
            n = float(raw)
            return int(n) if n.is_integer() else round(n, 4)
        if self.kind == self.CHECKBOX:
            return True if raw in (True, "true", "1", 1, "on") else None
        if self.kind == self.DATE:
            from django.utils.dateparse import parse_date
            d = parse_date(str(raw))
            if not d:
                raise ValueError("Not a date")
            return d.isoformat()
        if self.kind == self.DROPDOWN:
            if raw not in self.options:
                raise ValueError("Not one of the options")
            return raw
        if self.kind == self.LOCATION:
            return str(raw).strip()[:300] or None
        return str(raw).strip()[:500] or None


class CardFieldValue(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="field_values")
    field = models.ForeignKey(CustomField, on_delete=models.CASCADE, related_name="values")
    value = models.JSONField()
    # Location fields: map coordinates, looked up once per address (see geo.py)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    geocoded_for = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["card", "field"], name="uniq_card_field")]

    @property
    def map_url(self):
        from urllib.parse import quote
        return f"https://www.google.com/maps/search/?api=1&query={quote(str(self.value))}"

    def display(self):
        f, v = self.field, self.value
        if f.kind == CustomField.CHECKBOX:
            return "✓" if v else ""
        if f.kind == CustomField.LOCATION:
            return f"📍 {v}"
        if f.kind == CustomField.DATE:
            from datetime import date
            try:
                return date.fromisoformat(v).strftime("%-d %b %Y")
            except (TypeError, ValueError):
                return str(v)
        return str(v)


class Comment(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def reaction_groups(self, user):
        """[{emoji, count, mine, names}] in first-used order."""
        groups = {}
        for r in self.reactions.all():
            g = groups.setdefault(r.emoji, {"emoji": r.emoji, "count": 0, "mine": False, "names": []})
            g["count"] += 1
            g["names"].append(r.user.display_name)
            g["mine"] = g["mine"] or r.user_id == user.pk
        return list(groups.values())


class Activity(models.Model):
    """Append-only log of everything that happens on a board. Drives the card
    activity feed, the board activity panel and (later) notifications."""

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="activity")
    card = models.ForeignKey(Card, on_delete=models.CASCADE, null=True, blank=True, related_name="activity")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    verb = models.CharField(max_length=40)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["board", "-created_at"]), models.Index(fields=["card", "-created_at"])]

    def text(self):
        d = self.data
        return {
            "card_created": f"added this card to {d.get('list', '')}",
            "card_moved": f"moved this card from {d.get('from', '')} to {d.get('to', '')}",
            "card_completed": "marked this card as complete",
            "card_reopened": "marked this card as incomplete",
            "card_archived": "archived this card",
            "card_restored": "sent this card back to the board",
            "card_renamed": f"renamed this card from “{d.get('from', '')}”",
            "due_set": f"set the due date to {d.get('due', '')}",
            "due_removed": "removed the due date",
            "member_added": f"added {d.get('name', '')} to this card",
            "member_joined": "joined this card",
            "member_removed": f"removed {d.get('name', '')} from this card",
            "member_left": "left this card",
            "checklist_added": f"added checklist {d.get('title', '')}",
            "item_done": f"completed {d.get('text', '')}",
            "item_undone": f"marked {d.get('text', '')} incomplete",
            "list_created": f"added list {d.get('title', '')}",
            "board_created": "created this board",
            "board_member_added": f"added {d.get('name', '')} to this board",
            "automation": d.get("summary", "ran an automation"),
            "mirrored": f"mirrored this card to {d.get('to', '')}",
            "unmirrored": f"removed the mirror from {d.get('frm', '')}",
            "mirror_added": f"mirrored a card here from {d.get('source', '')} ({d.get('list', '')})",
            "attachment_added": f"attached {d.get('name', 'a file')}",
            "card_moved_board": f"moved this card here from {d.get('frm', '')} ({d.get('to', '')})",
            "card_moved_out": f"moved “{d.get('title', '')}” to {d.get('to', '')}",
            "card_deleted": f"deleted the card “{d.get('title', '')}”",
            "list_archived": f"archived list {d.get('title', '')}",
            "list_restored": f"restored list {d.get('title', '')}",
            "button": f"pressed {d.get('name', 'a button')}: {d.get('summary', '')}",
        }.get(self.verb, self.verb.replace("_", " "))


class Rule(models.Model):
    """Board automation: when <trigger> happens to a card, run <actions> on it."""

    ADDED, MOVED, COMPLETED, CHECKLIST_DONE, LABEL_ADDED, DUE_SOON, OVERDUE = (
        "card_added", "card_moved", "card_completed", "checklist_done", "label_added", "card_due_soon",
        "card_overdue")
    TRIGGERS = [
        (ADDED, "a card is added to list…"),
        (MOVED, "a card is moved into list…"),
        (COMPLETED, "a card is marked complete"),
        (CHECKLIST_DONE, "all checklist items on a card are done"),
        (LABEL_ADDED, "label … is added to a card"),
        (DUE_SOON, "a card is due within…"),
        (OVERDUE, "a card becomes overdue"),
    ]
    TIME_TRIGGERS = (DUE_SOON, OVERDUE)
    HOURS = [(1, "1 hour"), (3, "3 hours"), (12, "12 hours"), (24, "1 day"), (48, "2 days"), (72, "3 days"),
             (168, "1 week")]
    ACTIONS = [
        ("move", "move it to list…"),
        ("move_top", "move it to the top of its list"),
        ("complete", "mark it complete"),
        ("incomplete", "mark it incomplete"),
        ("check_all", "tick all its checklist items"),
        ("add_label", "add label…"),
        ("remove_label", "remove label…"),
        ("add_member", "add member…"),
        ("clear_members", "remove all members"),
        ("set_due", "set due date to … days from now"),
        ("clear_due", "remove the due date"),
        ("comment", "post comment…"),
        ("notify", "notify its members…"),
        ("add_sticker", "add sticker…"),
        ("archive", "archive it"),
    ]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="rules")
    trigger = models.CharField(max_length=20, choices=TRIGGERS)
    trigger_list = models.ForeignKey(List, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    trigger_label = models.ForeignKey(Label, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    trigger_hours = models.PositiveSmallIntegerField(null=True, blank=True)   # for "due within…"
    actions = models.JSONField(default=list)   # [{"type": "move", "list": 3}, {"type": "complete"}]
    is_enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    run_count = models.PositiveIntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


class RuleFire(models.Model):
    """A time-based rule ran for this card and due date — so it runs once per
    due date, and again only if the date changes."""

    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name="fires")
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="+")
    due_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule", "card", "due_at"], name="uniq_rule_fire")]


class Schedule(models.Model):
    """Recurring card: on a schedule, create a card (with optional checklist)."""

    DAILY, WEEKDAYS, WEEKLY, MONTHLY = "daily", "weekdays", "weekly", "monthly"
    FREQUENCIES = [(DAILY, "Every day"), (WEEKDAYS, "Every weekday (Mon–Fri)"),
                   (WEEKLY, "Every week on…"), (MONTHLY, "Every month on day…")]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="schedules")
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default=WEEKLY)
    weekday = models.PositiveSmallIntegerField(default=0)      # 0 = Monday, for weekly
    month_day = models.PositiveSmallIntegerField(default=1)    # 1–28, for monthly
    hour = models.PositiveSmallIntegerField(default=7)         # local time
    target_list = models.ForeignKey(List, on_delete=models.CASCADE, related_name="+")
    title = models.CharField(max_length=250)
    checklist = models.TextField(blank=True)                   # one item per line
    member = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    due_in_days = models.PositiveSmallIntegerField(null=True, blank=True)
    label = models.ForeignKey(Label, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    is_enabled = models.BooleanField(default=True)
    next_run_at = models.DateTimeField()
    last_run_at = models.DateTimeField(null=True, blank=True)
    run_count = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["next_run_at"]


def attachment_path(instance, filename):
    import uuid
    ext = filename.rsplit(".", 1)[-1].lower()[:10] if "." in filename else "bin"
    return f"attachments/{instance.card.list.board_id}/{uuid.uuid4().hex}.{ext}"


class Attachment(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_path, max_length=255)
    name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_image(self):
        return self.content_type in ("image/jpeg", "image/png", "image/gif", "image/webp")

    @property
    def size_label(self):
        n = self.size
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024


class Notification(models.Model):
    """Something a person should know about: assigned, mentioned, a comment on
    their card, or a due date coming up."""

    ASSIGNED, ITEM_ASSIGNED, MENTIONED, COMMENTED, DUE_SOON, ITEM_DUE_SOON, ADDED_TO_BOARD, WATCHED, AUTOMATION = (
        "assigned", "item_assigned", "mentioned", "commented", "due_soon", "item_due_soon", "added_to_board",
        "watched", "automation")
    KINDS = [(ASSIGNED, "Assigned to a card"), (ITEM_ASSIGNED, "Assigned a checklist item"),
             (MENTIONED, "Mentioned"), (COMMENTED, "Comment on your card"), (DUE_SOON, "Card due soon"),
             (ITEM_DUE_SOON, "Checklist item due soon"), (ADDED_TO_BOARD, "Added to a board"),
             (WATCHED, "Something you watch changed"), (AUTOMATION, "From a board rule")]

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=20, choices=KINDS)
    board = models.ForeignKey(Board, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    card = models.ForeignKey(Card, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    text = models.CharField(max_length=300)          # e.g. "assigned you to Order cake tins"
    excerpt = models.CharField(max_length=300, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["recipient", "read_at", "-created_at"])]


class InboxItem(models.Model):
    """A private to-do for one person, until it's scheduled or turned into a card."""

    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="inbox_items")
    text = models.CharField(max_length=250)
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True)
    is_done = models.BooleanField(default=False)
    position = models.FloatField(default=1000)
    source = models.CharField(max_length=20, blank=True)   # "typed", "list", "ai"
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["is_done", "position", "id"]

    def __str__(self):
        return self.text

    @property
    def due_state(self):
        if not self.due_date or self.is_done:
            return ""
        today = timezone.localdate()
        if self.due_date < today:
            return "overdue"
        return "soon" if self.due_date == today else ""


class CardMirror(models.Model):
    """The same card shown in another list — often on another department's
    board. It's one card: edits, ticks and comments show everywhere."""

    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="mirrors")
    list = models.ForeignKey(List, on_delete=models.CASCADE, related_name="mirrors")
    position = models.FloatField(default=1000)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [models.UniqueConstraint(fields=["card", "list"], name="uniq_card_mirror")]

    @property
    def token(self):
        return f"m{self.pk}"


class SavedSearch(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="saved_searches")
    name = models.CharField(max_length=60)
    query = models.CharField(max_length=300)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]


class Watch(models.Model):
    """Follow a card or a whole board you aren't assigned to and hear about changes."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="watches")
    board = models.ForeignKey(Board, on_delete=models.CASCADE, null=True, blank=True, related_name="watches")
    card = models.ForeignKey(Card, on_delete=models.CASCADE, null=True, blank=True, related_name="watches")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "board"], condition=models.Q(card__isnull=True),
                                    name="uniq_watch_board"),
            models.UniqueConstraint(fields=["user", "card"], condition=models.Q(card__isnull=False),
                                    name="uniq_watch_card"),
        ]


class Button(models.Model):
    """One click runs several actions: on the open card, or across the board."""

    CARD, BOARD = "card", "board"
    KINDS = [(CARD, "Card button"), (BOARD, "Board button")]
    BOARD_ACTIONS = [
        ("create_card", "create card … in list…"),
        ("move_all", "move every card from list… to list…"),
        ("archive_done", "archive every complete card"),
        ("sort_due", "sort list… by due date"),
    ]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="buttons")
    kind = models.CharField(max_length=10, choices=KINDS, default=CARD)
    name = models.CharField(max_length=40)
    icon = models.CharField(max_length=8, blank=True)
    actions = models.JSONField(default=list)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    run_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return self.name


REACTIONS = ["👍", "❤️", "😂", "🎉", "👀", "✅", "🙏"]


class Reaction(models.Model):
    comment = models.ForeignKey(Comment, on_delete=models.CASCADE, related_name="reactions")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    emoji = models.CharField(max_length=8)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["comment", "user", "emoji"], name="uniq_reaction")]


class Vote(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="votes")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["card", "user"], name="uniq_vote")]


class AuditEvent(models.Model):
    """Company-level log of sign-ins and admin changes (people, roles, sharing,
    board settings). Board-level card activity lives in Activity."""

    VERBS = {
        "login": "signed in",
        "login_failed": "failed to sign in as {email}",
        "dept_created": "created the department {dept}",
        "dept_member_added": "added {name} to {dept}",
        "dept_account_created": "created an account for {name} in {dept}",
        "dept_member_removed": "removed {name} from {dept}",
        "dept_member_left": "left {dept}",
        "dept_role_changed": "made {name} {role} of {dept}",
        "password_reset": "reset the password for {name}",
        "board_shared": "added {name} to the board {board} as {role}",
        "guest_invited": "invited the guest {name} to the board {board}",
        "board_member_removed": "removed {name} from the board {board}",
        "board_role_changed": "made {name} {role} on the board {board}",
        "board_visibility": "changed who can see {board} to “{value}”",
        "board_public_on": "turned on the public link for {board}",
        "board_public_off": "turned off the public link for {board}",
        "board_archived": "archived the board {board}",
        "board_restored": "restored the board {board}",
        "account_off": "switched off the account of {name}",
        "people_imported": "added {count} department memberships from a spreadsheet ({new} new accounts)",
        "account_on": "switched the account of {name} back on",
        "two_step_on": "turned on two-step sign-in",
        "two_step_off": "turned off two-step sign-in",
        "two_step_failed": "entered a wrong two-step code",
        "two_step_reset": "reset two-step sign-in for {name}",
        "settings_changed": "changed company settings: {changes}",
        "board_created": "created the board {board}",
    }

    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    verb = models.CharField(max_length=40)
    workspace = models.ForeignKey(Workspace, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    board = models.ForeignKey(Board, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    target = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    data = models.JSONField(default=dict, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def text(self):
        try:
            return self.VERBS.get(self.verb, self.verb.replace("_", " ")).format_map(_Blank(self.data))
        except (ValueError, IndexError):
            return self.verb.replace("_", " ")


class _Blank(dict):
    def __missing__(self, key):
        return "?"


class CompanySettings(models.Model):
    """One row (pk=1): company-wide rules set by company admins on Company → Settings."""

    BOARD_ADMINS, DEPT_ADMINS, COMPANY_ADMINS, NOBODY, EVERYONE = (
        "board_admins", "dept_admins", "company_admins", "nobody", "everyone")
    PUBLIC_CHOICES = [(BOARD_ADMINS, "Board admins"), (DEPT_ADMINS, "Department admins"),
                      (COMPANY_ADMINS, "Company admins only"), (NOBODY, "Nobody — public links are switched off")]
    GUEST_CHOICES = [(BOARD_ADMINS, "Board admins"), (DEPT_ADMINS, "Department admins"),
                     (COMPANY_ADMINS, "Company admins only"), (NOBODY, "Nobody — no new guests")]
    CREATE_CHOICES = [(EVERYONE, "Everyone in the department"), (DEPT_ADMINS, "Department admins"),
                      (COMPANY_ADMINS, "Company admins only")]
    PRIVATE_CHOICES = [(BOARD_ADMINS, "Board admins"), (DEPT_ADMINS, "Department admins"),
                       (COMPANY_ADMINS, "Company admins only")]
    TWO_STEP_CHOICES = [("off", "Optional — people can turn it on themselves"),
                        ("admins", "Required for company and department admins"),
                        ("staff", "Required for everyone except guests"),
                        ("everyone", "Required for everyone, guests included")]

    public_links = models.CharField(max_length=20, choices=PUBLIC_CHOICES, default=BOARD_ADMINS)
    invite_guests = models.CharField(max_length=20, choices=GUEST_CHOICES, default=BOARD_ADMINS)
    create_boards = models.CharField(max_length=20, choices=CREATE_CHOICES, default=EVERYONE)
    private_boards = models.CharField(max_length=20, choices=PRIVATE_CHOICES, default=BOARD_ADMINS)
    require_two_step = models.CharField(max_length=10, choices=TWO_STEP_CHOICES, default="off")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
