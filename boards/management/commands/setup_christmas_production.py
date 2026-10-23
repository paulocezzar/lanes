"""Fill the 'Christmas Production' board from the StockUp Christmas 2026 plan
(locked 16 Aug, forecast v2: 11 products, 26 components, 3,458 kg).

Each product is written the way a person would type it into 'New product' — the product
and planned quantity, then what it needs — and turned into a card whose checklist lines
track needed vs done. Dates are a first draft.
Runs only when LANES_XMAS_BOARD holds the board id and the board has no cards.
"""
import os
from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from boards import products
from boards.models import Board, Card, CustomField, Label, List

D = lambda m, d: date(2026, m, d)  # noqa: E731

LISTS = [("prep", "To prepare", "orange"), ("making", "In production", "blue"), ("done", "Done", "green")]
LABELS = {
    "cake": ("Christmas cake", "green"), "mince": ("Mince pies", "orange"), "yule": ("Yule log", "purple"),
    "tarts": ("Sweet tarts", "pink"), "savoury": ("Savoury tartlets", "sky"), "stollen": ("Stollen", "yellow"),
    "puff": ("Rough puff", "grey"),
}

# (label, forecast, launch, reason, start, end, text)
PRODUCTS = [
    ("cake", 407, D(10, 6), "buffer", D(10, 26), D(11, 13), """AF Christmas Cake — 430 cakes
Packaging:
1 x Printed Product Label artwork
1 x Printed Product Label
1 x 5mm Black Round Matt Masonite Board (8")
1 x Cake/Biscuit Tin
1 x Belly Band
Make:
[x] 217.417 kg Fruit Soak revised (Christmas Cake) 2026
[x] 387 kg Christmas Cake Batter revised (2026)
[x] 1 x AF Christmas Cake (Baked)
Finish:
1 x Decorate
1 x Pack"""),
    ("cake", 266, D(10, 6), "account for possible wastage", D(10, 26), D(11, 6), """Miniature Iced Christmas Cake 150g — 280 cakes
Packaging:
1 x Printed Product Label artwork
1 x Printed Product Label
1 x 700/350mm 20mic centre fold film
Make:
[x] 22.023 kg Fruit Soak revised (Christmas Cake) 2026
[x] 39.2 kg Christmas Cake Batter revised (2026)
1 x Mini Xmas Cake 2026 (Single)
Finish:
1 x Ice
1 x Pack"""),
    ("mince", 1504, D(10, 6), "account for wholesale demands and internal orders", D(10, 26), D(12, 4),
     """Crumble Topped Mince Pies Retail — 2000 packs
Packaging:
1 x 0427 die-cut box artwork
[x] 1 x 0427 die-cut self-locking box c/w 6 cell
1 x Thermal Label artwork
1 x Thermal Label
1 x Printed Product Label
1 x Greaseproof paper 230×450 40gsm
6 x Vacuum Pouch 400×500mm
Make:
600.168 kg Mincemeat (Mince Pie)
180.05 kg Crumble Topping
24.007 kg Hazelnut Praline
6 x Mince Pie Case
Finish:
6 x Bake mince pies
1 x Pack (6 per pack)"""),
    ("mince", 477, D(10, 6), "buffer", D(10, 26), D(11, 13), """Cranberry and Orange Mince Pie — 600 packs
Packaging:
1 x 0427 die-cut self-locking box c/w 6 cell
1 x Greaseproof paper 230×450 40gsm
1 x Printed Product Label (NPD-P2)
1 x Thermal Label (NPD-P32)
6 x Custard Foil 78mm
Make:
180.011 kg Cranberry and Orange Mincemeat
6 x Mince Pie Case
Finish:
6 x Bake mince pies
1 x Pack (6 per pack)"""),
    ("tarts", 208, D(10, 6), "account for wholesale and internal orders", D(10, 26), D(10, 30),
     """Dark Chocolate and Salted Caramel Tarts — 250 packs
Make:
65 kg Sweet Pastry Tart Case
10 kg Hazelnut Praline
85 kg Chocolate Icing
35 kg Salt Caramel
Finish:
2 x Assemble tarts
1 x Pack (2 per pack)"""),
    ("stollen", 190, D(10, 6), "account for possible wastage", D(10, 26), D(11, 3), """Stollen Wreath (Tinned) — 210 wreaths
Packaging:
1 x 5mm Black Round Matt Masonite Board (8")
1 x 700/350mm 20mic centre fold film
1 x Cake/Biscuit Tin
1 x Printed Product Label
1 x Thermal Label
Make:
72.531 kg Fruit Soak (Stollen)
27.111 kg Stollen Poolish
155.554 kg Stollen Dough
7.35 kg Stollen Butter Glaze
Finish:
1 x Bake
1 x Glaze & tin"""),
    ("stollen", 510, D(10, 6), "account for possible increase in orders", D(10, 26), D(11, 6), """TN Stollen 600g — 600 loaves
Packaging:
1 x 700/350mm 20mic centre fold film
1 x Belly Band
1 x Panibois Duc baking basket (175×110)
1 x Thermal Label
Make:
139.882 kg Fruit Soak (Stollen)
52.286 kg Stollen Poolish
300 kg Stollen Dough
24 kg Stollen Butter Glaze
Finish:
1 x Bake
1 x Glaze & pack"""),
    ("puff", 300, date(2026, 8, 24), "", D(10, 26), D(10, 29), """Rough Puff 1.5kg — 300 blocks
Make:
450 kg New Rough Puff Pastry Dough 2025
Finish:
1 x Portion & pack"""),
    ("yule", 256, D(11, 3), "buffer", D(10, 27), D(11, 2), """Black Forest Yule Log (1000g) — 270 logs
Packaging:
1 x 5mm Black Round Matt Masonite Board (8")
1 x Vacuum Pouch 400×500mm
Make:
92.571 kg Yule Log Batter
81 kg Yule Log Baked Sheet
148.5 kg Chocolate Buttercream
Finish:
1 x Roll & finish
1 x Pack"""),
    ("savoury", 184, D(11, 3), "account for possible wastage", D(10, 26), D(10, 30),
     """FRZN Goat's Cheese, Caramelised Onion & Thyme Tartlets — 200 boxes
Packaging:
6 x Custard Foil 78mm
1 x Printed Product Label
1 x Thermal Label
Make:
18 kg Caramelised Onions w Thyme
200 each Mini Savoury Shortcrust Tart Case
200 each Savoury Custard
6 x Mini Onion & Waterlip Tartlet (Single)
Finish:
6 x Bake tartlets
1 x Box & freeze (6 per box)"""),
    ("savoury", 310, D(11, 3), "account for possible wastage", D(10, 26), D(11, 2),
     """FRZN Ham Hock, Cheddar & Mustard Tartlets — 340 boxes
Packaging:
6 x Custard Foil 78mm
1 x Printed Product Label
1 x Thermal Label
Make:
6 x Ham Hock, Mustard & Parsley Filling
340 each Mini Savoury Shortcrust Tart Case
340 each Savoury Custard
6 x Mini Ham Hock, Cheddar & English Mustard tartlet (Single)
Finish:
6 x Bake tartlets
1 x Box & freeze (6 per box)"""),
]


