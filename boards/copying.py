"""Copy boards and cards — used by Copy board, board templates, Copy card and
card templates. Attachments and comments are never copied; activity starts
fresh on the copy."""
from django.db import transaction

from . import positions
from .models import (Board, BoardMember, Card, CardFieldValue, Checklist, ChecklistItem, CustomField, Label, List,
                     Rule)
from .services import log


def cross_board_maps(src_board, dst_board):
    """Labels and fields of src_board matched on dst_board by name — made there if missing — so a card
    (e.g. a department template) can be copied across boards with its labels and amounts intact."""
    labels = {}
    for lab in src_board.labels.all():
        match = (dst_board.labels.filter(name__iexact=lab.name, colour=lab.colour).first()
                 or (dst_board.labels.filter(name__iexact=lab.name).first() if lab.name else None))
        labels[lab.pk] = match or Label.objects.create(board=dst_board, name=lab.name, colour=lab.colour)
    fields = {}
    top = (dst_board.custom_fields.order_by("-position").first() or CustomField(position=0)).position
    for f in src_board.custom_fields.all():
        match = dst_board.custom_fields.filter(name__iexact=f.name).first()
        if not match:
            top += 1
            match = CustomField.objects.create(board=dst_board, name=f.name, kind=f.kind, options=f.options,
                                               show_on_card=f.show_on_card, position=top)
        fields[f.pk] = match.pk
    return labels, fields


@transaction.atomic
def copy_card(card, target_list, *, user, title=None, keep_labels=True, keep_members=True,
              keep_checklists=True, keep_dates=True, label_map=None, field_map=None, position=None):
    """Copy one card into target_list (possibly on another board, in which case
    label_map/field_map translate the old board's ids to the new board's)."""
    new = Card.objects.create(
        list=target_list, title=(title or card.title)[:250], description=card.description,
        start_date=card.start_date if keep_dates else None, due_at=card.due_at if keep_dates else None,
        created_by=user, position=position if position is not None else positions.end_position(
            target_list.cards.filter(is_archived=False)),
    )
    same_board = target_list.board_id == card.list.board_id
    if keep_labels:
        labels = list(card.labels.all()) if same_board else [
            label_map[lab.pk] for lab in card.labels.all() if label_map and lab.pk in label_map]
        new.labels.set(labels)
    if keep_members:
        allowed = set(BoardMember.objects.filter(board=target_list.board).values_list("user_id", flat=True))
        new.members.set([u for u in card.members.all() if u.pk in allowed])
    if keep_checklists:
        for cl in card.checklists.all():
            ncl = Checklist.objects.create(card=new, title=cl.title, position=cl.position)
            ChecklistItem.objects.bulk_create([
                ChecklistItem(checklist=ncl, text=i.text, position=i.position,
                              assignee_id=i.assignee_id if same_board else None,
                              qty_target=i.qty_target, qty_unit=i.qty_unit, per_unit=i.per_unit)
                for i in cl.items.all()])
    for v in card.field_values.all():
        fid = v.field_id if same_board else (field_map or {}).get(v.field_id)
        fid = getattr(fid, "pk", fid)
        if fid:
            CardFieldValue.objects.create(card=new, field_id=fid, value=v.value)
    log(target_list.board, user, "card_created", card=new, list=target_list.title)
    from .signals import bump
    bump(target_list.board_id)   # bulk_create above skips signals
    return new


