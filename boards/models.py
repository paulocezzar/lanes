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
    """One per department."""

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
    """A named group of boards inside a department, e.g. "2026 projects"."""

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
    COMMENTS = [("disabled", "Disabled — nobody can comment or react"),
                ("members", "Members — admins and board members"),
                ("observers", "Members and observers"),
                ("workspace", "Anyone in the department who can see the board")]
    comment_permission = models.CharField(max_length=10, choices=COMMENTS, default="observers")
    MEMBER_PERMS = [("admins", "Admins only"), ("members", "Admins and members")]
    member_permission = models.CharField(max_length=10, choices=MEMBER_PERMS, default="admins")
    show_complete = models.BooleanField(default=True)    # ✓ complete status on card fronts
    covers_enabled = models.BooleanField(default=True)   # show image/colour covers on card fronts
    # project management
    RAG = [("auto", "Automatic"), ("green", "On track"), ("amber", "At risk"), ("red", "Off track")]
    is_project = models.BooleanField(default=False)      # shows on the Portfolio page
    rag = models.CharField(max_length=6, choices=RAG, default="auto")
    status_note = models.CharField(max_length=300, blank=True)
    status_updated_at = models.DateTimeField(null=True, blank=True)
    project_owner = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

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
    is_pinned = models.BooleanField(default=False)   # stays on the left while the board scrolls

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
    RECUR = [("", "Never"), ("daily", "Daily"), ("weekdays", "Every weekday (Mon–Fri)"), ("weekly", "Weekly"),
             ("monthly", "Monthly"), ("yearly", "Yearly")]
    recur = models.CharField(max_length=10, choices=RECUR, blank=True)   # completing it makes the next one
    REMINDERS = [(-1, "None"), (0, "At time of due date"), (5, "5 minutes before"), (10, "10 minutes before"),
                 (15, "15 minutes before"), (60, "1 hour before"), (120, "2 hours before"), (1440, "1 day before"),
                 (2880, "2 days before")]
    reminder_minutes = models.SmallIntegerField(default=1440)
    is_complete = models.BooleanField(default=False)
    is_archived = models.BooleanField(default=False)
    is_template = models.BooleanField(default=False)
    is_milestone = models.BooleanField(default=False)      # a diamond on the Gantt, no duration
    estimate_hours = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    cover = models.ForeignKey("Attachment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cover_colour = models.CharField(max_length=10, blank=True)      # colour cover when there's no image
    cover_full = models.BooleanField(default=False)    # cover fills the whole card front, title on top
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

    def progress(self):
        """(made, target, percent, unit) from the Target / Made so far fields, for a progress bar."""
        from .production import progress
        p = progress(self)
        if not p:
            return None
        unit = next((str(v.value) for v in self.field_values.all() if v.field.name.strip().lower() == "unit"), "")
        return {"made": f"{p[0]:,}", "target": f"{p[1]:,}", "pct": p[2], "unit": unit, "done": p[0] >= p[1]}

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
    reminder_minutes = models.SmallIntegerField(default=1440)   # same choices as Card.REMINDERS; -1 = none

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
            "card_renamed": (f"renamed this card from “{d.get('from', '')}” to “{d['to']}”" if d.get("to")
                             else f"renamed this card from “{d.get('from', '')}”"),
            "label_added": f"added the {d.get('name', '')} label",
            "label_removed": f"removed the {d.get('name', '')} label",
            "item_added": f"added “{d.get('text', '')}” to {d.get('checklist', 'the checklist')}",
            "item_deleted": f"deleted “{d.get('text', '')}” from {d.get('checklist', 'the checklist')}",
            "item_assigned": f"assigned “{d.get('text', '')}” to {d.get('name', '')}",
            "item_unassigned": f"unassigned “{d.get('text', '')}”",
            "item_due": f"set “{d.get('text', '')}” due {d.get('due', '')}",
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
            "attachment_renamed": f"renamed an attachment to {d.get('name', '?')}",
            "dependency_added": f"made this card wait for {d.get('name', 'another card')}",
            "dependency_removed": "removed a dependency from this card",
            "report_sent": f"sent the weekly status report to {d.get('to', 0)} people",
            "time_logged": f"logged {d.get('time', 'time')} on this card",
            "cost_added": f"added a cost: {d.get('name', '')} ({d.get('amount', '')})",
            "status_updated": f"updated the project status: {d.get('rag', '')}{(' — ' + d['note']) if d.get('note') else ''}",
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
        (OVERDUE, "a card's due date passes…"),
        ("member_added", "someone is added to a card"),
        ("board_added", "a card is added to the board"),
        ("card_archived", "a card is archived"),
        ("due_set", "a due date is set on a card"),
        ("due_removed", "a card's due date is removed"),
        ("attachment_added", "an attachment is added to a card"),
        ("comment_added", "a comment is posted to a card"),
        ("mentioned", "someone is mentioned in a comment"),
        ("field_set", "custom field … is set on a card"),
        ("list_count", "list … has exactly … cards"),
        ("list_created", "a list is created"),
        ("due_weekday", "on the … before a card is due"),
    ]
    MEMBER_ADDED = "member_added"
    BOARD_ADDED, ARCHIVED, DUE_SET, DUE_REMOVED = "board_added", "card_archived", "due_set", "due_removed"
    ATTACHMENT, COMMENT, MENTIONED, FIELD_SET = "attachment_added", "comment_added", "mentioned", "field_set"
    LIST_COUNT, LIST_CREATED, DUE_WEEKDAY = "list_count", "list_created", "due_weekday"
    FILTERS = ["label", "no_label", "has_due", "no_due", "member", "complete", "incomplete", "in_list"]
    BY_WHOM = [("anyone", "by anyone"), ("me", "by me"), ("others", "by anyone except me")]
    TIME_TRIGGERS = (DUE_SOON, OVERDUE, "due_weekday")
    AFTER = [(0, "the moment it's due"), (1, "1 hour after"), (3, "3 hours after"), (24, "1 day after"),
             (72, "3 days after"), (168, "1 week after")]
    HOURS = [(1, "1 hour"), (2, "2 hours"), (3, "3 hours"), (12, "12 hours"), (24, "1 day"), (48, "2 days"), (72, "3 days"),
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
        ("add_checklist", "add checklist…"),
        ("set_due_workdays", "set due date to … working days from now"),
        ("add_sticker", "add sticker…"),
        ("archive", "archive it"),
        ("move_bottom", "move it to the bottom of its list"),
        ("copy", "copy it to list…"),
        ("remove_member", "remove member…"),
        ("set_field", "set custom field…"),
        ("sort_list", "sort its list by…"),
        ("cascade", "turn its checklist items into cards in list…"),
        ("rename", "rename it to…"),
        ("set_description", "set its description to…"),
        ("set_start", "set start date to … days from now"),
    ]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="rules")
    trigger = models.CharField(max_length=20, choices=TRIGGERS)
    trigger_list = models.ForeignKey(List, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    trigger_label = models.ForeignKey(Label, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    trigger_hours = models.PositiveSmallIntegerField(null=True, blank=True)   # for "due within…"
    by_whom = models.CharField(max_length=10, choices=BY_WHOM, default="anyone")   # whose action triggers it
    trigger_me = models.BooleanField(default=False)    # member_added / mentioned: only the rule's creator
    trigger_count = models.PositiveSmallIntegerField(null=True, blank=True)    # list_count
    trigger_weekday = models.PositiveSmallIntegerField(null=True, blank=True)  # due_weekday: 0 = Monday
    trigger_field = models.ForeignKey("CustomField", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    filters = models.JSONField(default=list, blank=True)   # [{"type": "label", "label": 3}, {"type": "has_due"}]
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
    MONTHLY_NTH, YEARLY = "month_nth", "yearly"
    FREQUENCIES = [(DAILY, "Every day"), (WEEKDAYS, "Every weekday (Mon–Fri)"),
                   (WEEKLY, "Every week on…"), (MONTHLY, "Every month on day…"),
                   (MONTHLY_NTH, "Every month on the…"), (YEARLY, "Every year on…")]
    NTH = [(1, "first"), (2, "second"), (3, "third"), (4, "fourth"), (5, "last")]
    MONTHS = [(i, n) for i, n in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                             "August", "September", "October", "November", "December"], start=1)]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="schedules")
    CARD, ACTIONS = "card", "actions"
    kind = models.CharField(max_length=10, choices=[(CARD, "Create a card"), (ACTIONS, "Run board actions")],
                            default=CARD)
    actions = models.JSONField(default=list, blank=True)       # for kind=actions: same as board buttons
    frequency = models.CharField(max_length=10, choices=FREQUENCIES, default=WEEKLY)
    weekday = models.PositiveSmallIntegerField(default=0)      # 0 = Monday, for weekly
    weekdays = models.CharField(max_length=20, blank=True)     # weekly on several days: "0,3"
    interval = models.PositiveSmallIntegerField(default=1)     # weekly: every N weeks
    anchor = models.DateField(null=True, blank=True)           # week the every-N-weeks count starts from
    month_day = models.PositiveSmallIntegerField(default=1)    # 1–28, for monthly
    hour = models.PositiveSmallIntegerField(default=7)         # local time
    nth = models.PositiveSmallIntegerField(default=1)          # month_nth: 1–4, or 5 = last
    month = models.PositiveSmallIntegerField(default=1)        # yearly: 1–12 (with month_day)
    target_list = models.ForeignKey(List, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    title = models.CharField(max_length=250, blank=True)
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
    file = models.FileField(upload_to=attachment_path, max_length=255, blank=True)
    url = models.URLField(max_length=1000, blank=True)     # a link instead of a file
    name = models.CharField(max_length=255)
    size = models.PositiveBigIntegerField(default=0)
    content_type = models.CharField(max_length=120, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def link_host(self):
        from urllib.parse import urlparse
        return urlparse(self.url).netloc.removeprefix("www.") if self.url else ""

    @property
    def is_image(self):
        if self.url:
            return False
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
             (WATCHED, "Something you watch changed"), (AUTOMATION, "From a board rule"),
             ("budget", "Project budget warning")]

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    kind = models.CharField(max_length=20, choices=KINDS)
    board = models.ForeignKey(Board, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    card = models.ForeignKey(Card, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    text = models.CharField(max_length=300)          # e.g. "assigned you to Send the report"
    excerpt = models.CharField(max_length=300, blank=True)
    dedupe_key = models.CharField(max_length=120, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)

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
        ("sort_field", "sort list… by custom field…"),
        ("archive_list", "archive every card in list…"),
        ("move_random", "move … random cards from list… to list…"),
        ("create_list", "create a list called…"),
        ("copy_named", "find card … and copy it to list…"),
        ("plan_days", "re-plan production days from list… into list…"),
    ]

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="buttons")
    kind = models.CharField(max_length=10, choices=KINDS, default=CARD)
    name = models.CharField(max_length=40)
    icon = models.CharField(max_length=8, blank=True)
    actions = models.JSONField(default=list)
    is_enabled = models.BooleanField(default=True)     # "Enabled": shown on cards / in the header
    close_card = models.BooleanField(default=False)    # card buttons: close the card after pressing
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
        "password_reset_requested": "asked for a password reset email for {email}",
        "ms_login": "signed in with Microsoft",
        "ms_account_created": "got a Lanes account from Microsoft sign-in",
        "invites_sent": "emailed {count} invites",
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
    MICROSOFT_CHOICES = [("off", "Off"),
                         ("allowed", "Allowed — staff can use Microsoft or a Lanes password"),
                         ("required", "Required for staff — Lanes passwords only for guests and company admins")]
    microsoft_login = models.CharField(max_length=10, choices=MICROSOFT_CHOICES, default="allowed")
    microsoft_auto_create = models.BooleanField(default=False)   # make accounts for anyone in the directory
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class OutgoingEmail(models.Model):
    """Emails waiting to go out. The cron job sends them, so a big batch
    (e.g. 300 invites) never holds up a web request."""

    to = models.EmailField()
    subject = models.CharField(max_length=200)
    text = models.TextField()
    html = models.TextField(blank=True)
    kind = models.CharField(max_length=20, blank=True)       # invite, notification, digest…
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["sent_at", "created_at"])]


class Dependency(models.Model):
    """`card` can't start until `depends_on` is finished (finish-to-start), plus `lag_days`."""
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="dependencies")
    depends_on = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="dependants")
    lag_days = models.SmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["card", "depends_on"], name="uniq_dependency")]


class TimeEntry(models.Model):
    """Time spent on a card. A running timer has started_at and no minutes yet."""
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="time_entries")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="time_entries")
    day = models.DateField()
    minutes = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)    # set while the timer runs
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-day", "-created_at"]


class CostItem(models.Model):
    """Money spent on a project board (labour comes from time entries × hourly rates)."""
    CATEGORIES = [("agency", "Agency staff"), ("materials", "Materials"), ("equipment", "Equipment"),
                  ("services", "Services"), ("other", "Other")]
    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="costs")
    card = models.ForeignKey(Card, on_delete=models.SET_NULL, null=True, blank=True, related_name="costs")
    description = models.CharField(max_length=200)
    category = models.CharField(max_length=12, choices=CATEGORIES, default="other")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    day = models.DateField()
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-day", "-created_at"]
