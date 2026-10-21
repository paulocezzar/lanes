# Lanes

Boards for every department at The Newt — a Trello-style Kanban app built in Django.

## What's in Lanes

**Boards** — departments (workspaces) with boards, collections to group them, department or private visibility,
board and card templates, copy board/card, archive, export to CSV/JSON.

**Cards** — drag-and-drop lists and cards (desktop and iPad), labels, start/due dates, checklists with per-item
assignee and due date, members, description, custom fields (text, number, date, checkbox, dropdown, location),
attachments on R2 with image covers, colour covers, stickers, comments with @mentions, activity, mirroring a card
onto other lists/boards (cross-department work).

**Views** — Board, Calendar (drag to reschedule), Table (sort/filter), Timeline (Gantt), Dashboard — per board
or across all boards (filter by department/collection). Board filter bar (people, due, labels, hide complete).

**For each person** — Planner (week of your cards, checklist items and inbox; drag onto a day), private Inbox
(paste a list, or ✨ AI capture from an email), My cards, search with operators and saved searches,
notifications bell (assigned, mentioned, comments, due soon), light/dark/system theme.

**Automation (⚡)** — rules (card added/moved/completed/checklist done/label added → move, move to top, complete,
tick all, labels, members, due date, comment, sticker, archive) and recurring cards.

**Lists** — colours, card limits (red count when over), collapse.

**People** — departments managed from the front end; temporary passwords; Admin/Member/Observer board roles;
guests from other departments.

**AI (optional)** — set `ANTHROPIC_API_KEY` to enable ✨ inbox capture and ✨ Suggest steps (Claude Haiku;
`ANTHROPIC_MODEL` to override). Card/message text is sent to Anthropic only when someone uses these buttons.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo        # demo departments/users, password: lanesdemo
python manage.py runserver
```

Log in as `paulo@example.com` / `lanesdemo` (superuser). Run tests with `python manage.py test`.

## Departments and people

- Company admins (superusers) create departments with **New department** on the Boards page.
- Each department has a **People** page. Department admins add people by email; anyone without an account gets one with a temporary password, shown once on screen. They can change it from their profile menu.
- Department admins can change roles, remove people and issue a new temporary password.
- Board admins share individual boards by email, including with people from other departments.

## Deploy to Render

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo. `render.yaml` creates the `lanes` web service and the `lanes-db` PostgreSQL database.
3. After the first deploy, open the service **Shell** and run `python manage.py createsuperuser`.
4. Never run `seed_demo` on production.

## How it works

- `boards/models.py` — Workspace → Board → List → Card, plus labels, checklists, comments and an append-only `Activity` log
- `boards/positions.py` — float positions: a drop between two cards takes the midpoint, so one row is written; the list is re-spaced when gaps get too small
- `boards/permissions.py` — view / edit / comment / manage rules in one place; private boards 404 for outsiders
- `boards/api.py` — JSON endpoints; every write is permission-checked and returns freshly rendered HTML
- `static/lanes/board.js` — drag-and-drop (SortableJS, vendored) and the card dialog

## Optional environment variables

| Variable | Purpose |
| --- | --- |
| `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Attachments in Cloudflare R2 / S3 |
| `ANTHROPIC_API_KEY` (`ANTHROPIC_MODEL`) | AI capture and Suggest steps |
| `ATTACHMENT_MAX_MB` | Upload limit (default 25) |

Recurring cards and due reminders run automatically while people use Lanes. For exact timing, add a Render
cron job running `python manage.py run_automations` every 15 minutes.

## Next steps

1. Email notifications and invite links (needs an email service)
2. Microsoft 365 sign-in
3. Install-to-home-screen (PWA) and push notifications on phones
