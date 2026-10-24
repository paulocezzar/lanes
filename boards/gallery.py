"""Ready-made board templates for the gallery. Kept general so any team can use them.

Each template has lists of cards. Cards marked "tip" explain how to use a list; people can leave
them out when they make a board ("Keep tips"). Example cards can be left out too ("Keep example cards").
Templates can also bring labels, custom fields, rules and a request form.
"""
from django.db import transaction

from . import positions
from .models import (Board, BoardMember, Card, Checklist, ChecklistItem, CustomField, Label, List,
                     RequestForm, Rule)

CATEGORIES = [
    ("projects", "Projects"),
    ("teams", "Teams & meetings"),
    ("requests", "Requests & support"),
    ("events", "Events"),
    ("people", "People & onboarding"),
    ("operations", "Planning & operations"),
    ("personal", "Personal"),
]


def T(title, desc="", items=(), labels=(), tip=False, due_days=None):
    return {"title": title, "desc": desc, "items": list(items), "labels": list(labels), "tip": tip, "due_days": due_days}


TEMPLATES = [
    {
        "key": "project", "title": "Project plan", "category": "projects", "bg": "dusk",
        "summary": "Plan a project from first ideas to done, with clear owners and dates.",
        "about": ["A simple structure for any project, big or small: keep reference material in one place, "
                  "agree what's next, and see at a glance what's stuck.",
                  "Comes with a rule that marks cards complete when they reach Done, and switches on the "
                  "Gantt and Portfolio views."],
        "labels": [("Urgent", "red"), ("Waiting on someone", "orange"), ("Quick win", "green")],
        "project": True,
        "rules": [{"trigger": "card_moved", "list": "Done", "actions": [{"type": "complete"}]}],
        "lists": [
            ("Resources", [T("💡 How to use this list", "Keep links, documents and decisions here so nobody has to "
                             "hunt for them. Pin the most important card to the top.", tip=True),
                           T("Project brief", "What we're doing, why, and what success looks like."),
                           T("Who's involved", "Names, roles and who signs off what.")]),
            ("To do", [T("💡 How to use this list", "Everything agreed but not started. Give each card a person "
                         "and a due date so it shows in their Planner.", tip=True),
                       T("Agree the budget", items=["Draft figures", "Review with finance", "Sign off"], due_days=7),
                       T("Book the kick-off meeting", due_days=3)]),
            ("Doing", [T("💡 How to use this list", "What's being worked on right now. Keep it short — if "
                         "it's getting long, things are stuck.", tip=True),
                       T("Write the plan", items=["Milestones", "Risks", "Dates"], labels=["Quick win"])]),
            ("Waiting", [T("💡 How to use this list", "Things that can't move until someone else acts. Use "
                           "“Waits for” on the card to show what it depends on.", tip=True),
                         T("Supplier quote", labels=["Waiting on someone"])]),
            ("Done", [T("💡 How to use this list", "Cards moved here are marked complete automatically.", tip=True)]),
        ],
    },
    {
        "key": "weekly", "title": "Team weekly board", "category": "teams", "bg": "ocean",
        "summary": "One board for the team's week: what's on, who's doing it, what's done.",
        "about": ["Run the week on one board. Each person's cards show in their own Planner, and the "
                  "Swimlanes view shows the week by person.",
                  "A scheduled automation tidies Done every Monday morning."],
        "labels": [("Priority", "red"), ("Routine", "blue")],
        "schedule_archive_done": True,
        "lists": [
            ("This week", [T("💡 How to use this list", "Agree these on Monday. Put a person on every card.", tip=True),
                           T("Weekly team catch-up", labels=["Routine"]),
                           T("Update the rota", labels=["Routine"])]),
            ("Today", [T("💡 How to use this list", "Pull cards in each morning. Keep it realistic.", tip=True)]),
            ("Doing", []),
            ("Done", [T("💡 How to use this list", "Cleared automatically every Monday — nothing is lost; "
                        "find old cards in Menu → Archived items.", tip=True)]),
            ("Next week", [T("Plan the month ahead")]),
        ],
    },
    {
        "key": "meetings", "title": "Meeting agenda & actions", "category": "teams", "bg": "slate",
        "summary": "Collect agenda items before the meeting and track the actions after it.",
        "about": ["Anyone can drop a topic in For the next meeting. In the meeting, work left to right; "
                  "anything agreed becomes an action with an owner and a date."],
        "labels": [("Decision needed", "purple"), ("For information", "sky")],
        "lists": [
            ("For the next meeting", [T("💡 How to use this list", "Add topics as they come up so you don't "
                                       "forget them. Add the “Decision needed” label if you need a yes or no.",
                                       tip=True),
                                     T("Holiday cover over the summer", labels=["Decision needed"])]),
            ("Discussed", []),
            ("Actions", [T("💡 How to use this list", "Each action needs one person and a due date. "
                           "Tick it off when it's done.", tip=True),
                         T("Send round the notes", due_days=1)]),
            ("Done", []),
        ],
    },
    {
        "key": "requests", "title": "Requests & helpdesk", "category": "requests", "bg": "forest",
        "summary": "Take requests from anyone through a form, then triage, work and close them.",
        "about": ["People send requests through a form link — they don't need to be on the board. "
                  "Each request lands in New as a card, and you're notified.",
                  "Triage, put someone on it, and move it along. Cards moved to Done are marked complete "
                  "automatically."],
        "labels": [("Urgent", "red"), ("Small job", "green"), ("Needs parts", "orange")],
        "fields": [("Where", "text", []), ("Priority", "dropdown", ["High", "Normal", "Low"])],
        "rules": [{"trigger": "card_moved", "list": "Done", "actions": [{"type": "complete"}]}],
        "form": {"title": "Make a request", "list": "New", "intro": "Tell us what you need and we'll pick it up.",
                 "questions": [{"label": "What do you need?", "kind": "short", "to": "title", "required": True},
                               {"label": "Details", "kind": "long", "to": "description", "required": False},
                               {"label": "Where?", "kind": "short", "to": "field:Where", "required": False},
                               {"label": "How urgent?", "kind": "choice", "to": "field:Priority", "required": True,
                                "options": ["High", "Normal", "Low"]},
                               {"label": "Needed by", "kind": "date", "to": "due", "required": False}]},
        "lists": [
            ("New", [T("💡 How to use this list", "Requests from the form arrive here. Check each one, set "
                       "its priority and move it on.", tip=True)]),
            ("Assigned", [T("💡 How to use this list", "Put one person on each card.", tip=True)]),
            ("In progress", []),
            ("Waiting", [T("💡 How to use this list", "Waiting for parts, access or an answer. Add a comment "
                           "saying what you're waiting for.", tip=True)]),
            ("Done", []),
        ],
    },
    {
        "key": "event", "title": "Event planning", "category": "events", "bg": "ember",
        "summary": "From first idea to the day itself — venue, guests, suppliers and the run-sheet.",
        "about": ["Organise an event of any size. Cards for each area of work, with checklists, a budget "
                  "and a run-sheet for the day."],
        "labels": [("Booked", "green"), ("Needs paying", "orange"), ("On the day", "purple")],
        "project": True,
        "lists": [
            ("Ideas", [T("💡 How to use this list", "Anything goes. Move ideas to Planning once agreed.", tip=True),
                       T("Theme and look")]),
            ("Planning", [T("Venue", items=["Shortlist", "Visit", "Book", "Pay deposit"], labels=["Needs paying"]),
                          T("Guest list", items=["Draft list", "Send invitations", "Chase replies", "Final numbers"]),
                          T("Food and drink", items=["Menu", "Dietary needs", "Confirm numbers"])]),
            ("Booked", [T("💡 How to use this list", "Confirmed and paid. Add the booking reference to the "
                          "card description.", tip=True)]),
            ("On the day", [T("Run-sheet", "Times, who does what, and phone numbers.", labels=["On the day"])]),
            ("Done", []),
        ],
    },
    {
        "key": "onboarding", "title": "New starter onboarding", "category": "people", "bg": "wheat",
        "summary": "Give each new starter the same good first weeks, with nothing forgotten.",
        "about": ["Make a copy of the New starter card for each person joining. It has a checklist for "
                  "before they start, their first day, first week and first month.",
                  "Ask for a sign-off at the end so their manager confirms they're settled in."],
        "labels": [("Starting soon", "sky"), ("First week", "green")],
        "lists": [
            ("Starting soon", [T("💡 How to use this list", "Copy the New starter card for each person and put "
                                 "their name and start date on it.", tip=True),
                               T("New starter — copy me",
                                 items=["Contract and paperwork signed", "Equipment and uniform ready",
                                        "Accounts and log-ins set up", "Buddy chosen", "Welcome message sent"],
                                 labels=["Starting soon"])]),
            ("First day", [T("First day checklist", items=["Welcome and tour", "Health and safety", "Meet the team",
                                                           "Lunch with buddy"])]),
            ("First weeks", [T("Training plan", items=["Core training", "Shadowing", "First review"])]),
            ("Settled in", [T("💡 How to use this list", "Ask the manager for a sign-off before moving a "
                              "card here.", tip=True)]),
        ],
    },
    {
        "key": "content", "title": "Content calendar", "category": "operations", "bg": "dusk",
        "summary": "Plan posts, newsletters and announcements, and see them on the Calendar.",
        "about": ["Each card is one piece of content with a publish date. Use the Calendar view to see the "
                  "month, and ask for a sign-off before anything goes out."],
        "labels": [("Social", "blue"), ("Newsletter", "purple"), ("Website", "green")],
        "lists": [
            ("Ideas", [T("💡 How to use this list", "Jot ideas down as cards. Give them a date when they're "
                         "agreed.", tip=True)]),
            ("Writing", [T("Monthly newsletter", items=["Draft", "Photos", "Proofread"], labels=["Newsletter"],
                           due_days=10)]),
            ("For approval", [T("💡 How to use this list", "Ask for a sign-off on the card. It can't be "
                                "marked done until it's approved.", tip=True)]),
            ("Scheduled", []),
            ("Published", []),
        ],
    },
    {
        "key": "stock", "title": "Ordering & suppliers", "category": "operations", "bg": "slate",
        "summary": "Track what needs ordering, what's on its way and what's arrived.",
        "about": ["One card per order, with the supplier and amount as fields. Move it along as it's "
                  "ordered and delivered. Swimlanes by label shows each supplier's orders."],
        "labels": [("Urgent", "red"), ("Regular order", "blue")],
        "fields": [("Supplier", "text", []), ("Order total", "number", [])],
        "lists": [
            ("To order", [T("💡 How to use this list", "Add a card for anything running low. Use a checklist "
                            "for the items and amounts.", tip=True),
                          T("Weekly order", items=["Check stock", "Place order", "Confirm delivery day"],
                            labels=["Regular order"])]),
            ("Ordered", []),
            ("Delivered — to check", [T("💡 How to use this list", "Check the delivery against the order "
                                        "before moving it on.", tip=True)]),
            ("Done", []),
        ],
    },
    {
        "key": "improvements", "title": "Ideas & improvements", "category": "teams", "bg": "forest",
        "summary": "Collect ideas from everyone, vote on them, and track the ones you do.",
        "about": ["Anyone can add an idea. Voting is switched on, so the most popular rise to the top. "
                  "Agreed ideas become actions with an owner."],
        "voting": True,
        "labels": [("Saves time", "green"), ("Saves money", "yellow"), ("Safety", "red")],
        "lists": [
            ("Ideas", [T("💡 How to use this list", "Add your idea as a card and vote 👍 for the ones you like.",
                         tip=True)]),
            ("Agreed", []),
            ("Trying it", []),
            ("Working well", []),
            ("Not now", [T("💡 How to use this list", "Good ideas that don't fit right now. Look again "
                           "each quarter.", tip=True)]),
        ],
    },
    {
        "key": "personal", "title": "My tasks", "category": "personal", "bg": "ocean",
        "summary": "A private board for your own to-dos.",
        "about": ["Private to you. Capture everything in Inbox, choose what you'll do today, and clear Done "
                  "when you like."],
        "private": True,
        "lists": [
            ("Inbox", [T("💡 How to use this list", "Get things out of your head — sort them later.", tip=True)]),
            ("Today", []),
            ("This week", []),
            ("Later", []),
            ("Done", []),
        ],
    },
]

