import datetime as dt
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.db.models import Sum
from django.shortcuts import render

from catalog.models import Customer
from sales import services as sales
from sales.models import CustomerRequest
from stock import pricing, services as stock
from stock.models import Batch, Shipment, ShipmentLine

from . import analytics as an
from . import excel


def _money_tip(d, cell):
    return f"<b>{an.month_label(d)}</b><br>{_fmt(cell.amount)} BYN · {cell.qty} шт"


def _fmt(value):
    return f"{value:,.0f}".replace(",", " ")


@login_required
def dashboard(request):
    today = dt.date.today()
    this_month = an.month_start(today)
    prev_month = an.add_months(this_month, -1)
    costs = pricing.can_see_costs(request.user)

    balances = stock.batch_balances()
    on_hand = sum(q for q in balances.values() if q > 0)
    positions = len({p for (p, _), q in balances.items() if q > 0})
    transit = stock.in_transit()
    value = pricing.stock_value(balances)[1] if costs else None

    # отгрузки за 12 месяцев — для графика и сравнения месяцев
    first = an.add_months(this_month, -11)
    months, _, totals, _ = an.monthly_shipments(first, today)
    by_month = dict(zip(months, totals))
    cur, prev = by_month.get(this_month, an.Cell()), by_month.get(prev_month, an.Cell())
    delta = ((cur.amount - prev.amount) / prev.amount * 100).quantize(Decimal("1")) if prev.amount else None
    chart = an.bar_chart([(m, by_month[m].amount, _money_tip(m, by_month[m])) for m in months])

    profit_month = None
    if costs:
        lines = pricing.with_costs(ShipmentLine.objects.filter(shipment__date__gte=this_month))
        profit_month = pricing.totals(lines)

    year_start = today.replace(month=1, day=1)
    year = (ShipmentLine.objects.filter(shipment__date__gte=year_start).values("shipment__customer__name")
            .annotate(qty=Sum("qty")).order_by("-qty"))
    year_amounts = {}
    for line in ShipmentLine.objects.filter(shipment__date__gte=year_start, price_byn__isnull=False).select_related("shipment__customer"):
        year_amounts[line.shipment.customer.name] = year_amounts.get(line.shipment.customer.name, 0) + line.amount
    top = [{"name": r["shipment__customer__name"], "qty": r["qty"], "amount": year_amounts.get(r["shipment__customer__name"], 0)} for r in year]
    top_max = max((t["amount"] for t in top), default=0) or 1
    for t in top:
        t["percent"] = int(t["amount"] * 100 / top_max)

    open_requests = list(CustomerRequest.objects.filter(status=CustomerRequest.Status.OPEN).select_related("customer").order_by("date")[:50])
    short_total = 0
    for r in open_requests:
        r.summary = sales.request_summary(sales.request_rows(r))
        short_total += r.summary["short"]

    return render(request, "reports/dashboard.html", {
        "today": today, "on_hand": on_hand, "positions": positions, "transit_qty": sum(transit.values()),
        "transit_batches": Batch.objects.filter(status=Batch.Status.IN_TRANSIT).annotate(qty=Sum("lines__qty_expected")).order_by("-created_at")[:5],
        "stock_value": value, "cur": cur, "prev": prev, "delta": delta, "month_label": an.month_label(this_month),
        "chart": chart, "chart_rows": [(an.month_label(m), by_month[m]) for m in months],
        "profit_month": profit_month, "top": top, "year": today.year,
        "open_requests": open_requests[:6], "open_count": len(open_requests), "short_total": short_total,
        "low": an.low_stock(today)[:6],
        "recent": Shipment.objects.select_related("customer").annotate(qty=Sum("lines__qty")).order_by("-date", "-created_at")[:6],
    })


@login_required
def index(request):
    return render(request, "reports/index.html")


def _period(params, months_back=11):
    today = dt.date.today()
    def parse(name, default):
        try:
            return dt.date.fromisoformat(params.get(name, ""))
        except ValueError:
            return default
    return parse("date_from", an.add_months(an.month_start(today), -months_back)), parse("date_to", today)


@login_required
def shipments_monthly(request):
    date_from, date_to = _period(request.GET)
    customer = request.GET.get("customer", "")
    by = "product" if request.GET.get("by") == "product" else "customer"
    measure = "qty" if request.GET.get("measure") == "qty" else "amount"
    months, rows, totals, grand = an.monthly_shipments(date_from, date_to, int(customer) if customer.isdigit() else None, by)
    if excel.wants_xlsx(request):
        import openpyxl

        wb = openpyxl.Workbook()
        caption = f"Отгрузки по месяцам {date_from:%d.%m.%Y} — {date_to:%d.%m.%Y}"
        first = "Изделие" if by == "product" else "Заказчик"
        heads = [first] + [an.month_label(m) for m in months] + ["Итого"]
        for title, attr in (("Сумма без НДС, BYN", "amount"), ("Количество, шт", "qty")):
            data = [[r.label] + [getattr(c, attr) for c in r.cell_list(months)] + [getattr(r.total, attr)] for r in rows]
            data.append(["Итого"] + [getattr(c, attr) for c in totals] + [getattr(grand, attr)])
            excel.add_sheet(wb, title.split(",")[0], heads, data, caption=f"{caption} · {title}", total_row=True)
        return excel.response(wb, f"Отгрузки по месяцам {date_from:%Y-%m}—{date_to:%Y-%m}.xlsx")
    chart = an.bar_chart([(m, getattr(t, measure), _money_tip(m, t)) for m, t in zip(months, totals)],
                         width=1100, height=300, unit="BYN" if measure == "amount" else "шт")
    return render(request, "reports/shipments_monthly.html", {
        "date_from": date_from, "date_to": date_to, "customer": customer, "customers": Customer.objects.all(),
        "by": by, "measure": measure, "months": [(m, an.month_label(m, short=True)) for m in months],
        "rows": [(r, r.cell_list(months)) for r in rows], "totals": totals, "grand": grand, "chart": chart,
    })


@login_required
def low_stock(request):
    rows = an.low_stock()
    if excel.wants_xlsx(request):
        data = [[r["product"].code, r["product"].yapib, str(r["product"].customer or ""), r["on_hand"], r["in_transit"], r["reserved"],
                 r["available"], r["avg"], r["months"], r["min_stock"], r["suggested"]] for r in rows]
        return excel.table("Заканчивается", ["Изделие", "ЯПИБ", "Заказчик", "На складе", "В пути", "Резерв", "Доступно",
                                             "Отгрузки в месяц", "Хватит, мес", "Минимум", "Заказать"],
                           data, f"Заканчивается {dt.date.today():%Y-%m-%d}.xlsx",
                           caption=f"Заканчивается товар на {dt.date.today():%d.%m.%Y}")
    return render(request, "reports/low_stock.html", {"rows": rows, "cover": an.COVER_MONTHS, "target": an.TARGET_MONTHS, "window": an.WINDOW_MONTHS})


@login_required
def help_page(request):
    return render(request, "reports/help.html")
