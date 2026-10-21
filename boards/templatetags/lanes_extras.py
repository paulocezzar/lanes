import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z][\w.'-]*(?: [A-Z][\w'-]*)?)")


@register.filter
def mentions(text):
    """Escape a comment and highlight @mentions; keeps line breaks."""
    html = _MENTION.sub(r'<span class="mention">@\1</span>', escape(text or ""))
    return mark_safe(html.replace("\n", "<br>"))


@register.filter
def cut_split(value):
    return str(value).split()