BY_KEY = {t["key"]: t for t in TEMPLATES}


def preview(t, keep_tips=True):
    """Lists and card titles, for the preview on the template page."""
    return [{"title": name, "cards": [c for c in cards if keep_tips or not c["tip"]]} for name, cards in t["lists"]]


@transaction.atomic
def build(t, *, workspace, user, title, visibility, keep_cards=True, keep_tips=True):
    from django.utils import timezone
    from datetime import timedelta
    board = Board.objects.create(workspace=workspace, title=title[:120], created_by=user, background=t["bg"],
                                 visibility=Board.PRIVATE if t.get("private") else visibility,
                                 is_project=bool(t.get("project")), voting=bool(t.get("voting")),
                                 description=t["summary"])
    BoardMember.objects.create(board=board, user=user, role=BoardMember.ADMIN)
    labels = {name: Label.objects.create(board=board, name=name, colour=colour) for name, colour in t.get("labels", [])}
    fields = {}
    for i, (name, kind, options) in enumerate(t.get("fields", []), start=1):
        fields[name] = CustomField.objects.create(board=board, name=name, kind=kind, options=options, position=i)
    lists = {}
    for i, (name, cards) in enumerate(t["lists"], start=1):
        lst = List.objects.create(board=board, title=name, position=i * positions.GAP)
        lists[name] = lst
        n = 0
        for c in cards:
            if (c["tip"] and not keep_tips) or (not c["tip"] and not keep_cards):
                continue
            n += 1
            card = Card.objects.create(list=lst, title=c["title"], description=c["desc"], created_by=user,
                                       position=n * positions.GAP,
                                       due_at=(timezone.now() + timedelta(days=c["due_days"])).replace(
                                           hour=17, minute=0, second=0, microsecond=0) if c["due_days"] else None)
            card.labels.set([labels[x] for x in c["labels"] if x in labels])
            if c["items"]:
                cl = Checklist.objects.create(card=card, title="Checklist")
                ChecklistItem.objects.bulk_create([ChecklistItem(checklist=cl, text=x, position=j * positions.GAP)
                                                   for j, x in enumerate(c["items"], start=1)])
    for r in t.get("rules", []):
        lst = lists.get(r.get("list"))
        Rule.objects.create(board=board, trigger=r["trigger"], trigger_list=lst, actions=r["actions"], created_by=user)
    if t.get("schedule_archive_done"):
        from .automation import next_run
        from .models import Schedule
        s = Schedule(board=board, kind="actions", actions=[{"type": "archive_done"}], frequency="weekly", weekday=0,
                     hour=6, created_by=user, next_run_at=timezone.now())
        s.next_run_at = next_run(s, timezone.now())
        s.save()
    if t.get("form"):
        import secrets
        f = t["form"]
        qs = []
        for q in f["questions"]:
            to = q["to"]
            if to.startswith("field:"):
                fld = fields.get(to[6:])
                to = f"field:{fld.pk}" if fld else "description"
            qs.append({"label": q["label"], "kind": q["kind"], "to": to, "required": q.get("required", False),
                       "options": q.get("options", []), "help": ""})
        RequestForm.objects.create(board=board, list=lists[f["list"]], title=f["title"], intro=f.get("intro", ""),
                                   questions=qs, token=secrets.token_urlsafe(12), created_by=user)
    from .services import log
    log(board, user, "board_created")
    from .shared_rules import apply_to_new_board
    apply_to_new_board(board)
    return board
