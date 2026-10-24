"""Department-wide rules: one rule, kept in step on every board in its department.

The rule lives on one board (the source). Each other board gets a copy whose lists,
labels and fields are matched by name. A board that lacks a list or label the rule needs
is skipped (and told why) rather than given new lists it never asked for; missing labels and
fields are simply created there."""
from django.db import transaction

from .models import Board, Rule

ID_KEYS = {"list": "lists", "from_list": "lists", "label": "labels", "field": "fields"}


def _maps(src_board, dst_board):
    def by(qs, key):
        return {key(o): o for o in qs}
    dst_lists = by(dst_board.lists.filter(is_archived=False), lambda o: o.title.strip().lower())
    dst_labels = by(dst_board.labels.all(), lambda o: (o.name.strip().lower(), o.colour))
    dst_labels_by_name = by(dst_board.labels.all(), lambda o: o.name.strip().lower())
    dst_fields = by(dst_board.custom_fields.all(), lambda o: o.name.strip().lower())
    maps = {"lists": {}, "labels": {}, "fields": {}}
    for lst in src_board.lists.all():
        maps["lists"][lst.pk] = (dst_lists.get(lst.title.strip().lower()), lst)
    for lab in src_board.labels.all():
        t = dst_labels.get((lab.name.strip().lower(), lab.colour)) or (
            dst_labels_by_name.get(lab.name.strip().lower()) if lab.name.strip() else None)
        maps["labels"][lab.pk] = (t, lab)
    for f in src_board.custom_fields.all():
        maps["fields"][f.pk] = (dst_fields.get(f.name.strip().lower()), f)
    maps["board"] = dst_board
    return maps


def _swap(obj_id, kind, maps):
    """The matching id on the other board. Missing labels and fields are made there (they're cheap and
    harmless); a missing list is a reason to skip the board — we don't invent lists."""
    from .models import CustomField, Label
    if obj_id in (None, "", "new"):
        return obj_id, None
    target, src = maps[kind].get(int(obj_id), (None, None))
    if target:
        return target.pk, None
    if src is None:
        return None, f"a {kind[:-1]} that no longer exists"
    dst = maps["board"]
    if kind == "labels":
        target = Label.objects.create(board=dst, name=src.name, colour=src.colour)
    elif kind == "fields":
        top = (dst.custom_fields.order_by("-position").first() or CustomField(position=0)).position
        target = CustomField.objects.create(board=dst, name=src.name, kind=src.kind, options=src.options,
                                            show_on_card=src.show_on_card, position=top + 1)
    else:
        return None, f"list “{src.title}”"
    maps[kind][int(obj_id)] = (target, src)
    return target.pk, None


def translate(rule, board):
    """Field values for a copy of `rule` on `board`, or (None, 'missing list “Done”')."""
    maps = _maps(rule.board, board)
    out = {}
    for attr, kind in (("trigger_list_id", "lists"), ("trigger_label_id", "labels"), ("trigger_field_id", "fields")):
        val, missing = _swap(getattr(rule, attr), kind, maps)
        if missing:
            return None, f"it has no {missing}"
        out[attr] = val
    also = []
    for a in rule.also or []:
        a = dict(a)
        for key, kind in (("trigger_list", "lists"), ("trigger_label", "labels"), ("trigger_field", "fields")):
            if key in a:
                a[key], missing = _swap(a[key], kind, maps)
                if missing:
                    return None, f"it has no {missing}"
        also.append(a)
    out["also"] = also
    for attr in ("filters", "actions"):
        items = []
        for a in getattr(rule, attr) or []:
            a = dict(a)
            for key, kind in ID_KEYS.items():
                if key in a:
                    a[key], missing = _swap(a[key], kind, maps)
                    if missing:
                        return None, f"it has no {missing}"
            items.append(a)
        out[attr] = items
    for attr in ("trigger", "trigger_hours", "by_whom", "trigger_me", "trigger_count", "trigger_weekday",
                 "is_enabled", "created_by_id"):
        out[attr] = getattr(rule, attr)
    return out, None


def boards_for(rule):
    return Board.objects.filter(workspace_id=rule.board.workspace_id, is_archived=False).exclude(pk=rule.board_id)


@transaction.atomic
def sync(rule):
    """Bring every copy in line with the source. Returns (boards updated, [(board, why skipped)])."""
    if rule.source_id:
        rule = rule.source
    if not rule.everywhere:
        rule.copies.all().delete()
        return 0, []
    done, skipped = 0, []
    for board in boards_for(rule):
        n, why = sync_board(rule, board)
        done += n
        if why:
            skipped.append((board, why))
    return done, skipped


def sync_board(rule, board):
    values, why = translate(rule, board)
    existing = rule.copies.filter(board=board).first()
    if values is None:
        if existing:
            existing.delete()
        return 0, why
    if existing:
        for k, v in values.items():
            setattr(existing, k, v)
        existing.save()
    else:
        Rule.objects.create(board=board, source=rule, **values)
    return 1, None


def apply_to_new_board(board):
    """A board just joined a department: give it that department's department-wide rules."""
    for rule in Rule.objects.filter(everywhere=True, source__isnull=True, board__workspace_id=board.workspace_id,
                                    board__is_archived=False).exclude(board=board).select_related("board"):
        sync_board(rule, board)
