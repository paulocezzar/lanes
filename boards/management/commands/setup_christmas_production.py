"""One-off: fill the 'Christmas Production' board from the StockUp Christmas 2026 plan
(locked 16 Aug: 11 products, 26 components, 3,458 kg).

Lists follow the order the work has to happen; labels are the product families.
Quantities, codes and StockUp warnings come from the plan; the dates are a first draft
for the Gantt (StockUp has most windows as 'unknown') and are meant to be dragged.
Runs only when LANES_XMAS_BOARD holds the board id; skips if the board already has cards.
"""
import os
from datetime import date, datetime, time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from boards.models import Board, CardFieldValue, Card, Checklist, ChecklistItem, CustomField, Dependency, Label, List

D = lambda m, d: date(2026, m, d)  # noqa: E731

LISTS = [
    ("packaging", "Packaging to order", "blue"),
    ("components", "Components to make", "orange"),
    ("products", "Bake, pack & freeze", "green"),
    ("done", "Done", ""),
]

LABELS = {
    "cake": ("Christmas cake", "green"),
    "mince": ("Mince pies", "orange"),
    "yule": ("Yule log", "purple"),
    "tarts": ("Sweet tarts", "pink"),
    "savoury": ("Savoury tartlets", "sky"),
    "stollen": ("Stollen", "yellow"),
    "puff": ("Rough puff", "grey"),
    "pack": ("Packaging", "blue"),
    "attention": ("Needs attention", "red"),
}

