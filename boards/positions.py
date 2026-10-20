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
