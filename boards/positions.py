"""Float positions for lists and cards.

New items go at the end (last + GAP). A drop between two neighbours takes the
midpoint. When neighbours get closer than MIN_GAP (after ~40 drops into the
same spot) the whole container is re-spaced back to GAP intervals.
"""
GAP = 1024.0
MIN_GAP = 1e-6


def end_position(queryset):
    last = queryset.order_by("-position").values_list("position", flat=True).first()
    return (last or 0) + GAP


def between(before, after):
    """Position for an item dropped between `before` and `after` (either may be None)."""
    if before is None and after is None:
        return GAP
    if before is None:
        return after / 2 if after > 0 else after - GAP
    if after is None:
        return before + GAP
    return (before + after) / 2


def needs_rebalance(before, after):
    return before is not None and after is not None and (after - before) < MIN_GAP


def rebalance(queryset):
    """Re-space every item in the container to GAP intervals, keeping order."""
    items = list(queryset.order_by("position", "id"))
    for i, item in enumerate(items, start=1):
        item.position = i * GAP
    queryset.model.objects.bulk_update(items, ["position"])


def place(item, siblings, before_id, after_id):
    """Set item.position from the ids of its new neighbours (ids, not positions,
    so the server never trusts client maths)."""
    others = siblings.exclude(pk=item.pk)
    before = others.filter(pk=before_id).values_list("position", flat=True).first() if before_id else None
    after = others.filter(pk=after_id).values_list("position", flat=True).first() if after_id else None
    if needs_rebalance(before, after):
        rebalance(others)
        before = others.filter(pk=before_id).values_list("position", flat=True).first() if before_id else None
        after = others.filter(pk=after_id).values_list("position", flat=True).first() if after_id else None
    item.position = between(before, after)


# ---------- lists that hold both cards and mirrored cards ----------

def _entry_positions(lst, exclude=None):
    """[(token, position)] for every live card and mirror in a list."""
    from .models import Card, CardMirror
    out = [(f"c{pk}", pos) for pk, pos in Card.objects.filter(list=lst, is_archived=False)
           .values_list("pk", "position")]
    out += [(f"m{pk}", pos) for pk, pos in CardMirror.objects.filter(list=lst, card__is_archived=False)
            .values_list("pk", "position")]
    return [(t, p) for t, p in out if t != exclude]


def rebalance_list(lst, exclude=None):
    from .models import Card, CardMirror
    entries = sorted(_entry_positions(lst, exclude), key=lambda e: (e[1], e[0]))
    cards, mirrors = [], []
    for i, (tok, _) in enumerate(entries, start=1):
        (cards if tok[0] == "c" else mirrors).append((int(tok[1:]), i * GAP))
    for pk, pos in cards:
        Card.objects.filter(pk=pk).update(position=pos)
    for pk, pos in mirrors:
        CardMirror.objects.filter(pk=pk).update(position=pos)


def place_in_list(obj, lst, before=None, after=None):
    """Set obj.position (a Card or CardMirror) between two neighbours given as
    tokens ('c12' card, 'm4' mirror) or bare card ids. Server-side lookup only."""
    def norm(t):
        if t in (None, ""):
            return None
        t = str(t)
        return t if t[0] in "cm" else f"c{t}"

    me = f"{'m' if obj.__class__.__name__ == 'CardMirror' else 'c'}{obj.pk}" if obj.pk else None
    before, after = norm(before), norm(after)
    for attempt in range(2):
        found = dict(_entry_positions(lst, exclude=me))
        b, a = found.get(before), found.get(after)
        if not needs_rebalance(b, a) or attempt:
            break
        rebalance_list(lst, exclude=me)
    if b is None and a is None and not (before or after):
        obj.position = max(found.values(), default=0) + GAP
    else:
        obj.position = between(b, a)
