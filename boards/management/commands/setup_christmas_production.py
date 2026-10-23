"""Fill the 'Christmas Production' board from the StockUp Christmas 2026 plan
(locked 16 Aug, forecast v2: 11 products, 26 components, 3,458 kg).

One card per product, like StockUp's Overview: inside each card a 'Components to make'
checklist, a 'Packaging' checklist and a 'Production' step, every item with its quantity,
code, StockUp warning and a draft due date. Shared packaging is listed on each product
with its share, and once more — as one combined order — on the 'Packaging orders' card.
Artwork is left out (handled outside production). Dates are a first draft for the Gantt.
Runs only when LANES_XMAS_BOARD holds the board id and the board has no cards.
"""
import os
from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from boards.models import Board, Card, CardFieldValue, Checklist, ChecklistItem, CustomField, Label, List

D = lambda m, d: date(2026, m, d)  # noqa: E731

LISTS = [("prep", "To prepare", "orange"), ("making", "In production", "blue"), ("done", "Done", "green")]

LABELS = {
    "cake": ("Christmas cake", "green"), "mince": ("Mince pies", "orange"), "yule": ("Yule log", "purple"),
    "tarts": ("Sweet tarts", "pink"), "savoury": ("Savoury tartlets", "sky"), "stollen": ("Stollen", "yellow"),
    "puff": ("Rough puff", "grey"), "pack": ("Packaging", "blue"), "attention": ("Needs attention", "red"),
}

ORDER = D(10, 27)   # draft: all packaging ordered by Tue 27 Oct
WARN = "⚠ "         # items StockUp flags red or amber

