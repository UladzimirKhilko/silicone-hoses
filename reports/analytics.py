"""Расчёты для сводки и отчётов (этап 6): отгрузки по месяцам, «Заканчивается», график."""

import datetime as dt
import math
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.db.models import Sum

from catalog.models import Product
from sales import services as sales
from stock import services as stock
from stock.models import ShipmentLine

MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

# «Заканчивается»: сколько месяцев продаж должен покрывать доступный товар. Поставки раз в квартал
# плюс ~2 месяца в пути → меньше 4 месяцев — пора заказывать; заказываем на 6 месяцев вперёд.
COVER_MONTHS = 4
TARGET_MONTHS = 6
WINDOW_MONTHS = 6  # средние отгрузки — за последние полгода


def month_start(d):
    return d.replace(day=1)


def add_months(d, n):
    m = d.year * 12 + d.month - 1 + n
    return dt.date(m // 12, m % 12 + 1, 1)


def month_range(first, last):
    out, d = [], month_start(first)
    while d <= last:
        out.append(d)
        d = add_months(d, 1)
    return out


def month_label(d, short=False):
    return f"{MONTHS_SHORT[d.month - 1]} {d:%y}" if short else f"{MONTHS[d.month - 1]} {d.year}"


# --- Отгрузки по месяцам × заказчики ---

@dataclass
class Cell:
    qty: int = 0
    amount: Decimal = Decimal(0)

    def add(self, qty, amount):
        self.qty += qty
        self.amount += amount or 0


@dataclass
class PivotRow:
    label: str
    key: object
    cells: dict = field(default_factory=lambda: defaultdict(Cell))
    total: Cell = field(default_factory=Cell)

    def cell_list(self, months):
        return [self.cells.get(m, Cell()) for m in months]


def monthly_shipments(date_from, date_to, customer_id=None, by="customer"):
    """Сводная таблица: строки — заказчики (или изделия), столбцы — месяцы; шт и сумма без НДС."""
    lines = ShipmentLine.objects.filter(shipment__date__range=(date_from, date_to)).select_related("shipment__customer", "product")
    if customer_id:
        lines = lines.filter(shipment__customer_id=customer_id)
    months = month_range(date_from, date_to)
    rows, totals = {}, defaultdict(Cell)
    grand = Cell()
    for line in lines:
        if by == "product":
            key, label = line.product_id, line.product.code
        else:
            key, label = line.shipment.customer_id, line.shipment.customer.name
        row = rows.get(key) or rows.setdefault(key, PivotRow(label, key))
        m = month_start(line.shipment.date)
        row.cells[m].add(line.qty, line.amount)
        row.total.add(line.qty, line.amount)
        totals[m].add(line.qty, line.amount)
        grand.add(line.qty, line.amount)
    ordered = sorted(rows.values(), key=lambda r: (-r.total.amount, -r.total.qty, r.label))
    return months, ordered, [totals.get(m, Cell()) for m in months], grand


# --- Заканчивается ---

def avg_monthly_shipments(product_ids=None, today=None, window=WINDOW_MONTHS):
    today = today or dt.date.today()
    since = add_months(month_start(today), -window)
    qs = ShipmentLine.objects.filter(shipment__date__gte=since, shipment__date__lt=month_start(today))
    if product_ids is not None:
        qs = qs.filter(product_id__in=product_ids)
    return {r["product_id"]: Decimal(r["q"]) / window for r in qs.values("product_id").annotate(q=Sum("qty"))}


def low_stock(today=None, cover=COVER_MONTHS, target=TARGET_MONTHS):
    """Изделия, которые пора заказывать. Доступно = на складе + в пути − резерв под заявки."""
    from sales.china import carton_sizes

    avg = avg_monthly_shipments(today=today)
    products = list(Product.objects.filter(pk__in=set(avg) | set(Product.objects.filter(min_stock__isnull=False).values_list("pk", flat=True)))
                    .select_related("customer"))
    ids = [p.pk for p in products]
    have, transit, reserved = stock.on_hand(ids), stock.in_transit(ids), sales.reserved(ids)
    cartons = carton_sizes(ids)
    rows = []
    for p in products:
        a = avg.get(p.pk, Decimal(0))
        available = have.get(p.pk, 0) + transit.get(p.pk, 0) - reserved.get(p.pk, 0)
        months = (Decimal(available) / a).quantize(Decimal("0.1")) if a else None
        below_min = p.min_stock is not None and available < p.min_stock
        if not (below_min or (months is not None and months < cover)):
            continue
        want = max(p.min_stock or 0, math.ceil(a * target))
        need = max(want - available, 0)
        per = cartons.get(p.pk)
        rows.append({
            "product": p, "on_hand": have.get(p.pk, 0), "in_transit": transit.get(p.pk, 0), "reserved": reserved.get(p.pk, 0),
            "available": available, "avg": a.quantize(Decimal("0.1")), "months": months, "min_stock": p.min_stock,
            "suggested": (math.ceil(need / per) * per if per else need), "carton": per,
            "level": "bad" if available <= 0 or (months is not None and months < 1) else "warn",
            "percent": max(0, min(100, int((months or 0) * 100 / cover))) if months is not None else (0 if below_min else 100),
        })
    rows.sort(key=lambda r: (r["months"] if r["months"] is not None else Decimal(-1), r["product"].code))
    return rows


# --- График: столбцы по месяцам (один ряд) ---

def _nice_step(maximum, ticks=4):
    if maximum <= 0:
        return 1
    raw = maximum / ticks
    power = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * power:
            return m * power
    return 10 * power


def compact(value):
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млн"
    if abs(value) >= 1000:
        return f"{value / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " тыс"
    return f"{value:.0f}"


def bar_chart(points, width=720, height=240, unit="BYN"):
    """points: [(дата месяца, значение, подсказка)] → геометрия SVG для шаблона reports/_bars.html.

    По правилам визуализации: столбец ≤ 24px, скругление 4px сверху, тонкая сетка,
    подписи только у последнего и максимального столбца, подсказка при наведении.
    """
    left, right, top, bottom = 52, 8, 18, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [float(v) for _, v, _ in points] or [0]
    step = _nice_step(max(values))
    top_value = step * max(1, math.ceil(max(values) / step))
    n = max(len(points), 1)
    band = plot_w / n
    bar_w = min(24, band * 0.6)
    best = values.index(max(values)) if any(values) else None
    bars = []
    for i, (month, value, tip) in enumerate(points):
        v = float(value)
        h = 0 if not top_value else plot_h * v / top_value
        x = left + band * i + (band - bar_w) / 2
        y = top + plot_h - h
        r = min(4, h / 2, bar_w / 2)
        path = (f"M{x:.1f},{top + plot_h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
                f"H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{top + plot_h:.1f} Z") if h > 0 else ""
        bars.append({
            "path": path, "x": x, "cx": x + bar_w / 2, "y": y, "label": MONTHS_SHORT[month.month - 1],
            "year": month.year if month.month == 1 or i == 0 else None,
            "hit_x": left + band * i, "hit_w": band, "tip": tip,
            "show_value": v > 0 and (i == len(points) - 1 or i == best), "value": compact(v),
        })
    grid = [{"y": top + plot_h - plot_h * k * step / top_value, "label": compact(k * step)} for k in range(int(top_value / step) + 1)]
    return {"width": width, "height": height, "bars": bars, "grid": grid, "left": left, "right": width - right,
            "base": top + plot_h, "plot_top": top, "plot_h": plot_h, "label_y": height - 8, "unit": unit}


# --- Заявки по месяцам (в штуках и деньгах) ---

@dataclass
class RequestTotals:
    count: int = 0
    qty: int = 0
    amount: Decimal = Decimal(0)
    shipped_qty: int = 0
    shipped_amount: Decimal = Decimal(0)
    left_amount: Decimal = Decimal(0)
    short_amount: Decimal = Decimal(0)
    no_price: int = 0

    def add(self, s):
        self.count += 1
        self.qty += s["qty"]
        self.amount += s["amount"]
        self.shipped_qty += s["shipped"]
        self.shipped_amount += s["shipped_amount"]
        self.left_amount += s["left_amount"]
        self.short_amount += s["short_amount"]
        self.no_price += s["no_price"]

    @property
    def percent(self):
        return round(self.shipped_amount * 100 / self.amount) if self.amount else (round(self.shipped_qty * 100 / self.qty) if self.qty else 0)


def monthly_requests(date_from, date_to, customer_id=None):
    """По месяцам даты заявки: сколько заказали (шт, BYN), сколько отгрузили по заявкам, сколько осталось / не хватает.
    Отменённые заявки не считаются. Возвращает [(месяц, итог, {заказчик: итог})], общий итог."""
    from sales.models import CustomerRequest

    reqs = (CustomerRequest.objects.filter(date__range=(date_from, date_to)).exclude(status=CustomerRequest.Status.CANCELLED)
            .select_related("customer").order_by("date"))
    if customer_id:
        reqs = reqs.filter(customer_id=customer_id)
    months = {m: (RequestTotals(), defaultdict(RequestTotals)) for m in month_range(date_from, date_to)}
    grand = RequestTotals()
    for req in reqs:
        s = sales.request_summary(sales.request_rows(req))
        total, by_customer = months[month_start(req.date)]
        total.add(s)
        by_customer[req.customer.name].add(s)
        grand.add(s)
    return [(m, t, dict(sorted(c.items()))) for m, (t, c) in months.items()], grand
