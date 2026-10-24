"""Request forms: a link anyone in the company (or anyone at all) can fill in, which makes a card on a board.

People asking for something don't need to be on the board, learn the board, or remember which list —
the form decides all that, and the board's people are told about each new request."""
import secrets
from datetime import datetime, time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.cache import cache
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import automation, notify, permissions, positions
from .board_context import board_context
from django.contrib.auth import get_user_model

from .models import BoardMember, Card, CardFieldValue, CustomField, Label, List, RequestForm, Rule
from .services import log

KINDS = [("short", "Short answer"), ("long", "Paragraph"), ("number", "Number"), ("date", "Date"),
         ("choice", "Pick one"), ("yesno", "Yes / no")]
MAX_QUESTIONS = 15
PER_HOUR = 60            # submissions per form per hour from one address — stops a stuck button or a bot
User = get_user_model()


def _targets(board):
    """Where an answer can go on the card."""
    out = [("title", "Card title"), ("description", "Description"), ("due", "Due date")]
    out += [(f"field:{f.pk}", f"Field: {f.name}") for f in board.custom_fields.all()]
    return out


def _clean_questions(post, board):
    targets = dict(_targets(board))
    qs, used_title = [], False
    for i in range(MAX_QUESTIONS):
        label = (post.get(f"q_label_{i}") or "").strip()[:200]
        if not label:
            continue
        kind = post.get(f"q_kind_{i}") if post.get(f"q_kind_{i}") in dict(KINDS) else "short"
        to = post.get(f"q_to_{i}") if post.get(f"q_to_{i}") in targets else "description"
        if to == "title":
            if used_title:
                to = "description"
            used_title = True
        if to == "due" and kind != "date":
            to = "description"
        options = [o.strip()[:100] for o in (post.get(f"q_options_{i}") or "").split("\n") if o.strip()][:30]
        if kind == "choice" and not options:
            kind = "short"
        qs.append({"label": label, "kind": kind, "to": to, "required": post.get(f"q_required_{i}") == "1",
                   "help": (post.get(f"q_help_{i}") or "").strip()[:200], "options": options})
    return qs


@login_required
def forms_page(request, board_id):
    board, ctx = board_context(request, board_id, "forms")
    can_edit = ctx["can_edit"]
    if request.method == "POST":
        if not can_edit:
            raise Http404
        return _save(request, board)
    forms = list(board.forms.select_related("list", "label", "assignee"))
    edit = None
    if request.GET.get("edit"):
        edit = next((f for f in forms if str(f.pk) == request.GET["edit"]), None)
    new = request.GET.get("new") == "1" or (can_edit and not forms and not edit)
    starter = [{"label": "What do you need?", "kind": "short", "to": "title", "required": True, "options": [], "help": ""},
               {"label": "Any details?", "kind": "long", "to": "description", "required": False, "options": [], "help": ""},
               {"label": "When do you need it by?", "kind": "date", "to": "due", "required": False, "options": [], "help": ""}]
    ctx.update({
        "forms": [{"f": f, "url": request.build_absolute_uri(reverse("form_fill", args=[f.token]))} for f in forms],
        "editing": edit, "new": new and can_edit,
        "questions": (edit.questions if edit else starter),
        "kinds": KINDS, "targets": _targets(board), "who_choices": RequestForm.WHO,
        "lists": board.lists.filter(is_archived=False), "board_labels": board.labels.all(),
        "max_questions": MAX_QUESTIONS,
    })
    return render(request, "boards/forms.html", ctx)


def _save(request, board):
    p = request.POST
    if p.get("delete"):
        f = get_object_or_404(RequestForm, pk=p["delete"], board=board)
        f.delete()
        messages.success(request, f"Deleted the form “{f.title}”. Its link no longer works.")
        return redirect("board_forms", board_id=board.pk)
    if p.get("toggle"):
        f = get_object_or_404(RequestForm, pk=p["toggle"], board=board)
        f.is_open = not f.is_open
        f.save(update_fields=["is_open"])
        messages.success(request, f"“{f.title}” is {'open again' if f.is_open else 'closed — the link shows a closed message'}.")
        return redirect("board_forms", board_id=board.pk)
    f = get_object_or_404(RequestForm, pk=p["form_id"], board=board) if p.get("form_id") else \
        RequestForm(board=board, created_by=request.user, token=secrets.token_urlsafe(12))
    f.title = (p.get("title") or "").strip()[:120] or "Request"
    f.intro = (p.get("intro") or "").strip()[:2000]
    f.thanks = (p.get("thanks") or "").strip()[:300]
    f.list = get_object_or_404(List, pk=p.get("list"), board=board, is_archived=False)
    f.label = Label.objects.filter(pk=p.get("label") or 0, board=board).first()
    f.assignee = User.objects.filter(pk=p.get("assignee") or 0, board_memberships__board=board).first()
    f.who = p.get("who") if p.get("who") in dict(RequestForm.WHO) else RequestForm.STAFF
    if f.who == RequestForm.ANYONE and not permissions.can_public_link(board, request.user):
        f.who = RequestForm.STAFF
        messages.warning(request, "Only people allowed to make public links can open a form to people outside Lanes — "
                                  "it's set to people signed in to Lanes.")
    f.questions = _clean_questions(p, board)
    if not f.questions:
        messages.error(request, "Add at least one question.")
        return redirect(f"{reverse('board_forms', args=[board.pk])}?{'edit=' + str(f.pk) if f.pk else 'new=1'}")
    f.save()
    messages.success(request, f"Saved “{f.title}”. Copy its link to share it.")
    return redirect(f"{reverse('board_forms', args=[board.pk])}#form-{f.pk}")