# item: (text, due, done, warning)
PRODUCTS = [
    dict(key="crumble", title="Crumble Topped Mince Pies Retail (Pack/6)", label="mince", qty=2000, unit="packs",
         forecast=1504, launch=D(10, 6), why="account for wholesale demands and internal orders", end=D(12, 4),
         components=[
             ("Mincemeat (Mince Pie) — 600.168 kg · NPD-R412", D(10, 30), False, "Short — window passed (24 May – 6 Oct)"),
             ("Crumble Topping — 180.05 kg · NPD-R567", D(10, 27), False, "Short — window passed (7 Aug – 6 Oct)"),
             ("Hazelnut Praline — 24.007 kg (also 10 kg for the Dark Choc tarts)", D(10, 26), False, "Past last safe start (23 Jul – 6 Oct)"),
             ("Mince Pie Case — 12,000 (also 3,600 for Cranberry & Orange)", D(11, 6), False, "Short — window passed (scheduled 6 Oct)"),
         ],
         packaging=[
             ("0427 die-cut self-locking box c/w 6 cell — 2,000 · NPD-P91 (ordered)", ORDER, True, ""),
             ("Greaseproof paper 230×450 40gsm — 2,000 · NPD-P94", ORDER, False, "No lead time"),
             ("Thermal Label — 2,000 (on the shared order of 3,350)", ORDER, False, "No lead time"),
             ("Printed Product Label — 2,000 (on the shared order of 3,460)", ORDER, False, "No lead time"),
             ("Vacuum Pouch 400×500mm — 12,000 (on the shared order of 12,270)", ORDER, False, ""),
         ],
         production=[("Bake & pack — 2,000 packs of 6", D(12, 4))]),
    dict(key="cran", title="Cranberry and Orange Mince Pie (Pack/6)", label="mince", qty=600, unit="packs",
         forecast=477, launch=D(10, 6), why="buffer", end=D(11, 13),
         components=[
             ("Cranberry and Orange Mincemeat — 180.011 kg · NPD-R2840", D(10, 29), False, "No window"),
             ("Mince Pie Case — 3,600 (made with the Crumble Topped cases)", D(11, 6), False, "Short — window passed (scheduled 6 Oct)"),
         ],
         packaging=[
             ("0427 die-cut self-locking box c/w 6 cell — quantity not captured", ORDER, False, "No quantity in StockUp"),
             ("Greaseproof paper 230×450 40gsm — quantity not captured", ORDER, False, "No quantity in StockUp"),
             ("Printed Product Label (NPD-P2) — quantity not captured", ORDER, False, "No quantity in StockUp"),
             ("Thermal Label (NPD-P32) — quantity not captured", ORDER, False, "No quantity in StockUp"),
             ("Custard Foil 78mm — 3,600 (on the shared order of 6,840)", ORDER, False, "No lead time"),
         ],
         production=[("Bake & pack — 600 packs of 6", D(11, 13))]),
    dict(key="af", title="AF Christmas Cake", label="cake", qty=430, unit="cakes",
         forecast=407, launch=D(10, 6), why="buffer", end=D(11, 13),
         components=[
             ("Christmas Cake Batter revised (2026) — 387 kg (493,000 g made)", None, True, ""),
             ("Fruit Soak revised (Christmas Cake) 2026 — 217.417 kg (267,500 g made)", None, True, ""),
             ("AF Christmas Cake (Baked) — 430 · NPD-R2822 (374,640 g made)", None, True, ""),
         ],
         packaging=[
             ("Printed Product Label — 430 (on the shared order of 3,460)", ORDER, False, "No lead time"),
             ("5mm Black Round Matt Masonite Board (8\") — 430 (on the shared order of 910)", ORDER, False, "No lead time"),
             ("Belly Band — 430 (on the shared order of 1,030)", ORDER, False, "No lead time"),
             ("Cake/Biscuit Tin — 430 (on the shared order of 640)", ORDER, False, "No lead time"),
         ],
         production=[("Decorate & pack — 430 cakes", D(11, 13))]),
    dict(key="mini", title="Miniature Iced Christmas Cake 150g (Pack/1)", label="cake", qty=280, unit="cakes",
         forecast=266, launch=D(10, 6), why="account for possible wastage", end=D(11, 6),
         components=[
             ("Christmas Cake Batter revised (2026) — 39.2 kg (made)", None, True, ""),
             ("Fruit Soak revised (Christmas Cake) 2026 — 22.023 kg (made)", None, True, ""),
             ("Mini Xmas Cake 2026 (Single) — 280 · NPD-R2810", D(10, 27), False, "No window"),
         ],
         packaging=[
             ("700/350mm 20mic centre fold film — 280 (on the shared order of 1,090)", ORDER, False, "No lead time"),
             ("Printed Product Label — 280 (on the shared order of 3,460)", ORDER, False, "No lead time"),
         ],
         production=[("Ice & pack — 280 cakes", D(11, 6))]),
    dict(key="tarts", title="Dark Chocolate and Salted Caramel Tarts (Pack/2)", label="tarts", qty=250, unit="packs",
         forecast=208, launch=D(10, 6), why="account for wholesale and internal orders", end=D(10, 30),
         components=[
             ("Sweet Pastry Tart Case — 65 kg · NPD-R174", D(10, 27), False, "No window"),
             ("Hazelnut Praline — 10 kg (made with the Crumble Topped praline)", D(10, 26), False, "Past last safe start (23 Jul – 6 Oct)"),
             ("Chocolate Icing — 85 kg · NPD-R1441", D(10, 28), False, "No window"),
             ("Salt Caramel — 35 kg · NPD-R2544", D(10, 28), False, "No window"),
         ],
         packaging=[],
         production=[("Assemble & pack — 250 packs of 2", D(10, 30))]),
    dict(key="wreath", title="Stollen Wreath (Tinned)", label="stollen", qty=210, unit="wreaths",
         forecast=190, launch=D(10, 6), why="account for possible wastage", end=D(11, 3),
         components=[
             ("Fruit Soak (Stollen) — 72.531 kg", D(10, 27), False, "No window"),
             ("Stollen Poolish — 27.111 kg", D(10, 28), False, "No window"),
             ("Stollen Dough — 155.554 kg", D(10, 30), False, "No window"),
             ("Stollen Butter Glaze — 7.35 kg", D(10, 30), False, "No window"),
         ],
         packaging=[
             ("5mm Black Round Matt Masonite Board (8\") — 210 (on the shared order of 910)", ORDER, False, "No lead time"),
             ("700/350mm 20mic centre fold film — 210 (on the shared order of 1,090)", ORDER, False, "No lead time"),
             ("Cake/Biscuit Tin — 210 (on the shared order of 640)", ORDER, False, "No lead time"),
             ("Printed Product Label — 210 (on the shared order of 3,460)", ORDER, False, "No lead time"),
             ("Thermal Label — 210 (on the shared order of 3,350)", ORDER, False, "No lead time"),
         ],
         production=[("Bake, glaze & tin — 210 wreaths", D(11, 3))]),
    dict(key="tn", title="TN Stollen 600g", label="stollen", qty=600, unit="loaves",
         forecast=510, launch=D(10, 6), why="account for possible increase in orders", end=D(11, 6),
         components=[
             ("Fruit Soak (Stollen) — 139.882 kg", D(10, 27), False, "No window"),
             ("Stollen Poolish — 52.286 kg", D(10, 28), False, "No window"),
             ("Stollen Dough — 300 kg", D(10, 30), False, "No window"),
             ("Stollen Butter Glaze — 24 kg", D(10, 30), False, "No window"),
         ],
         packaging=[
             ("700/350mm 20mic centre fold film — 600 (on the shared order of 1,090)", ORDER, False, "No lead time"),
             ("Belly Band — 600 (on the shared order of 1,030)", ORDER, False, "No lead time"),
             ("Panibois Duc baking basket (175×110) — 600 · NPD-P15", ORDER, False, "No lead time"),
             ("Thermal Label — 600 (on the shared order of 3,350)", ORDER, False, "No lead time"),
         ],
         production=[("Bake, glaze & pack — 600 loaves", D(11, 6))]),
    dict(key="puff", title="Rough Puff - 1.5kg", label="puff", qty=300, unit="blocks",
         forecast=300, launch=date(2026, 8, 24), why="", end=D(10, 29),
         components=[("New Rough Puff Pastry Dough 2025 — 450 kg · NPD-R1868", D(10, 28), False, "No window")],
         packaging=[],
         production=[("Portion & pack — 300 × 1.5 kg", D(10, 29))]),
    dict(key="yule", title="Black Forest Yule Log (1000g)", label="yule", qty=270, unit="logs",
         forecast=256, launch=D(11, 3), why="buffer", end=D(11, 2),
         components=[
             ("Yule Log Batter — 92.571 kg · NPD-R2836", D(10, 27), False, "No window"),
             ("Yule Log Baked Sheet — 81 kg · NPD-R2839", D(10, 28), False, "No window"),
             ("Chocolate Buttercream — 148.5 kg · NPD-R2837", D(10, 29), False, "No window"),
         ],
         packaging=[
             ("5mm Black Round Matt Masonite Board (8\") — 270 (on the shared order of 910)", ORDER, False, ""),
             ("Vacuum Pouch 400×500mm — 270 (on the shared order of 12,270)", ORDER, False, ""),
         ],
         production=[("Roll, finish & pack — 270 logs", D(11, 2))]),
    dict(key="goat", title="FRZN Goat's Cheese, Caramelised Onion & Thyme Tartlets (Box of 6)", label="savoury", qty=200,
         unit="boxes", forecast=184, launch=D(11, 3), why="account for possible wastage", end=D(10, 30),
         components=[
             ("Caramelised Onions w Thyme — 18 kg · NPD-R2174", D(10, 26), False, "No window"),
             ("Mini Savoury Shortcrust Tart Case — 200 (made with the Ham Hock cases)", D(10, 27), False, "No window"),
             ("Savoury Custard — 200 (made with the Ham Hock custard)", D(10, 28), False, "No window"),
             ("Mini Onion & Waterlip Tartlet (Single) — 1,200 · NPD-R2263", D(10, 29), False, "No window"),
         ],
         packaging=[
             ("Custard Foil 78mm — 1,200 (on the shared order of 6,840)", ORDER, False, ""),
             ("Printed Product Label — 200 (on the shared order of 3,460)", ORDER, False, ""),
             ("Thermal Label — 200 (on the shared order of 3,350)", ORDER, False, ""),
         ],
         production=[("Bake, box & freeze — 200 boxes of 6", D(10, 30))]),
    dict(key="ham", title="FRZN Ham Hock, Cheddar & Mustard Tartlets (Box of 6)", label="savoury", qty=340, unit="boxes",
         forecast=310, launch=D(11, 3), why="account for possible wastage", end=D(11, 2),
         components=[
             ("Ham Hock, Mustard & Parsley Filling — 2,040 · NPD-R2360", D(10, 28), False, "No window"),
             ("Mini Savoury Shortcrust Tart Case — 340", D(10, 27), False, "No window"),
             ("Savoury Custard — 340", D(10, 28), False, "No window"),
             ("Mini Ham Hock, Cheddar & English Mustard tartlet (Single) — 2,040 · NPD-R2264", D(10, 30), False, "No window"),
         ],
         packaging=[
             ("Custard Foil 78mm — 2,040 (on the shared order of 6,840)", ORDER, False, ""),
             ("Printed Product Label — 340 (on the shared order of 3,460)", ORDER, False, ""),
             ("Thermal Label — 340 (on the shared order of 3,350)", ORDER, False, ""),
         ],
         production=[("Bake, box & freeze — 340 boxes of 6", D(11, 2))]),
]