# key, list, title, labels, qty, unit, code, start, due, waits-for, note, extra
CARDS = [
    # ---------- packaging (shared orders counted once) ----------
    ("p_label", "packaging", "Printed Product Label", ["pack", "attention"], 3460, "labels", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared ×6 — one order covers all.",
     {"split": ["AF Christmas Cake — 430", "Crumble Topped Mince Pies — 2,000", "FRZN Goat's Cheese tartlets — 200",
                "FRZN Ham Hock tartlets — 340", "Miniature Iced Christmas Cake — 280", "Stollen Wreath (Tinned) — 210"]}),
    ("p_label_cran", "packaging", "Printed Product Label (NPD-P2) — Cranberry & Orange", ["pack", "mince", "attention"], None, "labels", "NPD-P2",
     D(10, 26), D(10, 27), [], "No quantity or usage basis captured in StockUp — needed by 6 Oct. Artwork is handled outside production.", {}),
    ("p_thermal", "packaging", "Thermal Label", ["pack", "attention"], 3350, "labels", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared ×5 — one order covers all.",
     {"split": ["Crumble Topped Mince Pies — 2,000", "FRZN Goat's Cheese tartlets — 200", "FRZN Ham Hock tartlets — 340",
                "Stollen Wreath (Tinned) — 210", "TN Stollen 600g — 600"]}),
    ("p_thermal_cran", "packaging", "Thermal Label (NPD-P32) — Cranberry & Orange", ["pack", "mince", "attention"], None, "labels", "NPD-P32",
     D(10, 26), D(10, 27), [], "No quantity or usage basis captured in StockUp — needed by 6 Oct. Artwork is handled outside production.", {}),
    ("p_box", "packaging", "0427 die-cut self-locking box c/w 6 cell", ["pack", "mince"], 2000, "boxes", "NPD-P91",
     D(10, 26), D(10, 27), [], "Ordered for Crumble Topped Mince Pies (2,000). Cranberry & Orange quantity not captured yet.", {}),
    ("p_grease", "packaging", "Greaseproof paper 230×450 40gsm", ["pack", "mince", "attention"], 2000, "sheets", "NPD-P94",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. 2,000 for Crumble Topped; Cranberry & Orange quantity not captured.", {}),
    ("p_board", "packaging", "5mm Black Round Matt Masonite Board (8\")", ["pack"], 910, "boards", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared ×3 — one order covers all.",
     {"split": ["AF Christmas Cake — 430", "Black Forest Yule Log — 270", "Stollen Wreath (Tinned) — 210"]}),
    ("p_band", "packaging", "Belly Band", ["pack"], 1030, "bands", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared with TN Stollen — one order covers both.",
     {"split": ["AF Christmas Cake — 430", "TN Stollen 600g — 600"]}),
    ("p_tin", "packaging", "Cake/Biscuit Tin", ["pack"], 640, "tins", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared with Stollen Wreath — one order covers both.",
     {"split": ["AF Christmas Cake — 430", "Stollen Wreath (Tinned) — 210"]}),
    ("p_film", "packaging", "700/350mm 20mic centre fold film", ["pack"], 1090, "", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared ×3 — one order covers all.",
     {"split": ["Miniature Iced Christmas Cake — 280", "Stollen Wreath (Tinned) — 210", "TN Stollen 600g — 600"]}),
    ("p_pouch", "packaging", "Vacuum Pouch 400×500mm", ["pack"], 12270, "pouches", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared with Black Forest Yule Log — one order covers both.",
     {"split": ["Crumble Topped Mince Pies — 12,000", "Black Forest Yule Log — 270"]}),
    ("p_foil", "packaging", "Custard Foil 78mm", ["pack"], 6840, "foils", "",
     D(10, 26), D(10, 27), [], "No lead time in StockUp. Shared ×3 — one order covers all.",
     {"split": ["Cranberry & Orange Mince Pie — 3,600", "FRZN Goat's Cheese tartlets — 1,200", "FRZN Ham Hock tartlets — 2,040"]}),
    ("p_basket", "packaging", "Panibois Duc baking basket (175×110)", ["pack", "stollen"], 600, "baskets", "NPD-P15",
     D(10, 26), D(10, 27), [], "No lead time in StockUp.", {}),
    ("ms_packaging", "packaging", "All packaging delivered", [], None, "", "",
     None, D(10, 30), ["p_label", "p_label_cran", "p_thermal", "p_thermal_cran", "p_box", "p_grease", "p_board", "p_band",
                       "p_tin", "p_film", "p_pouch", "p_foil", "p_basket"],
     "Yule Log and the savoury tartlets launch on 3 Nov — their boards, pouches, foil and labels must be in before then.",
     {"milestone": True}),

    # ---------- components ----------
    ("c_mincemeat", "components", "Mincemeat (Mince Pie)", ["mince", "attention"], 600.168, "kg", "NPD-R412",
     D(10, 26), D(10, 30), [], "Short — window passed (was 24 May – 6 Oct).", {}),
    ("c_crumble", "components", "Crumble Topping", ["mince", "attention"], 180.05, "kg", "NPD-R567",
     D(10, 26), D(10, 27), [], "Short — window passed (was 7 Aug – 6 Oct).", {}),
    ("c_praline", "components", "Hazelnut Praline", ["mince", "tarts", "attention"], 34.007, "kg", "",
     D(10, 26), D(10, 26), [], "Past last safe start (window 23 Jul – 6 Oct). 24.007 kg Crumble Topped + 10 kg Dark Choc tarts.", {}),
    ("c_cran_mincemeat", "components", "Cranberry and Orange Mincemeat", ["mince", "attention"], 180.011, "kg", "NPD-R2840",
     D(10, 28), D(10, 29), [], "No window set in StockUp.", {}),
    ("c_cases", "components", "Mince Pie Case", ["mince", "attention"], 15600, "each", "",
     D(10, 26), D(11, 6), [], "Short — window passed (scheduled 6 Oct). 12,000 Crumble Topped + 3,600 Cranberry & Orange.", {}),
    ("c_sweet_case", "components", "Sweet Pastry Tart Case", ["tarts", "attention"], 65, "kg", "NPD-R174",
     D(10, 26), D(10, 27), [], "No window set in StockUp.", {}),
    ("c_choc_icing", "components", "Chocolate Icing", ["tarts", "attention"], 85, "kg", "NPD-R1441",
     D(10, 28), D(10, 28), [], "No window set in StockUp.", {}),
    ("c_caramel", "components", "Salt Caramel", ["tarts", "attention"], 35, "kg", "NPD-R2544",
     D(10, 28), D(10, 28), [], "No window set in StockUp.", {}),
    ("c_mini_cake", "components", "Mini Xmas Cake 2026 (Single)", ["cake", "attention"], 280, "each", "NPD-R2810",
     D(10, 26), D(10, 27), [], "No window set in StockUp. Batter and fruit soak already made.", {}),
    ("c_onions", "components", "Caramelised Onions w Thyme", ["savoury", "attention"], 18, "kg", "NPD-R2174",
     D(10, 26), D(10, 26), [], "No window set in StockUp.", {}),
    ("c_savoury_case", "components", "Mini Savoury Shortcrust Tart Case", ["savoury", "attention"], 540, "each", "",
     D(10, 26), D(10, 27), [], "No window set in StockUp. 200 Goat's Cheese + 340 Ham Hock.", {}),
    ("c_custard", "components", "Savoury Custard", ["savoury", "attention"], 540, "each", "",
     D(10, 28), D(10, 28), [], "No window set in StockUp. 200 Goat's Cheese + 340 Ham Hock.", {}),
    ("c_hamhock", "components", "Ham Hock, Mustard & Parsley Filling", ["savoury", "attention"], 2040, "each", "NPD-R2360",
     D(10, 28), D(10, 28), [], "No window set in StockUp.", {}),
    ("c_fruit_soak", "components", "Fruit Soak (Stollen)", ["stollen", "attention"], 212.413, "kg", "",
     D(10, 26), D(10, 27), [], "No window set in StockUp. 72.531 kg Wreath + 139.882 kg TN Stollen.", {}),
    ("c_poolish", "components", "Stollen Poolish", ["stollen", "attention"], 79.397, "kg", "",
     D(10, 28), D(10, 28), [], "No window set in StockUp. 27.111 kg Wreath + 52.286 kg TN Stollen.", {}),
    ("c_stollen_dough", "components", "Stollen Dough", ["stollen", "attention"], 455.554, "kg", "",
     D(10, 29), D(10, 30), ["c_fruit_soak", "c_poolish"], "No window set in StockUp. 155.554 kg Wreath + 300 kg TN Stollen.", {}),
    ("c_glaze", "components", "Stollen Butter Glaze", ["stollen", "attention"], 31.35, "kg", "",
     D(10, 30), D(10, 30), [], "No window set in StockUp. 7.35 kg Wreath + 24 kg TN Stollen.", {}),
    ("c_puff", "components", "New Rough Puff Pastry Dough 2025", ["puff", "attention"], 450, "kg", "NPD-R1868",
     D(10, 26), D(10, 28), [], "No window set in StockUp.", {}),
    ("c_yule_batter", "components", "Yule Log Batter", ["yule", "attention"], 92.571, "kg", "NPD-R2836",
     D(10, 27), D(10, 27), [], "No window set in StockUp.", {}),
    ("c_yule_sheet", "components", "Yule Log Baked Sheet", ["yule", "attention"], 81, "kg", "NPD-R2839",
     D(10, 28), D(10, 28), ["c_yule_batter"], "No window set in StockUp.", {}),
    ("c_buttercream", "components", "Chocolate Buttercream", ["yule", "attention"], 148.5, "kg", "NPD-R2837",
     D(10, 29), D(10, 29), [], "No window set in StockUp.", {}),

    # ---------- finished products ----------
    ("f_crumble", "products", "Crumble Topped Mince Pies Retail (Pack/6)", ["mince"], 2000, "packs", "",
     D(11, 2), D(12, 4), ["c_mincemeat", "c_crumble", "c_praline"], "2,000 planned · 4 to make · 1 to buy.", {}),
    ("f_cran", "products", "Cranberry and Orange Mince Pie (Pack/6)", ["mince"], 600, "packs", "",
     D(11, 2), D(11, 13), ["c_cran_mincemeat"], "600 planned · 1 to make.", {}),
    ("f_goat", "products", "FRZN Goat's Cheese, Caramelised Onion & Thyme Tartlets (Box of 6)", ["savoury"], 200, "boxes", "",
     D(10, 29), D(10, 30), ["c_onions", "c_savoury_case", "c_custard"], "200 boxes = 1,200 Mini Onion & Waterlip Tartlets (NPD-R2263).", {}),
    ("f_ham", "products", "FRZN Ham Hock, Cheddar & Mustard Tartlets (Box of 6)", ["savoury"], 340, "boxes", "",
     D(10, 29), D(11, 2), ["c_hamhock", "c_savoury_case", "c_custard"], "340 boxes = 2,040 Mini Ham Hock, Cheddar & English Mustard tartlets (NPD-R2264).", {}),
    ("f_af_cake", "products", "AF Christmas Cake — decorate & pack", ["cake"], 430, "cakes", "",
     D(11, 2), D(11, 13), [], "430 planned. Baked already (374,640 g made) — needs board, tin, belly band and labels.", {}),
    ("f_mini_cake", "products", "Miniature Iced Christmas Cake 150g (Pack/1)", ["cake"], 280, "cakes", "",
     D(11, 2), D(11, 6), ["c_mini_cake"], "280 planned · 2 to make.", {}),
    ("f_wreath", "products", "Stollen Wreath (Tinned)", ["stollen"], 210, "wreaths", "",
     D(11, 2), D(11, 3), ["c_stollen_dough", "c_glaze"], "210 planned.", {}),
    ("f_tn_stollen", "products", "TN Stollen 600g", ["stollen"], 600, "loaves", "",
     D(11, 2), D(11, 6), ["c_stollen_dough", "c_glaze"], "600 planned.", {}),
    ("f_puff", "products", "Rough Puff 1.5kg", ["puff"], 300, "blocks", "",
     D(10, 29), D(10, 29), ["c_puff"], "300 planned.", {}),
    ("f_yule", "products", "Black Forest Yule Log (1000g)", ["yule"], 270, "logs", "",
     D(10, 30), D(11, 2), ["c_yule_sheet", "c_buttercream"], "270 planned.", {}),
    ("f_tarts", "products", "Dark Chocolate and Salted Caramel Tarts (Pack/2)", ["tarts"], 250, "packs", "",
     D(10, 29), D(10, 30), ["c_sweet_case", "c_choc_icing", "c_caramel", "c_praline"], "250 planned · 1 to make.", {}),
    ("ms_done", "products", "Christmas production complete", [], None, "", "",
     None, D(12, 7), ["f_crumble", "f_cran", "f_goat", "f_ham", "f_af_cake", "f_mini_cake", "f_wreath", "f_tn_stollen",
                       "f_puff", "f_yule", "f_tarts"], "", {"milestone": True}),

    # ---------- already made ----------
    ("d_batter", "done", "Christmas Cake Batter revised (2026)", ["cake"], 426.2, "kg", "",
     None, None, [], "493,000 g made. 387 kg AF Christmas Cake + 39.2 kg Miniature Iced.", {"complete": True}),
    ("d_soak", "done", "Fruit Soak revised (Christmas Cake) 2026", ["cake"], 239.44, "kg", "",
     None, None, [], "267,500 g made. 217.417 kg AF Christmas Cake + 22.023 kg Miniature Iced.", {"complete": True}),
    ("d_af_baked", "done", "AF Christmas Cake (Baked)", ["cake"], 430, "each", "NPD-R2822",
     None, None, [], "374,640 g made.", {"complete": True}),
]


# Demand tab (forecast v2 / post-reviewed, arrived 18 Aug 2026): forecast, planned, launch, reason
DEMAND = {
    "f_af_cake": (407, 430, D(10, 6), "buffer"),
    "f_yule": (256, 270, D(11, 3), "buffer"),
    "f_cran": (477, 600, D(10, 6), "buffer"),
    "f_crumble": (1504, 2000, D(10, 6), "account for wholesale demands and internal orders"),
    "f_tarts": (208, 250, D(10, 6), "account for wholesale and internal orders"),
    "f_goat": (184, 200, D(11, 3), "account for possible wastage"),
    "f_ham": (310, 340, D(11, 3), "account for possible wastage"),
    "f_mini_cake": (266, 280, D(10, 6), "account for possible wastage"),
    "f_wreath": (190, 210, D(10, 6), "account for possible wastage"),
    "f_tn_stollen": (510, 600, D(10, 6), "account for possible increase in orders"),
    "f_puff": (300, 300, date(2026, 8, 24), ""),
}


def _demand_text(key, today):
    fc, plan, launch, why = DEMAND[key]
    var = plan - fc
    pct = f" ({var / fc * 100:+.1f}%)" if var else ""
    lines = [f"**Demand** — forecast v2 (post-reviewed): {fc:,} · planned {plan:,} · "
             + (f"variance {var:+,}{pct} — {why}" if var else "matches forecast"),
             f"**Launch** {launch.strftime('%-d %b %Y')}" + (" — already on sale" if launch <= today else "")]
    return "\n".join(lines)


def _num(q):
    if q is None:
        return None
    return int(q) if float(q).is_integer() else round(float(q), 3)


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--board", default=os.environ.get("LANES_XMAS_BOARD", ""))

    def handle(self, *args, board="", **opts):
        if not str(board).strip():
            return
        b = Board.objects.filter(pk=int(board)).first()
        if not b:
            self.stdout.write("setup_christmas_production: board not found — skipping.")
            return
        if Card.objects.filter(list__board=b).exists():
            self.stdout.write("setup_christmas_production: board already has cards — skipping.")
            return
        n = build(b)
        self.stdout.write(f"setup_christmas_production: {n} cards on board {b.pk}.")


def wipe(b):
    """Remove everything this command created (lists, cards, labels, fields)."""
    with transaction.atomic():
        Card.objects.filter(list__board=b).delete()
        b.lists.all().delete()
        b.labels.all().delete()
        b.custom_fields.all().delete()


def build(b):
    if True:
        m = b.memberships.filter(role="admin").select_related("user").first()
        owner = m.user if m else get_user_model().objects.filter(is_superuser=True).first()
        tz = timezone.get_current_timezone()
        with transaction.atomic():
            b.lists.all().delete()          # the empty To do / Doing / Done
            lists = {k: List.objects.create(board=b, title=t, colour=c, position=(i + 1) * 1000)
                     for i, (k, t, c) in enumerate(LISTS)}
            labels = {k: Label.objects.create(board=b, name=n, colour=c) for k, (n, c) in LABELS.items()}
            top = (CustomField.objects.filter(board=b).order_by("-position").first() or CustomField(position=0)).position
            fq = CustomField.objects.create(board=b, name="Quantity", kind="number", show_on_card=True, position=top + 1)
            fu = CustomField.objects.create(board=b, name="Unit", kind="text", show_on_card=True, position=top + 2)
            fc = CustomField.objects.create(board=b, name="StockUp code", kind="text", show_on_card=False, position=top + 3)
            ff = CustomField.objects.create(board=b, name="Forecast", kind="number", show_on_card=False, position=top + 4)
            fv = CustomField.objects.create(board=b, name="Variance", kind="text", show_on_card=False, position=top + 5)
            fl = CustomField.objects.create(board=b, name="Launch", kind="date", show_on_card=True, position=top + 6)
            today = timezone.localdate()
            cards, pos = {}, {}
            for key, lk, title, labs, qty, unit, code, start, due, _deps, note, extra in CARDS:
                pos[lk] = pos.get(lk, 0) + 1000
                ms = extra.get("milestone", False)
                c = Card.objects.create(
                    list=lists[lk], title=title, position=pos[lk], created_by=owner,
                    description=(_demand_text(key, today) + "\n\n" if key in DEMAND else "") + note + ("\n\nDates are a first draft — drag the bar on the Gantt to the real window."
                                        if (start or due) and not ms else ""),
                    start_date=None if ms else start,
                    due_at=timezone.make_aware(datetime.combine(due, time(17)), tz) if due else None,
                    is_milestone=ms, is_complete=extra.get("complete", False), reminder_minutes=-1)
                c.labels.set([labels[x] for x in labs])
                if qty is not None:
                    CardFieldValue.objects.create(card=c, field=fq, value=_num(qty))
                if unit and qty is not None:
                    CardFieldValue.objects.create(card=c, field=fu, value=unit)
                if code:
                    CardFieldValue.objects.create(card=c, field=fc, value=code)
                if key in DEMAND:
                    fcst, plan, launch, why = DEMAND[key]
                    var = plan - fcst
                    CardFieldValue.objects.create(card=c, field=ff, value=fcst)
                    CardFieldValue.objects.create(card=c, field=fv, value=f"{var:+,} ({var / fcst * 100:+.1f}%)" if var else "—")
                    CardFieldValue.objects.create(card=c, field=fl, value=launch.isoformat())
                if extra.get("split"):
                    cl = Checklist.objects.create(card=c, title="Covers")
                    for i, line in enumerate(extra["split"]):
                        ChecklistItem.objects.create(checklist=cl, text=line, position=(i + 1) * 1000)
                cards[key] = c
            for key, *_rest in CARDS:
                deps = _rest[8]
                for d in deps:
                    Dependency.objects.create(card=cards[key], depends_on=cards[d])
            b.is_project = True
            b.project_owner = owner
            b.rag = "auto"
            b.status_note = ("From the StockUp Christmas 2026 plan (locked 16 Aug, forecast v2): 11 products, 26 components, "
                             "3,458 kg. Selling 24 Aug – 2 Jan. Dates are a first draft.")
            b.status_updated_at = timezone.now()
            b.save()
        return len(cards)
