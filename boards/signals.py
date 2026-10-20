"""Keep Board.version moving whenever anything on a board changes.

One cheap UPDATE per write. Browsers poll the version and only fetch fresh
HTML when it has moved, so many people can watch a board without load.
"""
from django.db.models import F
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from .models import (Board, BoardMember, Card, Checklist, ChecklistItem, Comment, Label, List)


def board_id_for(instance):
    if isinstance(instance, Board):
        return None  # board's own edits are rare and reload the page
    if isinstance(instance, (List, Label, BoardMember)):
        return instance.board_id
    if isinstance(instance, Card):
        return List.objects.filter(pk=instance.list_id).values_list("board_id", flat=True).first()
    if isinstance(instance, (Checklist, Comment)):
        return Card.objects.filter(pk=instance.card_id).values_list("list__board_id", flat=True).first()
    if isinstance(instance, ChecklistItem):
        return Checklist.objects.filter(pk=instance.checklist_id).values_list(
            "card__list__board_id", flat=True).first()
    return None


def bump(board_id):
    if board_id:
        Board.objects.filter(pk=board_id).update(version=F("version") + 1)


TRACKED = (List, Card, Checklist, ChecklistItem, Comment, Label, BoardMember)


@receiver(post_save)
def _saved(sender, instance, raw=False, **kwargs):
    if sender in TRACKED and not raw:
        bump(board_id_for(instance))


@receiver(post_delete)
def _deleted(sender, instance, **kwargs):
    if sender in TRACKED:
        try:
            bump(board_id_for(instance))
        except Exception:  # parent already gone in a cascade — the parent's own signal covers it
            pass


@receiver(m2m_changed, sender=Card.labels.through)
@receiver(m2m_changed, sender=Card.members.through)
def _card_m2m(sender, instance, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear") and isinstance(instance, Card):
        bump(board_id_for(instance))
