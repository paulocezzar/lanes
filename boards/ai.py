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


def _call(system, user_text, max_tokens=1500):
    if not enabled():
        raise AIUnavailable("AI isn't switched on for Lanes yet.")
    body = json.dumps({
        "model": settings.ANTHROPIC_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user_text[:MAX_INPUT]}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
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


CAPTURE_SYSTEM = """You turn pasted messages (emails, Teams or WhatsApp messages, meeting notes) into a short to-do list for the person who pasted them. They work in a bakery and food business in the UK.

Rules:
- Only include actions the reader needs to do or follow up. Skip greetings, sign-offs, FYIs and things already done.
- Each task starts with a verb and is under 80 characters, e.g. "Order 20 cake boards from Kingsmill".
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


STEPS_SYSTEM = """You draft a practical checklist for a task card on a team board in a UK bakery and food business.

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