@transaction.atomic
def copy_board(board, *, workspace, title, user, keep_cards=True, visibility=None, background=None):
    new = Board.objects.create(
        workspace=workspace, title=title[:120], description=board.description,
        background=background or board.background, visibility=visibility or board.visibility,
        created_by=user,
    )
    BoardMember.objects.create(board=new, user=user, role=BoardMember.ADMIN)
    label_map = {lab.pk: Label.objects.create(board=new, name=lab.name, colour=lab.colour)
                 for lab in board.labels.all()}
    field_map = {f.pk: CustomField.objects.create(board=new, name=f.name, kind=f.kind, options=f.options,
                                                  show_on_card=f.show_on_card, position=f.position).pk
                 for f in board.custom_fields.all()}
    list_map = {}
    for lst in board.lists.filter(is_archived=False):
        list_map[lst.pk] = List.objects.create(board=new, title=lst.title, position=lst.position)
        if keep_cards:
            for card in lst.cards.filter(is_archived=False, is_template=False).prefetch_related(
                    "labels", "checklists__items", "field_values"):
                copy_card(card, list_map[lst.pk], user=user, keep_members=False, keep_dates=False,
                          label_map=label_map, field_map=field_map, position=card.position)
            for card in lst.cards.filter(is_archived=False, is_template=True):
                c = copy_card(card, list_map[lst.pk], user=user, keep_members=False, keep_dates=False,
                              label_map=label_map, field_map=field_map, position=card.position)
                c.is_template = True
                c.save(update_fields=["is_template"])
    # Rules come across with their lists and labels translated
    for rule in board.rules.filter(source__isnull=True):   # shared copies come back via the department
        actions = []
        for a in rule.actions:
            a = dict(a)
            if "list" in a:
                if a["list"] not in list_map:
                    continue
                a["list"] = list_map[a["list"]].pk
            if "label" in a:
                if a["label"] not in label_map:
                    continue
                a["label"] = label_map[a["label"]].pk
            if a.get("type") == "add_member":
                continue  # members don't carry over
            actions.append(a)
        trig_list = list_map.get(rule.trigger_list_id)
        trig_label = label_map.get(rule.trigger_label_id)
        if not actions or (rule.trigger_list_id and not trig_list) or (rule.trigger_label_id and not trig_label):
            continue
        Rule.objects.create(board=new, trigger=rule.trigger, trigger_list=trig_list, trigger_label=trig_label,
                            actions=actions, is_enabled=rule.is_enabled, created_by=user)
    log(new, user, "board_created")
    from .shared_rules import apply_to_new_board
    apply_to_new_board(new)
    return new


@transaction.atomic
def transfer_card(card, target_list, *, user):
    """Move a card (with its comments, attachments, checklists and history) to a
    list on another board. Labels and custom fields are per board, so they're
    matched by name; anything with no match on the new board is dropped.
    Members and checklist assignees who aren't on the new board are removed."""
    from .models import Activity, CardMirror, Watch
    old_board, new_board = card.list.board, target_list.board
    labels = {(lab.name.lower(), lab.colour): lab for lab in new_board.labels.all()}
    by_name = {lab.name.lower(): lab for lab in new_board.labels.all() if lab.name}
    keep = []
    for lab in card.labels.all():
        match = labels.get((lab.name.lower(), lab.colour)) or (by_name.get(lab.name.lower()) if lab.name else None)
        if match:
            keep.append(match)
    card.labels.set(keep)
    allowed = set(BoardMember.objects.filter(board=new_board).values_list("user_id", flat=True))
    card.members.set([u for u in card.members.all() if u.pk in allowed])
    ChecklistItem.objects.filter(checklist__card=card).exclude(assignee_id__in=allowed).update(assignee=None)
    fields = {(f.name.lower(), f.kind): f for f in new_board.custom_fields.all()}
    for v in list(card.field_values.select_related("field")):
        f = fields.get((v.field.name.lower(), v.field.kind))
        if f and not (f.kind == CustomField.DROPDOWN and v.value not in f.options):
            if not CardFieldValue.objects.filter(card=card, field=f).exists():
                v.field = f
                v.save(update_fields=["field"])
                continue
        v.delete()
    CardMirror.objects.filter(card=card, list=target_list).delete()
    card.list = target_list
    card.is_template = False
    card.position = positions.end_position(target_list.cards.filter(is_archived=False))
    card.save(update_fields=["list", "position", "is_template", "updated_at"])
    Activity.objects.filter(card=card).update(board=new_board)    # history travels with the card
    Watch.objects.filter(card=card).exclude(user_id__in=allowed).exclude(user__is_superuser=True).delete()
    log(old_board, user, "card_moved_out", card=None, title=card.title, to=f"{new_board.title} › {target_list.title}")
    log(new_board, user, "card_moved_board", card=card, frm=old_board.title, to=target_list.title)
    return card
