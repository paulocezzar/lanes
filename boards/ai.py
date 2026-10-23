"""Small Claude API helper for inbox capture and Suggest steps.

Plain HTTPS (no SDK) so the dependency list stays short. Everything here is
optional: with no ANTHROPIC_API_KEY the features are hidden and these
functions raise AIUnavailable.
"""
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import date

from django.conf import settings

log = logging.getLogger(__name__)
API_URL = "https://api.anthropic.com/v1/messages"
MAX_INPUT = 8000


class AIUnavailable(Exception):
    pass


def enabled():
    return bool(settings.ANTHROPIC_API_KEY)


def _call(system, user_text, max_tokens=1500, images=(), max_input=MAX_INPUT, timeout=45, model=None):
    if not enabled():
        raise AIUnavailable("AI isn't switched on for Lanes yet.")
    content = user_text[:max_input]
    if images:
        content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}
                   for mt, data in images] + [{"type": "text", "text": content}]
    body = json.dumps({
        "model": model or settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        log.error("Claude API error %s: %s", e.code, e.read()[:500])
        raise AIUnavailable("The AI service returned an error. Try again in a minute.")
    except (urllib.error.URLError, TimeoutError) as e:
        log.error("Claude API unreachable: %s", e)
        raise AIUnavailable("Couldn't reach the AI service. Try again in a minute.")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIUnavailable("The AI reply couldn't be read. Try again.")
    try:
        return json.loads(m.group(0))
    except ValueError:
        raise AIUnavailable("The AI reply couldn't be read. Try again.")


CAPTURE_SYSTEM = """You turn pasted messages (emails, Teams or WhatsApp messages, meeting notes) into a short to-do list for the person who pasted them. They work for a UK organisation.

Rules:
- Only include actions the reader needs to do or follow up. Skip greetings, sign-offs, FYIs and things already done.
- Each task starts with a verb and is under 80 characters, e.g. "Send the signed contract to Alex".
- Resolve dates relative to today's date given below (UK format, week starts Monday). "Friday" means the next Friday on or after today; "end of month" means the last day of this month. If no date is stated or clearly implied, use null.
- Put useful detail (quantities, names, who asked) in "notes", or "" if none.
- At most 15 tasks.

Reply with JSON only, no prose:
{"items": [{"text": "...", "due": "YYYY-MM-DD" or null, "notes": "..."}]}"""


def capture_todos(text, today=None):
    today = today or date.today()
    reply = _call(CAPTURE_SYSTEM, f"Today is {today:%A %-d %B %Y} ({today.isoformat()}).\n\nPasted text:\n{text}")
    items = []
    for raw in _json(reply).get("items", [])[:15]:
        title = str(raw.get("text") or "").strip()[:250]
        if not title:
            continue
        due = None
        if raw.get("due"):
            try:
                due = date.fromisoformat(str(raw["due"])[:10])
            except ValueError:
                due = None
        items.append({"text": title, "due": due, "notes": str(raw.get("notes") or "").strip()[:2000]})
    return items


STEPS_SYSTEM = """You draft a practical checklist for a task card on a team board in a UK organisation.

Rules:
- 3 to 10 steps, in the order they'd be done.
- Each step starts with a verb and is under 70 characters.
- Be specific to the card; don't pad with generic steps like "Plan the task" or "Review".
- Don't repeat steps that are already on the card's existing checklists.
- Give the checklist a short title (2–4 words).

Reply with JSON only, no prose:
{"title": "...", "steps": ["...", "..."]}"""


def suggest_steps(title, description="", existing=()):
    prompt = f"Card title: {title}\n\nDescription:\n{description or '(none)'}"
    if existing:
        prompt += "\n\nAlready on the card:\n" + "\n".join(f"- {s}" for s in existing[:60])
    data = _json(_call(STEPS_SYSTEM, prompt, max_tokens=800))
    steps = [str(s).strip()[:250] for s in data.get("steps", []) if str(s).strip()][:12]
    if not steps:
        raise AIUnavailable("The AI didn't suggest any steps for this card.")
    return (str(data.get("title") or "Checklist").strip()[:120] or "Checklist"), steps


# ---------- ✨ summarise a card ----------

SUMMARY_SYSTEM = """You summarise a task card from a team board in a UK organisation, for a manager catching up.

Write in plain British English. Be concrete: names, numbers and dates from the card. Don't invent anything that isn't in the card.

Reply with JSON only, no prose:
{"summary": "2–4 sentences: what this is and where it stands now",
 "status": "one of: on track, at risk, blocked, done, not started",
 "next": "the single most useful next step, under 120 characters, or empty if done",
 "open_questions": ["unresolved questions or decisions from the comments, max 3"]}"""


def card_brief(card):
    """Everything about a card, as plain text for the model."""
    from django.utils import timezone
    lines = [f"Title: {card.title}", f"Board: {card.list.board.title} › list “{card.list.title}”",
             f"Complete: {'yes' if card.is_complete else 'no'}"]
    if card.due_at:
        lines.append(f"Due: {timezone.localtime(card.due_at):%a %-d %b %Y %H:%M}")
    if card.start_date:
        lines.append(f"Start: {card.start_date:%-d %b %Y}")
    members = [u.display_name for u in card.members.all()]
    lines.append(f"Members: {', '.join(members) or 'nobody'}")
    labels = [str(l) for l in card.labels.all()]
    if labels:
        lines.append(f"Labels: {', '.join(labels)}")
    for v in card.field_values.all():
        if v.display():
            lines.append(f"{v.field.name}: {v.display()}")
    if card.description:
        lines.append(f"\nDescription:\n{card.description[:3000]}")
    for cl in card.checklists.all():
        items = list(cl.items.all())
        lines.append(f"\nChecklist “{cl.title}” ({sum(i.is_done for i in items)}/{len(items)} done):")
        lines += [f"- [{'x' if i.is_done else ' '}] {i.text}" + (f" (assigned {i.assignee.display_name})" if i.assignee else "")
                  for i in items[:40]]
    comments = list(card.comments.select_related("author").order_by("-created_at")[:40])[::-1]
    if comments:
        lines.append("\nComments (oldest first):")
        lines += [f"- {timezone.localtime(c.created_at):%-d %b %H:%M} {c.author.display_name if c.author else 'Automation'}: "
                  f"{c.body[:600]}" for c in comments]
    acts = list(card.activity.select_related("actor").order_by("-created_at")[:25])[::-1]
    if acts:
        lines.append("\nActivity (oldest first):")
        lines += [f"- {timezone.localtime(a.created_at):%-d %b} {a.actor.display_name if a.actor else 'Automation'} {a.text()}"
                  for a in acts]
    return "\n".join(lines)


def summarise_card(card):
    from django.utils import timezone
    data = _json(_call(SUMMARY_SYSTEM, f"Today is {timezone.localdate():%A %-d %B %Y}.\n\n{card_brief(card)}",
                       max_tokens=700))
    status = str(data.get("status") or "").strip().lower()
    return {
        "summary": str(data.get("summary") or "").strip()[:1200],
        "status": status if status in ("on track", "at risk", "blocked", "done", "not started") else "",
        "next": str(data.get("next") or "").strip()[:200],
        "questions": [str(q).strip()[:200] for q in (data.get("open_questions") or []) if str(q).strip()][:3],
    }


# ---------- ✨ what should I do first? ----------

PRIORITY_SYSTEM = """You help someone in a UK organisation decide what to work on first today.
You get their open cards and checklist items with due dates, lists, labels and progress.

Pick the 5 (or fewer) most important to tackle first. Weigh: overdue and due-soon dates, labels like Urgent,
things others are waiting on (lists like "Waiting"/"Doing", comments), and quick wins that unblock others.
Use only the ids given. Each reason is under 110 characters and concrete (mention the date or the reason).

Reply with JSON only, no prose:
{"picks": [{"id": "c12", "why": "..."}], "note": "one short sentence of overall advice, or empty"}"""


def priorities(entries, today):
    """entries: [{"id": "c12" | "i5", "text": "..."}]. Returns (picks, note)."""
    listing = "\n".join(f"{e['id']}: {e['text']}" for e in entries)
    data = _json(_call(PRIORITY_SYSTEM, f"Today is {today:%A %-d %B %Y}.\n\nOpen work:\n{listing}", max_tokens=700))
    ids = {e["id"] for e in entries}
    picks, seen = [], set()
    for p in data.get("picks") or []:
        pid = str(p.get("id") or "").strip()
        if pid in ids and pid not in seen:
            seen.add(pid)
            picks.append({"id": pid, "why": str(p.get("why") or "").strip()[:160]})
    if not picks:
        raise AIUnavailable("The AI couldn't pick priorities this time. Try again.")
    return picks[:5], str(data.get("note") or "").strip()[:200]


# ---------- ✨ write up a description ----------

WRITE_SYSTEM = """You turn rough notes into a clear card description for a team board in a UK organisation.

Rules:
- Keep every fact from the notes (names, quantities, dates, suppliers). Never invent facts.
- British English, plain and friendly, no corporate fluff.
- Use this light formatting only: **bold** for key facts, "- " bullet lists, and "## " for at most 2 short headings if helpful.
- Usually 40–150 words. If the notes are already clear, just tidy them.
- If the notes are empty, write a short useful description from the title alone and keep it general.

Reply with the description text only — no preamble, no code fences."""


def write_description(title, notes):
    text = _call(WRITE_SYSTEM, f"Card title: {title}\n\nRough notes:\n{notes or '(none)'}", max_tokens=700).strip()
    text = re.sub(r"^```\w*\n?|\n?```$", "", text).strip()
    if not text:
        raise AIUnavailable("The AI didn't write anything. Try again.")
    return text[:6000]


DRAFT_SYSTEM = """You set up boards in Lanes, a Trello-style app. The person pastes or describes what they need to
track — a spreadsheet, a list, an email, a screenshot of another system, or a sentence — and you write the cards
in Lanes' plain-text format so they can check it before anything is created.

The format (follow it exactly; plain text only, no markdown, no commentary):
- Cards are separated by a line containing only "---". Never put blank lines inside a card.
- Optional: a line "# List name" puts the cards after it into that list.
- First line of a card: what it is, then " — ", then how many and a unit, e.g. "AF Christmas Cake — 430 cakes" or
  "Summer fair — 200 guests". If there is no meaningful quantity, just the name.
- Optional field lines straight after the first line, as "Name: value", e.g. "Forecast: 407", "Launch: 6 Oct 2026",
  "Deadline: 12/11/2026", "Notes: one short sentence". Only use facts that are in the input.
- Then sections: a line ending in ":" e.g. "Packaging:", "Make:", "Finish:", "Setup:". Group lines sensibly.
- Lines in a section:
  "1 x Thing" — this many for EACH one of the card's quantity (1 tin per cake, 6 cases per pack of 6).
  "217.417 kg Thing" — a fixed total, with a unit (kg, g, l, ml, each, boxes, packs, sheets, trays, labels…).
  "[x] …" — already done/made/ordered in the input.
  A line with no number — a plain tick, e.g. "Artwork approved".
- Prefer "N x" when the input shows a per-unit amount or the total divides evenly by the card's quantity
  (e.g. 430 labels for 430 cakes -> "1 x Printed Product Label"; 12,000 cases for 2,000 packs -> "6 x Mince Pie Case").
  Otherwise give the total.
- Keep the names and numbers from the input exactly. Never invent quantities; if a quantity is unknown, write the
  line without a number.
- End each card with a short last section for the finishing work when it is obvious (e.g. "Finish:" with "1 x Pack").
- At most 40 cards."""


def draft_cards(text, images=()):
    """Turn anything pasted (text and/or screenshots) into Lanes' card text format."""
    prompt = (text or "").strip() or "Set up the cards shown in the image."
    reply = _call(DRAFT_SYSTEM, f"Today is {date.today():%-d %B %Y}.\n\nWhat to set up:\n{prompt}",
                  max_tokens=6000, images=images, max_input=40000, timeout=55,
                  model=getattr(settings, "ANTHROPIC_DRAFT_MODEL", None))
    reply = re.sub(r"^```[a-z]*\n|\n```$", "", reply.strip())
    return reply.strip()
