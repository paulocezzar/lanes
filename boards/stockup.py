"""The StockUp link.

Lanes plans; StockUp makes. A card can be linked to an item on a StockUp seasonal
plan: Lanes then creates the run in StockUp (that day, that quantity), keeps its day
in step with the card's due date, gives the job to the card's person, and reads back
how much is made and the batch codes. When StockUp confirms the batch the card's
"Made" item fills up and the card completes itself (and moves to Done).

A board can also be made from a whole StockUp plan: one card per product and per
component, with every run as a part under its component. Quantities, recipes and
batches stay StockUp's; the people, dates of new runs, discussion and desk work are
Lanes'.

Off unless STOCKUP_URL and STOCKUP_TOKEN are set.
"""
import json
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
UNIT_WORD = {"kg": "kg", "each": "", "singles": "singles", "packs": "packs"}


class StockUpError(Exception):
    pass


def enabled():
    return bool(settings.STOCKUP_URL and settings.STOCKUP_TOKEN)


# ---------- which departments use StockUp ----------

def departments_for(user):
    """The user's Lanes departments that plan in StockUp (all of them, for a Lanes admin)."""
    from .models import Workspace
    qs = Workspace.objects.exclude(stockup_department="")
    if not user.is_superuser:
        qs = qs.filter(memberships__user=user).distinct()
    return list(qs.order_by("name"))


def allowed(user):
    return bool(enabled() and user.is_authenticated and departments_for(user))


def board_on(board):
    return bool(enabled() and board.workspace.stockup_department)


def plans_for(user, workspace=None):
    """StockUp plans the user may see: those of the StockUp departments their departments plan for."""
    wss = [workspace] if workspace is not None else departments_for(user)
    names = {w.stockup_department.strip().lower() for w in wss if w.stockup_department}
    return [p for p in plans() if (p.get("department") or "").strip().lower() in names]


def _call(method, path, data=None, timeout=15):
    if not enabled():
        raise StockUpError("StockUp isn't connected yet.")
    url = f"{settings.STOCKUP_URL}/api/agent/lanes/{path}"
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {settings.STOCKUP_TOKEN}", "Content-Type": "application/json",
        "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error") or ""
        except Exception:
            msg = ""
        if e.code == 403:
            msg = "StockUp refused the key — check STOCKUP_TOKEN on both sides."
        raise StockUpError(msg or f"StockUp answered {e.code}.")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise StockUpError("Couldn't reach StockUp just now.") from e


def get(path):
    return _call("GET", path)


def post(path, data):
    return _call("POST", path, data)


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def fmt(q, unit):
    from .models import ChecklistItem
    n = ChecklistItem.fmt_qty(_dec(q))
    w = UNIT_WORD.get(unit, unit)
    return f"{n} {w}".strip()


# ---------- catalogue (cached briefly; plans change rarely) ----------

def plans():
    key = "stockup:plans"
    got = cache.get(key)
    if got is None:
        got = get("plans/")["plans"]
        cache.set(key, got, 120)
    return got


def plan_items(plan_id):
    """What the plan still needs made — the picker's list."""
    key = f"stockup:plan:{plan_id}"
    got = cache.get(key)
    if got is None:
        got = get(f"plans/{int(plan_id)}/")
        cache.set(key, got, 120)
    return got


# ---------- the Made item on a linked card ----------

def _made_item(card, link, create=True):
    from .models import Checklist, ChecklistItem
    cl = card.checklists.filter(title="Made in StockUp").first()
    if cl is None:
        if not create:
            return None
        cl = Checklist.objects.create(card=card, title="Made in StockUp", position=0)
    item = cl.items.first()
    if item is None:
        if not create:
            return None
        item = ChecklistItem.objects.create(checklist=cl, text=link.item_name[:250], position=1)
    return item


def _apply_progress(link, user=None):
    """Mirror the run's planned / made quantity and state onto the card's Made item."""
    from .features import settle
    card = link.card
    item = _made_item(card, link)
    target = link.planned_qty or Decimal("0")
    made = min(link.made_qty or Decimal("0"), target) if target else link.made_qty
    done = link.done or (target and made >= target)
    if done and target:
        made = target
    changed = []
    unit = UNIT_WORD.get(link.unit, link.unit)
    for f, v in (("qty_target", target or None), ("qty_done", made or Decimal("0")), ("qty_unit", unit[:20])):
        if getattr(item, f) != v:
            setattr(item, f, v)
            changed.append(f)
    if bool(done) != item.is_done:
        item.is_done = bool(done)
        item.done_at = timezone.now() if done else None
        changed += ["is_done", "done_at"]
    if changed:
        item.save()
        settle(type(card).objects.get(pk=card.pk), user)