# One order each for shared packaging — what the buyer actually places. (text, warning, done)
COMBINED = [
    ("Printed Product Label — 3,460 (6 products)", "No lead time", False),
    ("Thermal Label — 3,350 (5 products)", "No lead time", False),
    ("Vacuum Pouch 400×500mm — 12,270 (Crumble Topped 12,000 + Yule Log 270)", "No lead time", False),
    ("Custard Foil 78mm — 6,840 (Cranberry & Orange 3,600 + Ham Hock 2,040 + Goat's Cheese 1,200)", "No lead time", False),
    ("700/350mm 20mic centre fold film — 1,090 (Mini Cake 280 + Wreath 210 + TN Stollen 600)", "No lead time", False),
    ("Belly Band — 1,030 (AF Cake 430 + TN Stollen 600)", "No lead time", False),
    ("5mm Black Round Matt Masonite Board (8\") — 910 (AF Cake 430 + Yule Log 270 + Wreath 210)", "No lead time", False),
    ("Cake/Biscuit Tin — 640 (AF Cake 430 + Wreath 210)", "No lead time", False),
    ("0427 die-cut box c/w 6 cell — 2,000 for Crumble Topped (ordered)", "", True),
    ("0427 die-cut box c/w 6 cell — Cranberry & Orange (qty not captured)", "No quantity", False),
    ("Greaseproof paper 230×450 — 2,000 Crumble Topped + Cranberry & Orange (qty not captured)", "No lead time", False),
    ("Printed Product Label NPD-P2 & Thermal Label NPD-P32 for Cranberry & Orange — qty not captured", "No quantity", False),
    ("Panibois Duc baking basket — 600 (TN Stollen)", "No lead time", False),
]


