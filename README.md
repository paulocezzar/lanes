# Lanes

Boards for every department at The Newt — a Trello-style Kanban app built in Django.

## What's in this version

- **Departments** (workspaces) with their own boards; boards can be department-visible or private
- **Boards, lists and cards** with drag-and-drop (cards between lists, into empty lists, and list reordering), works on iPad/phone
- **Card detail**: title, description, labels, start/due dates (overdue / due-soon colours), checklists with progress bar and hide-checked, members, mark complete, archive/restore, move to list
- **Comments and activity** merged into one feed per card; board-wide activity in the menu
- **Sharing** by email with roles: Admin (manages board and members), Member (edits cards), Observer (views and comments)
- **Guests**: people from other departments can be added to a single board
- **My cards**: everything assigned to you, across all boards
- Star boards, change board background and visibility, archived items list

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

## Next steps

1. Live updates between people on the same board (Django Channels + Redis)
2. Notifications and @mentions
3. Planner view (agenda / week / month of cards with dates)
4. Attachments (files on cards, stored in object storage)
5. Microsoft 365 sign-in
