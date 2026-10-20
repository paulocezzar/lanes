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


class Board(models.Model):
    PRIVATE, WORKSPACE = "private", "workspace"
    VISIBILITY = [(PRIVATE, "Private — board members only"), (WORKSPACE, "Workspace — everyone in the department can see")]

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

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.title


class Card(models.Model):
    list = models.ForeignKey(List, on_delete=models.CASCADE, related_name="cards")
    title = models.CharField(max_length=250)
    description = models.TextField(blank=True)
    position = models.FloatField(default=1000)
    start_date = models.DateField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    is_complete = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
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

    def tile_fields(self):
        """Custom field values to show as badges on the card tile."""
        vals = [v for v in self.field_values.all() if v.field.show_on_card and v.display()]
        return sorted(vals, key=lambda v: (v.field.position, v.field_id))

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

    TEXT, NUMBER, DATE, CHECKBOX, DROPDOWN = "text", "number", "date", "checkbox", "dropdown"
    KINDS = [(TEXT, "Text"), (NUMBER, "Number"), (DATE, "Date"), (CHECKBOX, "Checkbox"), (DROPDOWN, "Dropdown")]

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
        return str(raw).strip()[:500] or None


class CardFieldValue(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="field_values")
    field = models.ForeignKey(CustomField, on_delete=models.CASCADE, related_name="values")
    value = models.JSONField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["card", "field"], name="uniq_card_field")]

    def display(self):
        f, v = self.field, self.value
        if f.kind == CustomField.CHECKBOX:
            return "✓" if v else ""
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
        }.get(self.verb, self.verb.replace("_", " "))


class Rule(models.Model):
    """Board automation: when <trigger> happens to a card, run <actions> on it."""

    ADDED, MOVED, COMPLETED, CHECKLIST_DONE, LABEL_ADDED = (
        "card_added", "card_moved", "card_completed", "checklist_done", "label_added")
    TRIGGERS = [
        (ADDED, "a card is added to list…"),
        (MOVED, "a card is moved into list…"),
        (COMPLETED, "a card is marked complete"),
        (CHECKLIST_DONE, "all checklist items on a card are done"),
        (LABEL_ADDED, "label … is added to a card"),
    ]
    ACTIONS = [
        ("move", "move it to list…"),
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
        ("archive", "archive it"),
    ]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="rules")
    trigger = models.CharField(max_length=20, choices=TRIGGERS)
    trigger_list = models.ForeignKey(List, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    trigger_label = models.ForeignKey(Label, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    actions = models.JSONField(default=list)   # [{"type": "move", "list": 3}, {"type": "complete"}]
    is_enabled = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    run_count = models.PositiveIntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]


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