def _at(d, tz):
    return timezone.make_aware(datetime.combine(d, time(17)), tz) if d else None


def _items(cl, items, tz, owner):
    for i, (text, due, done, warn) in enumerate(items):
        ChecklistItem.objects.create(
            checklist=cl, text=(f"{text} — {WARN}{warn}" if warn else text)[:250], position=(i + 1) * 1000,
            is_done=done, done_by=owner if done else None, done_at=timezone.now() if done else None,
            due_at=None if done else _at(due, tz), reminder_minutes=-1)


def wipe(b):
    with transaction.atomic():
        Card.objects.filter(list__board=b).delete()
        b.lists.all().delete()
        b.labels.all().delete()
        b.custom_fields.all().delete()


def build(b):
    m = b.memberships.filter(role="admin").select_related("user").first()
    owner = m.user if m else get_user_model().objects.filter(is_superuser=True).first()
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    n = 0
    with transaction.atomic():
        lists = {k: List.objects.create(board=b, title=t, colour=c, position=(i + 1) * 1000)
                 for i, (k, t, c) in enumerate(LISTS)}
        labels = {k: Label.objects.create(board=b, name=nm, colour=c) for k, (nm, c) in LABELS.items()}
        fields = {}
        for i, (name, kind, show) in enumerate([("Planned", "number", True), ("Unit", "text", True),
                                                 ("Forecast", "number", False), ("Variance", "text", False),
                                                 ("Launch", "date", True)]):
            fields[name] = CustomField.objects.create(board=b, name=name, kind=kind, show_on_card=show, position=i + 1)

        # combined packaging orders first
        c = Card.objects.create(list=lists["prep"], title="Packaging orders — combined", position=500, created_by=owner,
                                start_date=D(10, 26), due_at=_at(ORDER, tz), reminder_minutes=-1,
                                description="Shared packaging as one order each — the totals of every product that "
                                            "uses it. Each product card lists its own share.\n\n"
                                            "Artwork is handled outside production.")
        c.labels.set([labels["pack"], labels["attention"]])
        cl = Checklist.objects.create(card=c, title="Orders to place", position=1000)
        _items(cl, [(t, ORDER, done, w) for t, w, done in COMBINED], tz, owner)
        n += 1

        for i, p in enumerate(sorted(PRODUCTS, key=lambda p: (p["launch"], p["end"]))):
            open_items = [x for x in p["components"] + p["packaging"] if not x[2]]
            dates = [d for _, d, _, _ in open_items if d]
            warns = sum(1 for x in open_items if x[3])
            var = p["qty"] - p["forecast"]
            demand = (f"**Demand** — forecast v2 (post-reviewed): {p['forecast']:,} · planned {p['qty']:,} · "
                      + (f"variance {var:+,} ({var / p['forecast'] * 100:+.1f}%) — {p['why']}" if var else "matches forecast"))
            launch = f"**Launch** {p['launch'].strftime('%-d %b %Y')}" + (" — already on sale" if p["launch"] <= today else "")
            flag = f"{WARN}{warns} item{'s' if warns != 1 else ''} flagged in StockUp — see the ⚠ lines.\n\n" if warns else ""
            c = Card.objects.create(
                list=lists["prep"], title=p["title"], position=(i + 1) * 1000, created_by=owner,
                start_date=min(dates) if dates else None, due_at=_at(p["end"], tz), reminder_minutes=-1,
                description=f"{demand}\n{launch}\n\n{flag}"
                            "Dates are a first draft — change them on the checklist items, or drag the bar on the Gantt.")
            c.labels.set([labels[p["label"]]] + ([labels["attention"]] if warns else []))
            for name, val in (("Planned", p["qty"]), ("Unit", p["unit"]), ("Forecast", p["forecast"]),
                              ("Variance", f"{var:+,} ({var / p['forecast'] * 100:+.1f}%)" if var else "—"),
                              ("Launch", p["launch"].isoformat())):
                CardFieldValue.objects.create(card=c, field=fields[name], value=val)
            pos = 1000
            for title, items in (("Components to make", p["components"]),
                                 ("Packaging", p["packaging"]),
                                 ("Production", [(t, d, False, "") for t, d in p["production"]])):
                if not items:
                    continue
                cl = Checklist.objects.create(card=c, title=title, position=pos)
                pos += 1000
                _items(cl, items, tz, owner)
            n += 1

        b.is_project = True
        b.project_owner = owner
        b.rag = "auto"
        b.status_note = ("From the StockUp Christmas 2026 plan (locked 16 Aug, forecast v2): 11 products, 26 components, "
                         "3,458 kg. Selling 24 Aug – 2 Jan. Dates are a first draft.")
        b.status_updated_at = timezone.now()
        b.save()
    return n


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_XMAS_BOARD", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            return
        if Card.objects.filter(list__board=b).exists():
            self.stdout.write("setup_christmas_production: board already has cards — skipping.")
            return
        self.stdout.write(f"setup_christmas_production: {build(b)} cards on board {b.pk}.")