def _due(d):
    if not d:
        return None
    return timezone.make_aware(datetime.combine(d, time(17, 0)))


def _actor(user):
    return getattr(user, "display_name", "") or ""


# ---------- link a card: schedule a run ----------

@transaction.atomic
def link_card(card, user, plan_id, owner, owner_id, unit, qty, day):
    from .models import StockUpLink
    from .services import log
    if hasattr(card, "stockup"):
        raise StockUpError("This card is already linked to StockUp.")
    detail = plan_items(plan_id)
    item = next((i for i in detail.get("items", []) if i["owner"] == owner and int(i["owner_id"]) == int(owner_id)), None)
    if item is None:
        raise StockUpError("That item isn't on the plan any more.")
    run = post("runs/", {"plan_id": int(plan_id), "owner": owner, "owner_id": int(owner_id),
                         "unit": unit or item["unit"], "qty": str(qty),
                         "date": day.isoformat() if day else "", "actor": _actor(user),
                         "note": f"Lanes card: {card.title[:150]}"})
    link = StockUpLink.objects.create(
        card=card, kind=StockUpLink.RUN, plan_id=int(plan_id), plan_name=detail["plan"]["name"],
        owner=owner, owner_id=int(owner_id), item_name=item["name"], item_code=item.get("code", ""),
        unit=run["unit"], run_id=run["id"])
    _store_run(link, run)
    if day and (not card.due_at or timezone.localdate(card.due_at) != day):
        type(card).objects.filter(pk=card.pk).update(due_at=_due(day))
        card.refresh_from_db()
    log(card.list.board, user, "stockup_linked", card=card, text=f"{item['name']} — {fmt(qty, run['unit'])}",
        day=day.strftime("%a %-d %b") if day else "no day yet")
    _apply_progress(link, user)
    sync_job(link, user)
    return link


def _store_run(link, run, save=True):
    link.planned_qty = _dec(run["planned_qty"])
    link.planned_date = date.fromisoformat(run["planned_date"]) if run.get("planned_date") else None
    link.made_qty = _dec(run.get("made_qty"))
    link.state = run["state"]
    link.batch_codes = run.get("batch_codes") or []
    link.error = ""
    link.synced_at = timezone.now()
    if save:
        link.save()


def unlink_card(card, user):
    from .services import log
    link = getattr(card, "stockup", None)
    if link is None:
        return
    if link.run_id and link.kind == link.RUN and link.state not in ("confirmed", "cancelled"):
        post(f"runs/{link.run_id}/", {"cancel": True, "actor": _actor(user)})
    if link.job_id:
        try:
            post(f"jobs/{link.job_id}/", {"archive": True, "actor": _actor(user)})
        except StockUpError:
            pass
    log(card.list.board, user, "stockup_unlinked", card=card, text=link.item_name)
    link.delete()


def change_run(link, user, day=None, qty=None):
    """Push a new day and/or quantity to StockUp. Raises StockUpError when StockUp refuses
    (e.g. work is already recorded against the run)."""
    data = {"actor": _actor(user)}
    if day is not False:
        data["planned_date"] = day.isoformat() if day else ""
    if qty is not None:
        data["planned_qty"] = str(qty)
    run = post(f"runs/{link.run_id}/", data)
    _store_run(link, run)
    _apply_progress(link, user)
    sync_job(link, user)


def after_card_change(card, user, changed):
    """Called after a linked card is edited: its due date moves the run, archiving
    cancels it, people changes give the job to someone else."""
    link = getattr(card, "stockup", None)
    if link is None or not enabled():
        return None
    try:
        if "archive" in changed and card.is_archived and link.kind == link.RUN:
            unlink_card(card, user)
            return "Cancelled the run in StockUp."
        if "due_at" in changed and link.kind == link.RUN and link.run_id and link.state != "confirmed":
            new = timezone.localdate(card.due_at) if card.due_at else None
            if new != link.planned_date:
                change_run(link, user, day=new)
                return f"Moved the StockUp run to {new:%a %-d %b}." if new else "Took the StockUp run off the calendar."
        if "members" in changed:
            sync_job(link, user)
    except StockUpError as e:
        link.error = str(e)[:300]
        link.save(update_fields=["error"])
        return f"StockUp: {e}"
    return None


