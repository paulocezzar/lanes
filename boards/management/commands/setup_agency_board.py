"""One-off: fill the 'Agency staff' board with the Christmas agency plan.

Runs from build.sh only when LANES_AGENCY_BOARD holds the board id. Skips a
board that already has a 'Jobs' list, so redeploys change nothing.
"""
import os
from datetime import date, datetime, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from boards.models import (Board, Button, Card, CardFieldValue, Checklist, ChecklistItem, CustomField, Label, List,
                           Rule)

LABELS = {"green": "Lime Hospitality", "blue": "Meridian", "orange": "Night shift", "red": "Urgent",
          "yellow": "Induction", "purple": "Job sheet"}

INDUCTION = ["Agency confirmation + right-to-work check", "Signed in at reception, ID badge issued",
             "Food safety & hygiene briefing (handwashing, illness reporting)", "Allergen briefing for the Christmas range",
             "Health & safety walk-round: fire exits, assembly point, first aid", "PPE and uniform signed out",
             "Shown welfare room, lockers and break times", "Met their buddy and station supervisor",
             "Job sheet for their station read through"]

PREP = [
    ("Confirm numbers, dates and rates with Lime & Meridian", date(2026, 11, 6), ["Urgent"],
     "Agree how many people we need each day of the peak and on the night shift, and what it costs.",
     ["Send numbers needed per day (15–22 Dec)", "Confirm shift times (early / late / night)",
      "Agree hourly rates and night uplift", "Ask for workers' names and start dates by 4 Dec",
      "Confirm cancellation / no-show terms"]),
    ("Night shift 19–22 Dec: HR guidance and pay uplift", date(2026, 11, 13), ["Night shift", "Urgent"],
     "E-commerce want bakery orders around 4am, so a night shift starting 12–1am. Plan for 2–3 people.",
     ["Ask Clare (HR) for guidance", "Agree pay uplift for night workers", "Lone-working check and supervisor cover",
      "Tell the agency the night shift numbers"]),
    ("Induction pack ready", date(2026, 11, 27), ["Induction"],
     "One pack per worker, printed and ready on day one.",
     ["Get the current induction pack template from HR", "Food safety & hygiene briefing sheet", "Allergen briefing sheet",
      "Site health & safety induction sheet", "PPE / uniform sign-out sheet", "Print packs for every worker"]),
    ("PPE and uniforms", date(2026, 12, 1), [],
     "Enough for every agency worker on every shift, plus spares.",
     ["Count sizes needed (whites / tunics)", "Hairnets and beard snoods", "Aprons and blue gloves",
      "Safe footwear rule sent to the agency", "Sign-out sheet ready"]),
    ("Site access and parking", date(2026, 12, 1), [],
     "So people can get in on day one without waiting at the gate.",
     ["Door codes / fobs for each shift", "Parking spaces or where to park", "Where to report on arrival",
      "Fire assembly point on the site map", "Lockers and welfare room"]),
    ("Rota for the peak week", date(2026, 12, 4), [],
     "Who is where, on which shift, each day 15–22 Dec. Copy the result into the Shifts cards.",
     ["Stations per day (packing, boxes, labelling, dispatch, cleaning)", "Early / late / night shifts",
      "Break cover", "Buddy pairs", "Share the rota with both agencies"]),
    ("Food safety and allergen briefing", date(2026, 12, 8), ["Induction"],
     "A short briefing every agency worker gets before they touch product.",
     ["Briefing sheet written", "Allergen matrix for the Christmas range", "Handwashing and illness reporting",
      "Sign-off sheet (keep for audit)"]),
    ("Assign buddies", date(2026, 12, 8), [],
     "Each agency worker is paired with someone from the team who knows the station.",
     ["Pick a buddy per station from the team", "Brief buddies on what to show and check", "Add buddies to the rota"]),
]

