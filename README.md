# Lanes

Boards for every department at The Newt — a Trello-style Kanban app built in Django.

## What's in Lanes

**Boards** — departments (workspaces) with boards, collections to group them, department or private visibility,
board and card templates, copy board/card, move a card to another board, archive and restore boards, lists
and cards (archived cards can be deleted for good), export to CSV/JSON.

**Cards** — drag-and-drop lists and cards (desktop and iPad), labels, start/due dates, checklists with per-item
assignee and due date, members, description, custom fields (text, number, date, checkbox, dropdown, location),
attachments on R2 with image covers, colour covers, stickers, comments with @mentions and emoji reactions,
light formatting (**bold**, *italic*, lists, links) in descriptions and comments, activity, mirroring a card
onto other lists/boards (cross-department work), 👁 watching cards and boards.

**Views** — Board, Calendar (drag to reschedule), Table (sort/filter), Timeline (Gantt), Dashboard, Map (cards
with a location field; UK postcodes via postcodes.io, other places via OpenStreetMap Nominatim) — per board
or across all boards (filter by department/collection). Board filter bar (people, due, labels, hide complete).

**For each person** — Planner (week of your cards, checklist items and inbox; drag onto a day), private Inbox
(paste a list, or ✨ AI capture from an email), My cards, search with operators and saved searches,
notifications bell (assigned, mentioned, comments, due soon), light/dark/system theme.

**Automation (⚡)** — rules (card added/moved/completed/checklist done/label added → move, move to top, complete,
tick all, labels, members, due date, comment, sticker, archive), recurring cards, and 🔘 buttons — card buttons
(several actions in one click on any card) and board buttons (create a card, move every card between lists,
archive complete cards, sort a list by due date).

**Power-ups** (per board, Menu → Settings) — 👍 voting on cards, 🍂 card ageing (untouched cards fade after
1, 2 and 4 weeks), and a read-only public link (no sign-in; comments, attachments and members are never shown).

**Lists** — colours, card limits (red count when over), collapse.

**People** — departments managed from the front end; temporary passwords; Admin/Member/Observer board roles;
people from other departments; external guests (suppliers, contractors) invited from a board's Share dialog —
they only ever see boards they're added to and can't be board admins.

**Email** — invites with a set-your-own-password link, “Forgot your password?”, notification emails (as they
happen — grouped every few minutes and only for things not already seen — or a 7am summary, or off; each person
chooses in their profile menu).

**Microsoft sign-in** — “Sign in with Microsoft” for staff (Entra ID); optional automatic accounts for anyone
in the directory; “Required” mode turns off Lanes passwords for staff.

**Keyboard shortcuts** — press `?` in Lanes for the list (arrows to move between cards, `n` new card, `space`
add me, `l`/`d`/`m` labels/dates/members, `1`–`9` labels, `c` archive, `/` search…).

**Company admins** — Company → Usage (sign-ins, active people per department, open/overdue cards, busiest and
quiet boards, never-signed-in) and Audit log (sign-ins and failed sign-ins, people and role changes, password
resets, sharing, guests, public links, board visibility/archive; filter and CSV download).

**Security** — two-step sign-in with an authenticator app (recovery codes; the Django admin sign-in goes through
it too). Company → Settings can require it and decides who may turn on public links, invite guests, create
boards and make boards private. Company → People lists every account, resets two-step and switches leavers off.

**Due-date rules** — "a card is due within 1 hour…1 week" and "a card becomes overdue" triggers, plus a
"notify its members" action. Checked about once a minute (or by the `run_automations` cron job), once per card
per due date.

**AI (optional)** — set `ANTHROPIC_API_KEY` to enable ✨ inbox capture, ✨ Suggest steps, ✨ Summarise (card +
comments), ✨ Write it up (descriptions) and ✨ What should I do first? on My cards (Claude Haiku;
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

All of these go on the **lanes** web service (Render → lanes → Environment).

| Variable | Purpose |
| --- | --- |
| `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Attachments in Cloudflare R2 / S3 |
| `ANTHROPIC_API_KEY` (`ANTHROPIC_MODEL`) | The ✨ AI features |
| `MS_TENANT_ID`, `MS_CLIENT_ID`, `MS_CLIENT_SECRET` | Sign in with Microsoft (see below) |
| `MS_MAIL_SENDER` | Send email through Microsoft 365 from this mailbox, e.g. `lanes@thenewt.co.uk` |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | …or send email through any SMTP service instead |
| `SITE_URL` | Public address used in email links (set to `https://lanes.onrender.com` by the blueprint) |
| `CRON_TOKEN` | Generated by the blueprint; the `lanes-automations` cron job uses it |
| `ATTACHMENT_MAX_MB` | Upload limit (default 25) |

The `lanes-automations` cron job calls `/internal/run-automations/` every 5 minutes: recurring cards, due
reminders, due-date rules and queued email (invites, notification emails, the 7am summary).

## Microsoft sign-in (and email through Microsoft 365)

Someone with admin rights in Microsoft Entra (usually IT) does this once:

1. **entra.microsoft.com → Applications → App registrations → New registration.**
   Name: `Lanes`. Supported account types: *Accounts in this organizational directory only*.
   Redirect URI: platform **Web**, `https://lanes.onrender.com/login/microsoft/callback/`. Register.
2. On the app's **Overview**, copy the *Application (client) ID* → `MS_CLIENT_ID` and the
   *Directory (tenant) ID* → `MS_TENANT_ID`.
3. **Certificates & secrets → New client secret** (24 months). Copy the *Value* → `MS_CLIENT_SECRET`.
   Put a reminder in the calendar to renew it before it expires.
4. **API permissions**: `openid`, `profile`, `email` (Microsoft Graph, delegated) are enough for sign-in —
   click *Grant admin consent*.
5. *For email:* add Microsoft Graph **application** permission `Mail.Send`, grant admin consent, create a
   mailbox such as `lanes@thenewt.co.uk` and set `MS_MAIL_SENDER` to it. Ask IT to limit the app to that one
   mailbox with an *application access policy* (Exchange) so it can't send as anyone else.
6. Add the variables on Render. In Lanes: **Company → Settings → Sign in with Microsoft** (Allowed or
   Required) and whether anyone in the directory may sign in. Use **Send me a test email** to check email.

Notes: Microsoft does its own two-step check, so Lanes doesn't ask again after a Microsoft sign-in. With
“Required”, staff can't use Lanes passwords — only guests and company admins (as a break-glass) can — so
someone disabled in Microsoft can't sign in again. Their open Lanes session ends when it expires (2 weeks)
or straight away if you **Switch off** the account in Company → People.

## Next steps

1. Email notifications and invite links (needs an email service)
2. Microsoft 365 sign-in
3. Install-to-home-screen (PWA) and push notifications on phones