# ---------- people and jobs ----------

def people(refresh=False):
    key = "stockup:people"
    got = None if refresh else cache.get(key)
    if got is None:
        got = get("people/")["people"]
        cache.set(key, got, 600)
    return got


def _norm(s):
    return " ".join((s or "").lower().split())


def match_people():
    """Link Lanes users to StockUp people by email, then by full name. Never overwrites
    a link someone chose by hand. Returns how many new links were made."""
    from django.contrib.auth import get_user_model
    from .models import StockUpPerson
    User = get_user_model()
    them = people(refresh=True)
    by_email = {_norm(p["email"]): p for p in them if p.get("email")}
    by_name = {}
    for p in them:
        by_name.setdefault(_norm(p["name"]), []).append(p)
    taken = set(StockUpPerson.objects.values_list("person_id", flat=True))
    made = 0
    for u in User.objects.filter(is_active=True, stockup_person__isnull=True):
        p = by_email.get(_norm(u.email))
        if p is None:
            hits = by_name.get(_norm(f"{u.first_name} {u.last_name}"), [])
            p = hits[0] if len(hits) == 1 else None
        if p and p["id"] not in taken:
            StockUpPerson.objects.create(user=u, person_id=p["id"], person_name=p["name"],
                                         department=p.get("department", ""))
            taken.add(p["id"])
            made += 1
    return made


def sync_job(link, user=None):
    """The card's first member who is a StockUp person gets the job on the run's day.
    Nobody linked → the job is archived. Done jobs are left alone."""
    from .models import StockUpPerson
    if link.kind != link.RUN or link.state in ("confirmed", "cancelled"):
        return
    card = link.card
    members = list(card.members.all())
    sp = StockUpPerson.objects.filter(user__in=members).select_related("user").first()
    day = link.planned_date
    name = f"{link.item_name} — {fmt(link.planned_qty, link.unit)}"[:120]
    note = f"From Lanes: {card.title}\n{settings.SITE_URL}/b/{card.list.board_id}/?card={card.pk}"
    try:
        if sp is None or day is None:
            if link.job_id and link.job_status == "open":
                post(f"jobs/{link.job_id}/", {"archive": True, "actor": _actor(user)})
                link.job_id, link.job_person, link.job_status = None, "", ""
                link.save(update_fields=["job_id", "job_person", "job_status"])
            return
        if link.job_id:
            if link.job_status and link.job_status != "open":
                return
            job = post(f"jobs/{link.job_id}/", {"date": day.isoformat(), "person_id": sp.person_id,
                                                "name": name, "actor": _actor(user)})
        else:
            job = post("jobs/", {"person_id": sp.person_id, "date": day.isoformat(), "name": name,
                                 "note": note, "actor": _actor(user)})
        _store_job(link, job)
    except StockUpError as e:
        link.error = str(e)[:300]
        link.save(update_fields=["error"])


def _store_job(link, job):
    link.job_id = job["id"]
    link.job_person = job.get("person") or ""
    link.job_status = "archived" if job.get("archived") else (job.get("status") or "")
    link.job_done_by = job.get("done_by") or ""
    link.save(update_fields=["job_id", "job_person", "job_status", "job_done_by"])


# ---------- reading progress back ----------

def sync(run_ids=None, job_ids=None):
    """Bring linked cards up to date from StockUp. With no ids: every open link."""
    from .models import StockUpLink
    if not enabled():
        return {"skipped": "not connected"}
    links = StockUpLink.objects.select_related("card__list__board").exclude(run_id=None)
    if run_ids is not None:
        links = links.filter(run_id__in=run_ids)
    else:
        links = links.exclude(state__in=["cancelled"]).exclude(
            state="confirmed", synced_at__lt=timezone.now() - timedelta(days=3))
    links = list(links)
    updated = 0
    for chunk in range(0, len(links), 200):
        part = links[chunk:chunk + 200]
        got = get("runs/?ids=" + ",".join(str(l.run_id) for l in part))
        by_id = {r["id"]: r for r in got["runs"]}
        for link in part:
            r = by_id.get(link.run_id)
            if r is None:
                if link.state != "cancelled":
                    link.state, link.error = "cancelled", "The run is no longer in StockUp."
                    link.save(update_fields=["state", "error"])
                continue
            before = (link.state, link.made_qty, link.planned_date, link.planned_qty, list(link.batch_codes))
            _store_run(link, r)
            after = (link.state, link.made_qty, link.planned_date, link.planned_qty, list(link.batch_codes))
            if before != after:
                updated += 1
                _after_run_changed(link, before)
    jl = StockUpLink.objects.exclude(job_id=None)
    if job_ids is not None:
        jl = jl.filter(job_id__in=job_ids)
    else:
        jl = jl.filter(job_status="open")
    jl = list(jl)
    if jl:
        got = get("jobs/?ids=" + ",".join(str(l.job_id) for l in jl[:300]))
        by_id = {j["id"]: j for j in got["jobs"]}
        for link in jl:
            if link.job_id in by_id:
                was = link.job_status
                _store_job(link, by_id[link.job_id])
                if was != link.job_status and link.job_status == "done":
                    from .services import log
                    log(link.card.list.board, None, "stockup_job_done", card=link.card,
                        name=link.job_done_by or link.job_person)
    return {"runs": len(links), "updated": updated, "jobs": len(jl)}


