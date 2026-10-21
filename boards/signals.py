"""Keep Board.version moving whenever anything on a board changes.

One cheap UPDATE per write. Browsers poll the version and only fetch fresh
HTML when it has moved, so many people can watch a board without load.
"""
from django.db.models import F
from django.db.models.signals import m2m_changed, post_delete, post_save
from django.dispatch import receiver

from .models import (Attachment, Board, BoardMember, Card, CardFieldValue, CardMirror, Checklist, ChecklistItem,
                     Comment, CustomField, Label, List)


def board_id_for(instance):
    if isinstance(instance, Board):
        return None  # board's own edits are rare and reload the page
    if isinstance(instance, CardMirror):
        return List.objects.filter(pk=instance.list_id).values_list("board_id", flat=True).first()
    if isinstance(instance, (List, Label, BoardMember, CustomField)):
        return instance.board_id
    if isinstance(instance, Card):
        return List.objects.filter(pk=instance.list_id).values_list("board_id", flat=True).first()
    if isinstance(instance, (Checklist, Comment, CardFieldValue, Attachment)):
        return Card.objects.filter(pk=instance.card_id).values_list("list__board_id", flat=True).first()
    if isinstance(instance, ChecklistItem):
        return Checklist.objects.filter(pk=instance.checklist_id).values_list(
            "card__list__board_id", flat=True).first()
    return None


def bump(board_id, card_id=None):
    """Move the version of the board — and of every board the card is mirrored onto."""
    ids = {board_id} if board_id else set()
    if card_id:
        from .models import CardMirror
        ids |= set(CardMirror.objects.filter(card_id=card_id).values_list("list__board_id", flat=True))
    if ids:
        Board.objects.filter(pk__in=ids).update(version=F("version") + 1)


def card_id_for(instance):
    if isinstance(instance, Card):
        return instance.pk
    if isinstance(instance, (Comment, CardFieldValue, Attachment, Checklist)):
        return instance.card_id
    if isinstance(instance, ChecklistItem):
        return Checklist.objects.filter(pk=instance.checklist_id).values_list("card_id", flat=True).first()
    return None


TRACKED = (List, Card, Checklist, ChecklistItem, Comment, Label, BoardMember, CustomField, CardFieldValue, Attachment,
           CardMirror)


@receiver(post_save)
def _saved(sender, instance, raw=False, **kwargs):
    if sender in TRACKED and not raw:
        bump(board_id_for(instance), card_id_for(instance))


@receiver(post_delete)
def _deleted(sender, instance, **kwargs):
    if sender in TRACKED:
        try:
            bump(board_id_for(instance), card_id_for(instance))
        except Exception:  # parent already gone in a cascade — the parent's own signal covers it
            pass


@receiver(m2m_changed, sender=Card.labels.through)
@receiver(m2m_changed, sender=Card.members.through)
def _card_m2m(sender, instance, action, **kwargs):
    if action in ("post_add", "post_remove", "post_clear") and isinstance(instance, Card):
        bump(board_id_for(instance), instance.pk)