def _at(d, tz):
    return timezone.make_aware(datetime.combine(d, time(17)), tz) if d else None


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
    with transaction.atomic():
        lists = {k: List.objects.create(board=b, title=t, colour=c, position=(i + 1) * 1000)
                 for i, (k, t, c) in enumerate(LISTS)}
        labels = {k: Label.objects.create(board=b, name=nm, colour=c) for k, (nm, c) in LABELS.items()}
        n = 0
        for i, (lab, forecast, launch, why, start, end, text) in enumerate(PRODUCTS):
            planned = products.parse(text)["planned"]
            var = int(planned) - forecast
            card = products.create(lists["prep"], text, owner, position=(i + 1) * 1000, extra_fields={
                "Forecast": (CustomField.NUMBER, False, forecast),
                "Variance": (CustomField.TEXT, False, f"{var:+,} ({var / forecast * 100:+.1f}%)" if var else "—"),
                "Launch": (CustomField.DATE, True, launch.isoformat()),
            })
            card.start_date, card.due_at, card.reminder_minutes = start, _at(end, tz), -1
            card.description = (
                f"**Demand** — forecast v2 (post-reviewed): {forecast:,} · planned {int(planned):,} · "
                + (f"variance {var:+,} ({var / forecast * 100:+.1f}%) — {why}" if var else "matches forecast")
                + f"\n**Launch** {launch.strftime('%-d %b %Y')}" + (" — already on sale" if launch <= today else "")
                + "\n\nRecord what's done on each line (the box next to it). Change **Planned** and every "
                  "“per product” line follows. Dates are a first draft.")
            card.save()
            card.labels.set([labels[lab]])
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
