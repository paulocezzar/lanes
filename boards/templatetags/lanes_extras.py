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


# ---------- light Markdown for descriptions and comments ----------
# Escape first, then add a small, safe set of formatting. No raw HTML ever
# gets through, and links only allow http(s).

_CODE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
_URL = re.compile(r"(?<![\w/\"=])(https?://[^\s<]+[^\s<.,;:!?)\]'])")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITAL = re.compile(r"(?<![\w*])[*_](?=\S)([^*_\n]+?)(?<=\S)[*_](?![\w*])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_UL = re.compile(r"^\s*[-*•]\s+(.*)$")
_OL = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_TASK = re.compile(r"^\[( |x|X)\]\s+(.*)$")


def _inline(s):
    keep = []

    def stash(html):
        keep.append(html)
        return f"\x00{len(keep) - 1}\x00"

    s = _CODE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), s)
    s = _LINK.sub(lambda m: stash(f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>'), s)
    s = _URL.sub(lambda m: stash(f'<a href="{m.group(1)}" target="_blank" rel="noopener">{m.group(1)}</a>'), s)
    s = _BOLD.sub(r"<strong>\1</strong>", s)
    s = _STRIKE.sub(r"<s>\1</s>", s)
    s = _ITAL.sub(r"<em>\1</em>", s)
    s = _MENTION.sub(r'<span class="mention">@\1</span>', s)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], s)


def render_md(text):
    lines = escape(text or "").replace("\r\n", "\n").split("\n")
    out, para, lst = [], [], None   # lst = ("ul"|"ol", [items])

    def flush_para():
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
            para.clear()

    def flush_list():
        nonlocal lst
        if lst:
            tag, items = lst
            lis = []
            for it in items:
                t = _TASK.match(it)
                if t:
                    box = "☑" if t.group(1).lower() == "x" else "☐"
                    lis.append(f'<li class="task">{box} {_inline(t.group(2))}</li>')
                else:
                    lis.append(f"<li>{_inline(it)}</li>")
            out.append(f"<{tag}>{''.join(lis)}</{tag}>")
            lst = None

    for line in lines:
        ul, ol = _UL.match(line), _OL.match(line)
        if ul or ol:
            flush_para()
            tag = "ul" if ul else "ol"
            if not lst or lst[0] != tag:
                flush_list()
                lst = (tag, [])
            lst[1].append((ul or ol).group(1))
            continue
        flush_list()
        stripped = line.strip()
        if not stripped:
            flush_para()
        elif stripped.startswith("#"):
            flush_para()
            level = min(len(stripped) - len(stripped.lstrip("#")), 2)
            out.append(f"<h{3 + level}>{_inline(stripped.lstrip('#').strip())}</h{3 + level}>")
        elif stripped.startswith("&gt;"):
            flush_para()
            out.append(f"<blockquote>{_inline(stripped[4:].strip())}</blockquote>")
        elif stripped in ("---", "***"):
            flush_para()
            out.append("<hr>")
        else:
            para.append(line)
    flush_para()
    flush_list()
    return "".join(out)


@register.filter
def md(text):
    return mark_safe(render_md(text))