def _after_run_changed(link, before):
    from .services import log
    card = link.card
    board = card.list.board
    was_state, _, was_day, _, was_codes = before
    new_codes = [c for c in link.batch_codes if c not in was_codes]
    if new_codes:
        log(board, None, "stockup_batch", card=card, codes=", ".join(new_codes))
    if link.planned_date != was_day and link.planned_date and link.kind == link.RUN:
        type(card).objects.filter(pk=card.pk).update(due_at=_due(link.planned_date))
        card.refresh_from_db()
        log(board, None, "stockup_moved", card=card, day=link.planned_date.strftime("%a %-d %b"))
    _apply_progress(link)


# ---------- a board from a whole plan ----------

def _parse(d):
    return date.fromisoformat(d) if d else None


@transaction.atomic
def build_plan_board(plan_id, user, workspace):
    """A new board for a StockUp plan. Lists: Products, Components, Runs, Desk work, Done."""
    from . import positions
    from .models import Board, BoardMember
    detail = get(f"plans/{int(plan_id)}/")
    plan = detail["plan"]
    board = Board.objects.create(
        workspace=workspace, title=f"{plan['name']} — StockUp"[:120], created_by=user,
        background="forest", stockup_plan_id=plan["id"], is_project=True,
        description=f"Made from the StockUp plan “{plan['name']}”. Products, components and runs "
                    "come from StockUp and stay in step with it; add your own desk work, people and dates here.")
    BoardMember.objects.create(board=board, user=user, role=BoardMember.ADMIN)
    for i, t in enumerate(["Products", "Components", "Runs", "Desk work", "Done"], 1):
        board.lists.create(title=t, position=i * positions.GAP)
    refresh_plan_board(board, user, detail=detail)
    return board


