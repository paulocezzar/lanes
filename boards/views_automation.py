"""Board automation page: build rules and recurring-card schedules."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import automation, permissions
from .board_context import board_context
from .models import Board, BoardMember, Label, List, Rule, Schedule

MAX_ACTIONS = 4
NEEDS = {"move": "list", "add_label": "label", "remove_label": "label", "add_member": "user",
         "set_due": "days", "comment": "text"}


@login_required
def automation_page(request, board_id):
    board, ctx = board_context(request, board_id, "automation")
    rules = []
    for r in board.rules.select_related("trigger_list", "trigger_label"):
        when, then = automation.describe_rule(r)
        rules.append({"rule": r, "when": when, "then": then})
    schedules = [{"s": s, "when": automation.describe_schedule(s)}
                 for s in board.schedules.select_related("target_list", "member", "label")]
    ctx.update({
        "rules": rules,
        "schedules": schedules,
        "triggers": Rule.TRIGGERS,
        "actions": Rule.ACTIONS,
        "action_slots": range(MAX_ACTIONS),
        "lists": board.lists.filter(is_archived=False),
        "frequencies": Schedule.FREQUENCIES,
        "weekdays": list(enumerate(automation.WEEKDAYS)),
        "hours": range(0, 24),
        "month_days": range(1, 29),
    })
    return render(request, "boards/automation.html", ctx)


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
    back = redirect("board_automation", board_id=board.pk)
    if not ok:
        return back
    p = request.POST
    trigger = p.get("trigger")
    if trigger not in dict(Rule.TRIGGERS):
        messages.error(request, "Pick when the rule should run.")
        return back
    rule = Rule(board=board, trigger=trigger, created_by=request.user)
    if trigger in (Rule.ADDED, Rule.MOVED):
        rule.trigger_list = List.objects.filter(pk=p.get("trigger_list") or 0, board=board).first()
        if not rule.trigger_list:
            messages.error(request, "Pick which list triggers the rule.")
            return back
    if trigger == Rule.LABEL_ADDED:
        rule.trigger_label = Label.objects.filter(pk=p.get("trigger_label") or 0, board=board).first()
        if not rule.trigger_label:
            messages.error(request, "Pick which label triggers the rule.")
            return back

    actions = []
    for i in range(MAX_ACTIONS):
        t = p.get(f"action_{i}")
        if not t:
            continue
        if t not in dict(Rule.ACTIONS):
            continue
        a = {"type": t}
        need = NEEDS.get(t)
        if need == "list":
            lst = List.objects.filter(pk=p.get(f"list_{i}") or 0, board=board).first()
            if not lst:
                messages.error(request, "Pick the list to move cards to.")
                return back
            a["list"] = lst.pk
        elif need == "label":
            lab = Label.objects.filter(pk=p.get(f"label_{i}") or 0, board=board).first()
            if not lab:
                messages.error(request, "Pick the label.")
                return back
            a["label"] = lab.pk
        elif need == "user":
            m = BoardMember.objects.filter(board=board, user_id=p.get(f"user_{i}") or 0).first()
            if not m:
                messages.error(request, "Pick a board member to add.")
                return back
            a["user"] = m.user_id
        elif need == "days":
            try:
                a["days"] = max(0, min(int(p.get(f"days_{i}") or 0), 365))
            except ValueError:
                a["days"] = 0
        elif need == "text":
            text = (p.get(f"text_{i}") or "").strip()[:2000]
            if not text:
                messages.error(request, "Write the comment to post.")
                return back
            a["text"] = text
        actions.append(a)
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
    return redirect("board_automation", board_id=board.pk)


@login_required
@require_POST
def schedule_create(request, board_id):
    board, ok = _editable_board(request, board_id)
    back = redirect("board_automation", board_id=board.pk)
    if not ok:
        return back
    p = request.POST
    lst = List.objects.filter(pk=p.get("target_list") or 0, board=board, is_archived=False).first()
    title = (p.get("title") or "").strip()[:250]
    if not lst or not title:
        messages.error(request, "A recurring card needs a title and a list.")
        return back

    def num(name, lo, hi, default):
        try:
            return max(lo, min(int(p.get(name)), hi))
        except (TypeError, ValueError):
            return default

    s = Schedule(
        board=board, target_list=lst, title=title, created_by=request.user,
        frequency=p.get("frequency") if p.get("frequency") in dict(Schedule.FREQUENCIES) else Schedule.WEEKLY,
        weekday=num("weekday", 0, 6, 0), month_day=num("month_day", 1, 28, 1), hour=num("hour", 0, 23, 7),
        checklist=(p.get("checklist") or "")[:5000],
        due_in_days=num("due_in_days", 0, 365, None) if p.get("due_in_days") not in (None, "") else None,
    )
    member = BoardMember.objects.filter(board=board, user_id=p.get("member") or 0).first()
    s.member_id = member.user_id if member else None
    label = Label.objects.filter(pk=p.get("label") or 0, board=board).first()
    s.label = label
    s.next_run_at = automation.next_run(s, timezone.now())
    s.save()
    messages.success(request, f"Recurring card added. First one: {timezone.localtime(s.next_run_at):%a %-d %b, %H:%M}.")
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
            messages.success(request, "Recurring card deleted.")
        elif action == "run":
            automation.create_from_schedule(s, s.target_list)
            messages.success(request, f"Created “{s.title}” in {s.target_list.title}.")
        else:
            s.is_enabled = not s.is_enabled
            if s.is_enabled:  # don't fire a backlog of missed runs on re-enable
                s.next_run_at = automation.next_run(s, timezone.now())
            s.save(update_fields=["is_enabled", "next_run_at"])
    return redirect("board_automation", board_id=board.pk)
