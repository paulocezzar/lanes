"""Board automation page: build rules and recurring-card schedules."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import automation, permissions
from .board_context import board_context
from .models import STICKERS, Board, BoardMember, Label, List, Rule, Schedule

MAX_ACTIONS = 4
NEEDS = {"move": "list", "add_label": "label", "remove_label": "label", "add_member": "user",
         "set_due": "days", "set_due_workdays": "days", "comment": "text", "add_sticker": "sticker",
         "notify": "message", "add_checklist": "checklist"}
SECTIONS = [("rules", "Rules"), ("due", "Due date"), ("scheduled", "Scheduled"),
            ("card-buttons", "Card buttons"), ("board-buttons", "Board buttons"), ("activity", "Activity")]

# Clickable examples. Lists/labels are matched by name on this board ("first"/"last" = position).
EXAMPLES = {
    "rules": [
        ("When a <b>card is added to list</b> “To do” <b>by me</b>, <b>add the “Steps” checklist</b>.",
         {"trigger": "card_added", "trigger_list": "first", "by_whom": "me",
          "actions": [{"type": "add_checklist", "title": "Steps"}]}),
        ("When a <b>card is moved to list</b> “Done” <b>by anyone</b>, <b>mark it complete</b> and "
         "<b>remove all members</b>.",
         {"trigger": "card_moved", "trigger_list": "done", "by_whom": "anyone",
          "actions": [{"type": "complete"}, {"type": "clear_members"}]}),
        ("<b>When I am added to a card</b>, <b>set the due date</b> in 5 working days and <b>post a comment</b> "
         "saying “I got this!”",
         {"trigger": "member_added", "member_who": "me",
          "actions": [{"type": "set_due_workdays", "days": 5}, {"type": "comment", "text": "I got this!"}]}),
        ("When <b>all checklists are complete</b>, <b>move the card</b> to “Done”.",
         {"trigger": "checklist_done", "actions": [{"type": "move", "list": "done"}]}),
    ],
    "due": [
        ("<b>The moment a card is due</b>, <b>move it to the top</b> of its list and <b>notify its members</b>.",
         {"trigger": "card_overdue", "trigger_hours": 0, "actions": [{"type": "move_top"}, {"type": "notify"}]}),
        ("2 hours <b>before a card is due</b>, <b>add the red label</b>.",
         {"trigger": "card_due_soon", "trigger_hours": 2, "actions": [{"type": "add_label", "label": "red"}]}),
        ("3 days <b>after a card is due</b>, <b>post a comment</b> “This card was due 3 days ago”.",
         {"trigger": "card_overdue", "trigger_hours": 72,
          "actions": [{"type": "comment", "text": "This card was due 3 days ago — can we catch up?"}]}),
        ("1 day <b>before a card is due</b>, <b>notify its members</b> and <b>add the ⏰ sticker</b>.",
         {"trigger": "card_due_soon", "trigger_hours": 24, "actions": [{"type": "notify"}, {"type": "add_sticker", "sticker": "⏰"}]}),
    ],
    "scheduled": [
        ("<b>Every weekday</b> at 06:00, <b>sort</b> the first list <b>by due date</b>.",
         {"kind": "actions", "frequency": "weekdays", "hour": 6, "actions": [{"type": "sort_due", "list": "first"}]}),
        ("<b>Every third Wednesday</b> of the month, <b>create a card</b> “Planning meeting” in the first list.",
         {"kind": "card", "frequency": "month_nth", "nth": 3, "weekday": 2, "hour": 8, "title": "Planning meeting",
          "target_list": "first"}),
        ("<b>Every year on 14 February</b>, <b>create a list</b> called “Valentine's Day ideas”.",
         {"kind": "actions", "frequency": "yearly", "month": 2, "month_day": 14, "hour": 7,
          "actions": [{"type": "create_list", "text": "Valentine's Day ideas"}]}),
        ("<b>Every Monday</b> at 07:00, <b>create a card</b> “Weekly check” with a checklist.",
         {"kind": "card", "frequency": "weekly", "weekday": 0, "hour": 7, "title": "Weekly check",
          "target_list": "first", "checklist": "Review open cards\nUpdate due dates\nArchive finished cards"}),
        ("<b>Every Friday</b> at 17:00, <b>move every card</b> from “Done” to the last list.",
         {"kind": "actions", "frequency": "weekly", "weekday": 4, "hour": 17,
          "actions": [{"type": "move_all", "from_list": "done", "list": "last"}]}),
    ],
    "card-buttons": [
        ("<b>Assign to me:</b> join the card, add the “In progress” label and an empty “To do” checklist.",
         {"name": "Assign to me", "icon": "👋", "actions": [{"type": "add_me"}, {"type": "add_label", "label": "progress"},
                                                            {"type": "add_checklist", "title": "To do"}]}),
        ("<b>Completed:</b> move the card to “Done” and remove its due date.",
         {"name": "Completed", "icon": "✅", "actions": [{"type": "move", "list": "done"}, {"type": "clear_due"}]}),
        ("<b>Send to review:</b> add the “For review” label and post “Ready for review!”.",
         {"name": "Send to review", "icon": "📌", "actions": [{"type": "add_label", "label": "review"},
                                                             {"type": "comment", "text": "Ready for review!"}]}),
        ("<b>Delivered:</b> move to “Done”, mark complete and post a comment.",
         {"name": "Delivered", "icon": "🚚", "actions": [{"type": "move", "list": "done"}, {"type": "complete"},
                                                         {"type": "comment", "text": "Delivered ✅"}]}),
    ],
    "board-buttons": [
        ("<b>Monday setup:</b> archive every card in “This week”, then move everything from “Next week” to “This week”.",
         {"name": "Monday setup", "icon": "🧹", "actions": [{"type": "archive_list", "list": "this week"},
                                                            {"type": "move_all", "from_list": "next week", "list": "this week"}]}),
        ("<b>Prioritise:</b> sort the first list by a custom field, high to low.",
         {"name": "Prioritise", "icon": "🔥", "actions": [{"type": "sort_field", "list": "first", "order": "desc"}]}),
        ("<b>Shuffle pick:</b> move 2 random cards from the first list to the second.",
         {"name": "Shuffle pick", "icon": "⭐", "actions": [{"type": "move_random", "count": 2, "from_list": "first", "list": "second"}]}),
        ("<b>Tidy up:</b> archive every complete card.",
         {"name": "Tidy up", "icon": "📦", "actions": [{"type": "archive_done"}]}),
    ],
}


@login_required
def automation_page(request, board_id):
    import json

    from django.utils.safestring import mark_safe
    board, ctx = board_context(request, board_id, "automation")
    section = request.GET.get("s") or ("rules" if request.GET.get("trigger") else "rules")
    if section not in dict(SECTIONS):
        section = "rules"
    rules, due_rules = [], []
    for r in board.rules.select_related("trigger_list", "trigger_label", "created_by"):
        when, then = automation.describe_rule(r)
        (due_rules if r.trigger in Rule.TIME_TRIGGERS else rules).append({"rule": r, "when": when, "then": then})
    schedules = [{"s": s, "when": automation.describe_schedule(s), "does": automation.describe_schedule_job(s)}
                 for s in board.schedules.select_related("target_list", "member", "label")]
    from .models import Activity, Button
    buttons = [{"b": b, "does": automation.describe_button_actions(b)} for b in board.buttons.all()]
    counts = {"rules": len(rules), "due": len(due_rules), "scheduled": len(schedules),
              "card-buttons": sum(1 for b in buttons if b["b"].kind == Button.CARD),
              "board-buttons": sum(1 for b in buttons if b["b"].kind == Button.BOARD)}
    ctx.update({
        "section": section,
        "sections": [(k, v, counts.get(k)) for k, v in SECTIONS],
        "examples": [(mark_safe(text), json.dumps(data)) for text, data in EXAMPLES.get(section, [])],
        "rules": rules,
        "due_rules": due_rules,
        "event_triggers": [t for t in Rule.TRIGGERS if t[0] not in Rule.TIME_TRIGGERS],
        "time_triggers": [t for t in Rule.TRIGGERS if t[0] in Rule.TIME_TRIGGERS],
        "by_whom": Rule.BY_WHOM,
        "auto_activity": (Activity.objects.filter(board=board, verb__in=["automation", "button"])
                          .select_related("actor", "card")[:60]) if section == "activity" else [],
        "open_form": bool(request.GET.get("trigger")) or request.GET.get("new") == "1",
        "card_buttons": [b for b in buttons if b["b"].kind == Button.CARD],
        "board_buttons": [b for b in buttons if b["b"].kind == Button.BOARD],
        "buttons": buttons,
        "board_actions": Button.BOARD_ACTIONS,
        "button_icons": ["", "✅", "🚚", "📦", "🔥", "⭐", "🧹", "📌", "⏰", "👋"],
        "schedules": schedules,
        "triggers": Rule.TRIGGERS,
        "trigger_hours": Rule.HOURS,
        "trigger_after": Rule.AFTER,
        "actions": Rule.ACTIONS,
        "action_slots": range(MAX_ACTIONS),
        "lists": board.lists.filter(is_archived=False),
        "frequencies": Schedule.FREQUENCIES,
        "weekdays": list(enumerate(automation.WEEKDAYS)),
        "hours": range(0, 24),
        "month_days": range(1, 32),
        "nths": Schedule.NTH,
        "months": Schedule.MONTHS,
        "stickers": STICKERS,
        "fields": board.custom_fields.all(),
    })
    return render(request, "boards/automation.html", ctx)


def _back(board, request, section=None):
    section = section or request.POST.get("section")
    url = reverse("board_automation", kwargs={"board_id": board.pk})
    return redirect(f"{url}?s={section}" if section in dict(SECTIONS) else url)


def _editable_board(request, board_id):
    board = get_object_or_404(Board, pk=board_id, is_archived=False)
    if not permissions.can_view(board, request.user):
        raise Http404
    if not permissions.can_edit(board, request.user):
        messages.error(request, "Only board admins and members can change automation.")
        return board, False
    return board, True


@login_required
@require_POST
def rule_create(request, board_id):
    board, ok = _editable_board(request, board_id)
    back = _back(board, request)
    if not ok:
        return back
    p = request.POST
    trigger = p.get("trigger")
    if trigger not in dict(Rule.TRIGGERS):
        messages.error(request, "Pick when the rule should run.")
        return back
    rule = Rule(board=board, trigger=trigger, created_by=request.user)
    if trigger not in Rule.TIME_TRIGGERS and p.get("by_whom") in dict(Rule.BY_WHOM):
        rule.by_whom = p["by_whom"]
    if trigger == Rule.MEMBER_ADDED:
        rule.trigger_me = p.get("member_who") == "me"
    if trigger in (Rule.ADDED, Rule.MOVED):
        rule.trigger_list = List.objects.filter(pk=p.get("trigger_list") or 0, board=board).first()
        if not rule.trigger_list:
            messages.error(request, "Pick which list triggers the rule.")
            return back
    if trigger == Rule.DUE_SOON:
        try:
            rule.trigger_hours = int(p.get("trigger_hours"))
        except (TypeError, ValueError):
            rule.trigger_hours = None
        if rule.trigger_hours not in dict(Rule.HOURS):
            messages.error(request, "Pick how soon before the due date the rule should run.")
            return back
    if trigger == Rule.OVERDUE:
        try:
            rule.trigger_hours = int(p.get("trigger_hours") or 0)
        except ValueError:
            rule.trigger_hours = 0
        if rule.trigger_hours not in dict(Rule.AFTER):
            rule.trigger_hours = 0
    if trigger == Rule.LABEL_ADDED:
        rule.trigger_label = Label.objects.filter(pk=p.get("trigger_label") or 0, board=board).first()
        if not rule.trigger_label:
            messages.error(request, "Pick which label triggers the rule.")
            return back

    actions, error = parse_actions(p, board)
    if error:
        messages.error(request, error)
        return back
    if not actions:
        messages.error(request, "Add at least one action.")
        return back
    if rule.trigger == Rule.MOVED and any(a["type"] == "move" and a.get("list") == rule.trigger_list_id
                                          for a in actions):
        messages.error(request, "A rule can't move cards into the same list that triggers it.")
        return back
    rule.actions = actions
    rule.save()
    messages.success(request, "Rule added.")
    return back


def parse_actions(p, board, prefix="", extra=()):
    """Read action_0..action_3 (+ their parameters) from a POST. Returns (actions, error)."""
    allowed = dict(Rule.ACTIONS) | dict(extra)
    actions = []
    for i in range(MAX_ACTIONS):
        t = p.get(f"{prefix}action_{i}")
        if not t or t not in allowed:
            continue
        a = {"type": t}
        need = NEEDS.get(t)
        if t == "move_all":
            src = List.objects.filter(pk=p.get(f"{prefix}from_list_{i}") or 0, board=board).first()
            if not src:
                return [], "Pick the list to move cards from."
            a["from_list"] = src.pk
            need = "list"
        if t == "move_random":
            src = List.objects.filter(pk=p.get(f"{prefix}from_list_{i}") or 0, board=board).first()
            if not src:
                return [], "Pick the list to move cards from."
            a["from_list"] = src.pk
            try:
                a["count"] = max(1, min(int(p.get(f"{prefix}count_{i}") or 1), 50))
            except ValueError:
                a["count"] = 1
        if t == "sort_field":
            from .models import CustomField
            f = CustomField.objects.filter(pk=p.get(f"{prefix}field_{i}") or 0, board=board).first()
            if not f:
                return [], "Pick the custom field to sort by (add one in Menu → Fields first)."
            a["field"] = f.pk
            a["order"] = "desc" if p.get(f"{prefix}order_{i}") == "desc" else "asc"
        if t == "create_list":
            name = (p.get(f"{prefix}text_{i}") or "").strip()[:120]
            if not name:
                return [], "Name the new list."
            a["text"] = name
        if t in ("create_card", "sort_due", "archive_list", "move_random", "sort_field"):
            need = "list"
        if need == "list":
            lst = List.objects.filter(pk=p.get(f"{prefix}list_{i}") or 0, board=board).first()
            if not lst:
                return [], "Pick the list."
            a["list"] = lst.pk
        elif need == "label":
            lab = Label.objects.filter(pk=p.get(f"{prefix}label_{i}") or 0, board=board).first()
            if not lab:
                return [], "Pick the label."
            a["label"] = lab.pk
        elif need == "user":
            m = BoardMember.objects.filter(board=board, user_id=p.get(f"{prefix}user_{i}") or 0).first()
            if not m:
                return [], "Pick a board member to add."
            a["user"] = m.user_id
        elif need == "days":
            try:
                a["days"] = max(0, min(int(p.get(f"{prefix}days_{i}") or 0), 365))
            except ValueError:
                a["days"] = 0
        elif need == "checklist":
            a["title"] = (p.get(f"{prefix}cltitle_{i}") or "").strip()[:120] or "Checklist"
            a["text"] = (p.get(f"{prefix}clitems_{i}") or "")[:5000]
        elif need == "sticker":
            st = p.get(f"{prefix}sticker_{i}")
            if st not in STICKERS:
                return [], "Pick a sticker."
            a["sticker"] = st
        if need == "message":
            a["text"] = (p.get(f"{prefix}text_{i}") or "").strip()[:250]
        if need == "text" or t == "create_card":
            text = (p.get(f"{prefix}text_{i}") or "").strip()[:2000]
            if not text:
                return [], "Write the card title." if t == "create_card" else "Write the comment to post."
            a["text"] = text[:250] if t == "create_card" else text
        actions.append(a)
    return actions, None


@login_required
@require_POST
def button_create(request, board_id):
    from .models import Button
    board, ok = _editable_board(request, board_id)
    back = _back(board, request)
    if not ok:
        return back
    p = request.POST
    kind = p.get("kind") if p.get("kind") in dict(Button.KINDS) else Button.CARD
    name = (p.get("name") or "").strip()[:40]
    if not name:
        messages.error(request, "Give the button a name.")
        return back
    if kind == Button.CARD:
        actions, error = parse_actions(p, board, prefix="b", extra=[("add_me", "")])
    else:
        actions, error = parse_actions(p, board, prefix="bb", extra=Button.BOARD_ACTIONS)
        actions = [a for a in actions if a["type"] in dict(Button.BOARD_ACTIONS)]
    if error:
        messages.error(request, error)
        return back
    if not actions:
        messages.error(request, "Add at least one action.")
        return back
    Button.objects.create(board=board, kind=kind, name=name, icon=(p.get("icon") or "")[:8], actions=actions,
                          created_by=request.user)
    messages.success(request, f"Button “{name}” added. " + (
        "It shows on every card on this board." if kind == Button.CARD else "It shows in the board's header."))
    return back


@login_required
@require_POST
def button_update(request, board_id, button_id):
    from .models import Button
    board, ok = _editable_board(request, board_id)
    if ok and request.POST.get("action") == "delete":
        get_object_or_404(Button, pk=button_id, board=board).delete()
        messages.success(request, "Button deleted.")
    return _back(board, request)


@login_required
@require_POST
def rule_update(request, board_id, rule_id):
    board, ok = _editable_board(request, board_id)
    if ok:
        rule = get_object_or_404(Rule, pk=rule_id, board=board)
        if request.POST.get("action") == "delete":
            rule.delete()
            messages.success(request, "Rule deleted.")
        else:
            rule.is_enabled = not rule.is_enabled
            rule.save(update_fields=["is_enabled"])
    return _back(board, request)


@login_required
@require_POST
def schedule_create(request, board_id):
    board, ok = _editable_board(request, board_id)
    back = _back(board, request)
    if not ok:
        return back
    p = request.POST
    from .models import Button
    kind = Schedule.ACTIONS if p.get("kind") == Schedule.ACTIONS else Schedule.CARD
    lst, title, actions = None, "", []
    if kind == Schedule.CARD:
        lst = List.objects.filter(pk=p.get("target_list") or 0, board=board, is_archived=False).first()
        title = (p.get("title") or "").strip()[:250]
        if not lst or not title:
            messages.error(request, "A scheduled card needs a title and a list.")
            return back
    else:
        actions, error = parse_actions(p, board, prefix="s", extra=Button.BOARD_ACTIONS)
        actions = [a for a in actions if a["type"] in dict(Button.BOARD_ACTIONS)]
        if error or not actions:
            messages.error(request, error or "Add at least one action.")
            return back

    def num(name, lo, hi, default):
        try:
            return max(lo, min(int(p.get(name)), hi))
        except (TypeError, ValueError):
            return default

    s = Schedule(
        board=board, kind=kind, actions=actions, target_list=lst, title=title, created_by=request.user,
        frequency=p.get("frequency") if p.get("frequency") in dict(Schedule.FREQUENCIES) else Schedule.WEEKLY,
        weekday=num("weekday", 0, 6, 0), month_day=num("month_day", 1, 31, 1), hour=num("hour", 0, 23, 7),
        nth=num("nth", 1, 5, 1), month=num("month", 1, 12, 1),
        checklist=(p.get("checklist") or "")[:5000],
        due_in_days=num("due_in_days", 0, 365, None) if p.get("due_in_days") not in (None, "") else None,
    )
    member = BoardMember.objects.filter(board=board, user_id=p.get("member") or 0).first()
    s.member_id = member.user_id if member else None
    label = Label.objects.filter(pk=p.get("label") or 0, board=board).first()
    s.label = label
    s.next_run_at = automation.next_run(s, timezone.now())
    s.save()
    messages.success(request, f"Scheduled. First run: {timezone.localtime(s.next_run_at):%a %-d %b, %H:%M}.")
    return back


@login_required
@require_POST
def schedule_update(request, board_id, schedule_id):
    board, ok = _editable_board(request, board_id)
    if ok:
        s = get_object_or_404(Schedule, pk=schedule_id, board=board)
        action = request.POST.get("action")
        if action == "delete":
            s.delete()
            messages.success(request, "Scheduled job deleted.")
        elif action == "run":
            if s.kind == Schedule.ACTIONS:
                done = automation.run_board_actions(board, s.actions, request.user)
                messages.success(request, ("Done: " + "; ".join(done) + ".") if done else "Ran — nothing to change.")
            elif s.target_list and not s.target_list.is_archived:
                automation.create_from_schedule(s, s.target_list)
                messages.success(request, f"Created “{s.title}” in {s.target_list.title}.")
            else:
                messages.error(request, "Its list has been archived or deleted.")
        else:
            s.is_enabled = not s.is_enabled
            if s.is_enabled:  # don't fire a backlog of missed runs on re-enable
                s.next_run_at = automation.next_run(s, timezone.now())
            s.save(update_fields=["is_enabled", "next_run_at"])
    return _back(board, request)