# ---------- filling it in ----------

def _rows(f, values, errors):
    return [{"i": i, "q": q, "value": values.get(f"a{i}", ""), "error": errors.get(f"a{i}", "")}
            for i, q in enumerate(f.questions)]


def form_fill(request, token):
    f = get_object_or_404(RequestForm.objects.select_related("board", "list"), token=token)
    if f.board.is_archived or f.list.is_archived:
        raise Http404
    if f.who == RequestForm.STAFF and not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    ctx = {"form": f, "kinds": dict(KINDS), "values": {}, "errors": {}, "done": False, "rows": _rows(f, {}, {})}
    if not f.is_open:
        return render(request, "boards/form_fill.html", {**ctx, "closed": True})
    if request.method == "POST":
        if request.POST.get("website"):                 # honeypot: people never see this box
            return render(request, "boards/form_fill.html", {**ctx, "done": True})
        ip = request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")).split(",")[0].strip()
        key = f"formrate:{f.pk}:{ip}"
        if cache.get(key, 0) >= PER_HOUR:
            ctx["errors"]["general"] = "Too many requests from here in the last hour — please try again later."
            return render(request, "boards/form_fill.html", ctx, status=429)
        answers, errors = _read_answers(request.POST, f)
        name = (request.POST.get("your_name") or "").strip()[:120]
        email = (request.POST.get("your_email") or "").strip()[:200]
        if not request.user.is_authenticated and not name:
            errors["your_name"] = "Please tell us your name."
        if errors:
            return render(request, "boards/form_fill.html", {**ctx, "values": request.POST, "errors": errors,
                                                             "rows": _rows(f, request.POST, errors)})
        card = _make_card(f, answers, request.user if request.user.is_authenticated else None, name, email)
        cache.set(key, cache.get(key, 0) + 1, 3600)
        return render(request, "boards/form_fill.html", {**ctx, "done": True, "card": card,
                                                         "can_see": request.user.is_authenticated and permissions.can_view(f.board, request.user)})
    return render(request, "boards/form_fill.html", ctx)


def _read_answers(post, f):
    answers, errors = [], {}
    for i, q in enumerate(f.questions):
        raw = (post.get(f"a{i}") or "").strip()
        val = raw
        if q["kind"] == "yesno":
            val = "Yes" if raw == "yes" else ("No" if raw == "no" else "")
        elif q["kind"] == "number" and raw:
            try:
                n = float(raw.replace(",", ""))
                val = int(n) if n.is_integer() else n
            except ValueError:
                errors[f"a{i}"] = "Please enter a number."
        elif q["kind"] == "date" and raw:
            try:
                val = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                errors[f"a{i}"] = "Please pick a date."
        elif q["kind"] == "choice" and raw and raw not in q["options"]:
            errors[f"a{i}"] = "Please pick one of the options."
        if q.get("required") and val in ("", None):
            errors[f"a{i}"] = "This one's needed."
        answers.append((q, val))
    return answers, errors


@transaction.atomic
def _make_card(f, answers, user, name, email):
    board = f.board
    title, lines, due, fields = "", [], None, []
    for q, val in answers:
        if val in ("", None):
            continue
        text = val.strftime("%-d %b %Y") if hasattr(val, "strftime") else str(val)
        if q["to"] == "title" and not title:
            title = text.replace("\n", " ")[:250]
        elif q["to"] == "due" and hasattr(val, "strftime"):
            due = timezone.make_aware(datetime.combine(val, time(17, 0)))
            lines.append(f"**{q['label']}** {text}")
        elif q["to"].startswith("field:"):
            fields.append((q["to"][6:], val, text))
        else:
            lines.append(f"**{q['label']}**\n{text}" if q["kind"] == "long" else f"**{q['label']}** {text}")
    who = user.display_name if user else name
    if not title:
        title = f"{f.title} — {who}"[:250]
    sender = f"Sent by {who}" + (f" ({email})" if email and not user else (f" ({user.email})" if user else "")) + \
        f" through the form “{f.title}”."
    card = Card.objects.create(list=f.list, title=title, description="\n\n".join(lines + [f"_{sender}_"]), due_at=due,
                               created_by=user, position=positions.end_position(f.list.cards.filter(is_archived=False)))
    if f.label_id:
        card.labels.add(f.label)
    if f.assignee_id:
        card.members.add(f.assignee)
    for fid, val, text in fields:
        field = CustomField.objects.filter(pk=fid, board=board).first()
        if not field:
            continue
        if field.kind == "number" and not isinstance(val, (int, float)):
            continue
        if field.kind == "dropdown" and text not in (field.options or []):
            continue
        if field.kind == "checkbox":
            val = text == "Yes"
        elif field.kind == "date" and hasattr(val, "isoformat"):
            val = val.isoformat()
        elif not isinstance(val, (int, float, bool)):
            val = text[:500]
        CardFieldValue.objects.create(card=card, field=field, value=val)
    RequestForm.objects.filter(pk=f.pk).update(submissions=f.submissions + 1)
    log(board, user, "form_request", card=card, form=f.title)
    automation.fire(card, Rule.ADDED, list_=f.list, actor=user)
    told = set()
    people = ([f.assignee] if f.assignee else []) + [m.user for m in BoardMember.objects.filter(board=board, role="admin").select_related("user")]
    people += notify.watchers(board=board)
    for u in people:
        if u and u.pk not in told:
            told.add(u.pk)
            notify.send(u, "form", actor=user, card=card, text=f"sent a new request: {card.title}" if user else
                        f"{name} sent a new request: {card.title}", excerpt=f"Form: {f.title}")
    from .signals import bump
    bump(board.pk)
    return card