JOBS = [
    ("Packing — mince pies and Christmas range", "Packing room", [
        "Wash hands and put on PPE", "Check today's pick list and what you're packing", "Check product looks right and the date code is correct",
        "Pack to spec: right count, right way up, no damaged product", "Seal and label", "Stack on the correct trolley",
        "Record counts on the packing sheet", "Clean down the station at the end"],
     "Counts are exactly right, every pack is labelled straight with the right date, nothing damaged goes out.",
     "Knives and box cutters only as shown. Lift with your knees. Report any allergen spill straight away."),
    ("Box building", "Packing room", [
        "Get the right box size for today's products", "Fold to the spec shown on the station sheet",
        "Check every box is square and the base is locked", "Stack by size in the marked area", "Count and write totals on the sheet"],
     "Boxes are square, strong and ready before packing needs them. No crushed or torn boxes used.",
     "Watch for paper cuts. Keep walkways clear of stacks."),
    ("Labelling", "Packing room", [
        "Check the product and the label match", "Check the best-before date and batch code", "Check allergen information is correct",
        "Apply the label straight and smooth", "Spot-check every 20th pack", "Put any wrong labels in the reject bin and tell the supervisor"],
     "Every pack has the right label, date and allergens. Wrong labels never leave the room.",
     "If you're unsure about a label, stop and ask — never guess on allergens."),
    ("Dispatch and picking", "Dispatch bay", [
        "Pick by order sheet", "Check counts against the order", "Frozen lines: check temperature and keep out of the freezer for minutes only",
        "Load trolleys / crates by route", "Paperwork signed and with the order"],
     "Every order complete and correct, frozen stays frozen, paperwork matches.",
     "Use the trolleys — don't carry heavy crates. Freezer: max time in, PPE on."),
    ("Cleaning / kitchen porter", "Wash-up and production floor", [
        "Wash-up: pans, trays, utensils as they come in", "Floors swept and mopped on the schedule", "Bins emptied and liners replaced",
        "Use chemicals exactly as labelled (COSHH)", "Sign the cleaning schedule after each task"],
     "Kit is clean and back in place before it's needed, floors safe, schedule signed.",
     "Wet-floor signs out when mopping. Never mix chemicals. Gloves and goggles for chemicals."),
]