def refresh_plan_board(board, user=None, detail=None):
    """Add what's new on the plan, update what changed. Never deletes a card — a run
    StockUp cancelled is marked and archived; your own cards are never touched."""
    from . import positions
    from .models import Card, StockUpLink
    detail = detail or get(f"plans/{int(board.stockup_plan_id)}/")
    lists = {l.title: l for l in board.lists.filter(is_archived=False)}

    def lst(name):
        if name not in lists:
            lists[name] = board.lists.create(title=name, position=positions.end_position(board.lists.all()))
        return lists[name]

    have = {(l.kind, l.owner_id if l.kind != "run" else l.run_id): l
            for l in StockUpLink.objects.filter(card__list__board=board).select_related("card")}
    made = {"cards": 0}

    def card_for(kind, key, title, target_list, **link_fields):
        link = have.get((kind, key))
        if link:
            card = link.card
            if card.title != title[:250] and kind != "run":
                Card.objects.filter(pk=card.pk).update(title=title[:250])
            for f, v in link_fields.items():
                setattr(link, f, v)
            link.save()
            return card, link, False
        card = Card.objects.create(list=target_list, title=title[:250], created_by=user,
                                   position=positions.end_position(target_list.cards.all()))
        link = StockUpLink.objects.create(card=card, kind=kind, plan_id=board.stockup_plan_id,
                                          plan_name=detail["plan"]["name"], **link_fields)
        have[(kind, key)] = link
        made["cards"] += 1
        return card, link, True

    for p in detail["products"]:
        card, link, new = card_for("product", p["id"], p["name"], lst("Products"), owner="plan_line",
                                   owner_id=p["id"], item_name=p["name"], unit="singles",
                                   planned_qty=_dec(p["quantity"]))
        launch = _parse(p.get("launch"))
        sell_end = _parse(detail["plan"].get("end"))
        # A product's bar is its SELLING period — launch to the end of the plan — not a deadline
        # that passes on launch day. (Boards made before this had launch as a milestone due date.)
        untouched = card.due_at is None or (launch and card.due_at == _due(launch) and not card.start_date)
        if new or untouched:      # never overwrite dates someone set by hand
            Card.objects.filter(pk=card.pk).update(start_date=launch, due_at=_due(sell_end or launch),
                                                   is_milestone=False)
        desc = f"{fmt(p['quantity'], '')} planned{(' · ' + p['pack_size']) if p.get('pack_size') else ''}" \
               f"{(' · on sale from ' + launch.strftime('%a %-d %b')) if launch else ''}{(' to ' + sell_end.strftime('%a %-d %b')) if sell_end else ''}. From StockUp."
        Card.objects.filter(pk=card.pk).update(description=desc)

    comp_cards = {}
    for c in detail["components"]:
        title = f"{c['name']} ({c['code']})"
        card, link, new = card_for("component", c["id"], title, lst("Components"), owner="requirement",
                                   owner_id=c["id"], item_name=c["name"], item_code=c["code"], unit=c["unit"],
                                   planned_qty=_dec(c["required_qty"]))
        pct = _dec(c.get("progress_pct") or 0)
        link.made_qty = (link.planned_qty or 0) * pct / 100
        link.state = "confirmed" if pct >= 100 else c.get("state", "")
        link.save()
        fields = {}
        if c.get("earliest"):
            fields["start_date"] = _parse(c["earliest"])
        if c.get("latest") and (new or not card.due_at):
            fields["due_at"] = _due(_parse(c["latest"]))
        used = ", ".join(c.get("used_in") or []) if isinstance(c.get("used_in"), list) and all(isinstance(x, str) for x in c.get("used_in") or []) else ""
        fields["description"] = f"Needs {fmt(c['required_qty'], c['unit'])} for this plan" + (f" · used in {used}" if used else "") + ". From StockUp."
        Card.objects.filter(pk=card.pk).update(**fields)
        comp_cards[c["id"]] = card
        _apply_progress(link, user)

    for r in detail["runs"]:
        parent = comp_cards.get(r["owner_id"]) if r["owner"] == "requirement" else None
        title = f"{r['name']} — run {r['seq']}: {fmt(r['planned_qty'], r['unit'])}"
        card, link, new = card_for("run", r["id"], title, lst("Runs"), owner=r["owner"], owner_id=r["owner_id"],
                                   item_name=r["name"], item_code=r.get("code", ""), unit=r["unit"], run_id=r["id"])
        _store_run(link, r)
        day = _parse(r.get("planned_date"))
        Card.objects.filter(pk=card.pk).update(parent=parent, start_date=day, due_at=_due(day))
        _apply_progress(link, user)
    # runs no longer on the plan (cancelled in StockUp)
    live = {r["id"] for r in detail["runs"]}
    for (kind, key), link in list(have.items()):
        if kind == "run" and key not in live and link.state != "cancelled":
            link.state = "cancelled"
            link.save(update_fields=["state"])
            Card.objects.filter(pk=link.card_id).update(is_archived=True)
    board.stockup_synced_at = timezone.now()
    board.save(update_fields=["stockup_synced_at"])
    cache.delete(f"stockup:plan:{board.stockup_plan_id}")
    from .signals import bump
    bump(board.pk)
    return made["cards"]


def refresh_plan_boards(max_age_minutes=30):
    from .models import Board
    if not enabled():
        return 0
    n = 0
    stale = timezone.now() - timedelta(minutes=max_age_minutes)
    for b in Board.objects.filter(stockup_plan_id__isnull=False, is_archived=False):
        if b.stockup_synced_at and b.stockup_synced_at > stale:
            continue
        try:
            refresh_plan_board(b)
            n += 1
        except StockUpError as e:
            logger.warning("stockup refresh of board %s failed: %s", b.pk, e)
    return n


def refresh_card(card, user=None):
    """Pull StockUp's figures for one linked card (run: its run; product/component: its plan board)."""
    link = getattr(card, "stockup", None)
    if not link:
        return
    if link.run_id:
        sync(run_ids=[link.run_id], job_ids=[link.job_id] if link.job_id else [])
    elif card.list.board.stockup_plan_id:
        refresh_plan_board(card.list.board, user)