def at(d, h):
    return timezone.make_aware(datetime.combine(d, time(h, 0)))


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_AGENCY_BOARD", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            self.stdout.write(f"setup_agency_board: no board {board}.")
            return
        if b.lists.filter(title="Jobs", is_archived=False).exists():
            self.stdout.write("setup_agency_board: already set up — skipping.")
            return
        m = b.memberships.filter(role="admin").select_related("user").first()
        owner = m.user if m else None
        with transaction.atomic():
            b.title = "Agency staff — Christmas"
            b.visibility = Board.PRIVATE
            b.description = ("Christmas agency cover: prep before they arrive, a job sheet per station, one card per "
                             "agency worker, and a card per shift in the peak.")
            b.save()
            have = {l.title: l for l in b.lists.filter(is_archived=False)}
            names = ["Prep before they arrive", "Jobs", "People", "Shifts", "Done"]
            lists = {}
            if "Doing" in have and "Jobs" not in have:
                have["Doing"].title = "Jobs"
                have["Doing"].save(update_fields=["title"])
                have["Jobs"] = have.pop("Doing")
            for i, n in enumerate(names, start=1):
                lists[n] = have.get(n) or List.objects.create(board=b, title=n, position=i * 1024)
                lists[n].position = i * 1024
                lists[n].save(update_fields=["position"])
            labels = {}
            for colour, name in LABELS.items():
                lab = Label.objects.filter(board=b, colour=colour).first() or Label(board=b, colour=colour)
                lab.name = name
                lab.save()
                labels[name] = lab

            def field(name, kind, options=(), pos=0):
                return CustomField.objects.create(board=b, name=name, kind=kind, options=list(options), position=pos)
            f_agency = field("Agency", "dropdown", ["Lime Hospitality", "Meridian"], 1)
            f_station = field("Station", "dropdown", ["Packing", "Box building", "Labelling", "Dispatch", "Cleaning / KP"], 2)
            f_shift = field("Shift", "dropdown", ["Early", "Late", "Night"], 3)
            field("Buddy", "text", pos=4)
            f_head = field("Headcount", "number", pos=5)

            def card(lst, title, desc="", due=None, start=None, labs=(), cl_title=None, items=(), template=False, member=True):
                c = Card.objects.create(list=lst, title=title, description=desc, due_at=due, start_date=start,
                                        created_by=owner, is_template=template,
                                        position=(lst.cards.count() + 1) * 1024)
                if owner and member:
                    c.members.add(owner)
                c.labels.add(*[labels[n] for n in labs])
                if cl_title:
                    cl = Checklist.objects.create(card=c, title=cl_title)
                    for i, t in enumerate(items, start=1):
                        ChecklistItem.objects.create(checklist=cl, text=t, position=i * 1024)
                return c

            for title, due, labs, desc, items in PREP:
                card(lists["Prep before they arrive"], title, desc, at(due, 17), labs=labs, cl_title="To do", items=items)

            for title, where, steps, good, safety in JOBS:
                desc = (f"**Where:** {where}\n**Supervisor:** (add the supervisor as a member of this card)\n\n"
                        f"**What good looks like:** {good}\n\n**Safety:** {safety}\n\n"
                        "Print this card or open it on the station tablet. Agency workers follow the steps below.")
                c = card(lists["Jobs"], title, desc, labs=["Job sheet"], cl_title="Steps", items=steps, member=False)
                station = {"Packing": "Packing", "Box": "Box building", "Lab": "Labelling", "Dis": "Dispatch",
                           "Cle": "Cleaning / KP"}
                val = next(v for k, v in station.items() if title.startswith(k))
                CardFieldValue.objects.create(card=c, field=f_station, value=val)

            card(lists["People"], "Agency worker — copy me", (
                "**How to add someone:** click the template icon next to “+ Add a card” in this list, pick this card, then rename the copy to the person's name.\n\n"
                "Fill in **Agency**, **Station**, **Shift** and **Buddy**, set the **start and end dates**, and add their buddy "
                "and supervisor as members. Tick the induction steps on their first morning."),
                labs=["Induction"], cl_title="Induction", items=INDUCTION, template=True, member=False)

            peak = [date(2026, 12, 15) + timedelta(days=i) for i in range(8)]
            for d in peak:
                c = card(lists["Shifts"], f"{d:%a %-d %b} — day shifts", "Who's on, and where. Add each worker as a member.",
                         at(d, 6), start=d, cl_title="Shift checks",
                         items=["Everyone signed in", "Stations covered (see rota)", "Buddies paired", "Breaks covered",
                                "Sign-out sheet done"])
                CardFieldValue.objects.create(card=c, field=f_head, value=0)
            for d in peak[4:]:
                c = card(lists["Shifts"], f"{d:%a %-d %b} — night shift (12–1am start)",
                         "Night shift for e-commerce orders around 4am. 2–3 people, with supervisor cover.",
                         at(d, 0), start=d, labs=["Night shift"], cl_title="Shift checks",
                         items=["Everyone signed in", "Supervisor on site", "Lone-working check", "Orders ready for 4am",
                                "Sign-out sheet done"])
                CardFieldValue.objects.create(card=c, field=f_shift, value="Night")
                CardFieldValue.objects.create(card=c, field=f_head, value=0)

            # automations
            Rule.objects.create(board=b, trigger=Rule.ADDED, trigger_list=lists["People"], created_by=owner,
                                actions=[{"type": "add_checklist", "title": "Induction", "text": "\n".join(INDUCTION)},
                                         {"type": "add_label", "label": labels["Induction"].pk}])
            Rule.objects.create(board=b, trigger=Rule.CHECKLIST_DONE, created_by=owner,
                                filters=[{"type": "in_list", "list": lists["People"].pk}],
                                actions=[{"type": "remove_label", "label": labels["Induction"].pk},
                                         {"type": "comment", "text": "Induction complete ✅"}])
            Rule.objects.create(board=b, trigger=Rule.CHECKLIST_DONE, created_by=owner,
                                filters=[{"type": "in_list", "list": lists["Prep before they arrive"].pk}],
                                actions=[{"type": "move", "list": lists["Done"].pk, "position": "top"}, {"type": "complete"}])
            Rule.objects.create(board=b, trigger=Rule.DUE_SOON, trigger_hours=48, created_by=owner,
                                filters=[{"type": "in_list", "list": lists["Prep before they arrive"].pk}],
                                actions=[{"type": "notify", "text": "is due in 2 days"}])
            Button.objects.create(board=b, kind=Button.CARD, name="Start induction", icon="👋", created_by=owner,
                                  actions=[{"type": "add_checklist", "title": "Induction", "text": "\n".join(INDUCTION)},
                                           {"type": "add_label", "label": labels["Induction"].pk},
                                           {"type": "set_due", "days": 0}])
            Button.objects.create(board=b, kind=Button.CARD, name="Shift done", icon="✅", created_by=owner,
                                  actions=[{"type": "check_all"}, {"type": "complete"},
                                           {"type": "move", "list": lists["Done"].pk, "position": "top"}])
            b.version += 1
            b.save(update_fields=["version"])
        self.stdout.write(f"setup_agency_board: board {b.pk} filled.")
