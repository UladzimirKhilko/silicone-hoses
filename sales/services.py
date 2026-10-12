"""Спецификации (цена и лимит в отгрузках), заявки (выполнение, резерв, нехватка)."""

import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db.models import Q, Sum

from catalog.models import Product, find_product
from stock import services as stock
from stock.models import ShipmentLine

from .models import CustomerRequest, RequestLine, SpecLine, Specification

# --- Спецификации ---


def spec_usage(spec_line_ids=None):
    """{spec_line_id: отгружено шт}."""
    qs = ShipmentLine.objects.filter(spec_line__isnull=False)
    if spec_line_ids is not None:
        qs = qs.filter(spec_line_id__in=spec_line_ids)
    return {r["spec_line_id"]: r["q"] for r in qs.values("spec_line_id").annotate(q=Sum("qty"))}


def active_spec_lines(customer, on_date, product_ids=None):
    """Строки действующих спецификаций заказчика на дату: {product_id: [строки, новые первыми]}."""
    qs = SpecLine.objects.filter(spec__contract__customer=customer, spec__closed=False, spec__date__lte=on_date).filter(
        Q(spec__valid_until__isnull=True) | Q(spec__valid_until__gte=on_date)
    ).select_related("spec__contract").order_by("-spec__date", "-spec_id")
    if product_ids is not None:
        qs = qs.filter(product_id__in=product_ids)
    result = defaultdict(list)
    for line in qs:
        result[line.product_id].append(line)
    return result


def apply_specs(customer, on_date, rows, spec=None):
    """Привязывает строки отгрузки к спецификации и подставляет цену. Возвращает предупреждения.

    Берётся выбранная спецификация, иначе самая новая действующая, где есть изделие.
    Пустая цена — из спецификации. Превышение лимита не запрещено, но предупреждаем.
    """
    warnings = []
    product_ids = [r["product"].pk for r in rows]
    if spec is not None:
        chosen = {line.product_id: [line] for line in spec.lines.filter(product_id__in=product_ids).select_related("spec")}
    else:
        chosen = active_spec_lines(customer, on_date, product_ids)
    used = spec_usage([lines[0].pk for lines in chosen.values()])
    planned = defaultdict(int)
    for r in rows:
        lines = chosen.get(r["product"].pk)
        if not lines:
            if spec is not None:
                warnings.append(f"{r['product'].code}: нет в {spec} — строка без спецификации.")
            continue
        line = lines[0]
        r["spec_line"] = line
        if r.get("price_byn") is None:
            r["price_byn"] = line.price_byn
        elif r["price_byn"] != line.price_byn:
            warnings.append(f"{r['product'].code}: цена {r['price_byn']} отличается от спецификации ({line.price_byn}).")
        planned[line.pk] += r["qty"]
        if line.qty_limit is not None:
            after = used.get(line.pk, 0) + planned[line.pk]
            if after > line.qty_limit:
                warnings.append(f"{r['product'].code}: превышен лимит {line.spec} — лимит {line.qty_limit} шт, "
                                f"с этой отгрузкой {after} шт (+{after - line.qty_limit}).")
    return warnings


def spec_rows(spec):
    """Строки спецификации с отгружено / осталось по лимиту / на складе."""
    lines = list(spec.lines.select_related("product"))
    used = spec_usage([line.pk for line in lines])
    have = stock.on_hand([line.product_id for line in lines])
    for line in lines:
        line.shipped = used.get(line.pk, 0)
        line.left = line.qty_limit - line.shipped if line.qty_limit is not None else None
        line.percent = round(line.shipped * 100 / line.qty_limit) if line.qty_limit else None
        line.on_hand = have.get(line.product_id, 0)
    return lines


def link_past_shipments(spec):
    """Привязать к спецификации прошлые отгрузки этого заказчика (без спецификации) с даты спецификации."""
    count = 0
    for line in spec.lines.all():
        qs = ShipmentLine.objects.filter(spec_line__isnull=True, product_id=line.product_id,
                                         shipment__customer=spec.contract.customer, shipment__date__gte=spec.date)
        if spec.valid_until:
            qs = qs.filter(shipment__date__lte=spec.valid_until)
        count += qs.update(spec_line=line)
    return count


# --- Цена продажи изделия (для сумм в остатках и заявках) ---

def sale_prices(pairs, on_date=None):
    """{(product_id, customer_id): (цена без НДС, откуда)} для пар изделие-заказчик.

    Порядок: действующая спецификация заказчика → последняя отгрузка этому заказчику →
    последняя отгрузка любому заказчику. Нет ничего — (None, "").
    """
    import datetime as dt

    on_date = on_date or dt.date.today()
    pairs = set(pairs)
    product_ids = {p for p, _ in pairs}
    spec = {}
    qs = SpecLine.objects.filter(product_id__in=product_ids, spec__closed=False, spec__date__lte=on_date).filter(
        Q(spec__valid_until__isnull=True) | Q(spec__valid_until__gte=on_date)
    ).select_related("spec").order_by("spec__date", "spec_id")
    for line in qs:  # новые спецификации перезаписывают старые
        spec[line.product_id, line.spec.contract.customer_id] = (line.price_byn, f"спец. № {line.spec.number}")
    by_customer, anyone = {}, {}
    rows = (ShipmentLine.objects.filter(product_id__in=product_ids, price_byn__isnull=False)
            .order_by("shipment__date", "pk").values_list("product_id", "shipment__customer_id", "price_byn"))
    for product_id, customer_id, price in rows:
        by_customer[product_id, customer_id] = (price, "последняя продажа")
        anyone[product_id] = (price, "последняя продажа")
    result = {}
    for pair in pairs:
        result[pair] = spec.get(pair) or by_customer.get(pair) or anyone.get(pair[0]) or (None, "")
    return result


# --- Вставка таблицы из Excel / Word ---

_NUM = re.compile(r"^-?\d[\d\s ]*([.,]\d+)?$")


def _number(cell):
    cell = cell.strip()
    if not _NUM.match(cell):
        return None
    try:
        return Decimal(cell.replace(" ", "").replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


def _find(cell, customer):
    product = find_product(cell, customer)
    if product:
        return product
    m = re.search(r"\b(\d{2}\.\d{4})\b", cell)  # ЯПИБ 24.1119
    if m:
        qs = Product.objects.filter(yapib=m.group(1))
        if customer is not None and qs.count() > 1:
            qs = qs.filter(customer=customer)
        if qs.count() == 1:
            return qs.first()
    return None


@dataclass
class PastedRow:
    product: Product
    qty: int | None
    price: Decimal | None
    text: str


def parse_pasted(text, customer=None):
    """Строки таблицы, скопированной из Excel/Word: изделие (код, синоним или ЯПИБ), кол-во, цена.

    В каждой строке ищем ячейку с изделием; числа правее неё — количество и цена:
    если среди них есть «кол-во × цена = сумма», понимаем однозначно; иначе целое — количество,
    дробное — цена. Результат всегда показывается человеку для проверки.
    """
    rows, unknown = [], []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        cells = [c.strip() for c in re.split(r"\t|;|\s{3,}", raw) if c.strip()]
        found = next(((i, p) for i, c in enumerate(cells) if (p := _find(c, customer))), None)
        if not found:
            unknown.append(raw.strip())
            continue
        i, product = found
        nums = [n for n in (_number(c) for c in cells[i + 1:]) if n is not None and n > 0]
        qty = price = None
        for a in nums:
            for b in nums:
                if a is not b and a == int(a) and any(abs(a * b - c) <= Decimal("0.02") * max(c, 1) for c in nums if c is not a and c is not b):
                    qty, price = int(a), b
                    break
            if qty:
                break
        if qty is None:
            ints = [n for n in nums if n == int(n)]
            fracs = [n for n in nums if n != int(n)]
            if ints:
                qty = int(ints[0])
            if fracs:
                price = fracs[0]
            elif len(ints) > 1:
                price = ints[1]
        rows.append(PastedRow(product, qty, price, raw.strip()))
    return rows, unknown


# --- Заявки ---


def request_shipped(request_ids):
    """{(request_id, product_id): отгружено по заявке}."""
    qs = ShipmentLine.objects.filter(shipment__request_id__in=request_ids)
    return {(r["shipment__request_id"], r["product_id"]): r["q"]
            for r in qs.values("shipment__request_id", "product_id").annotate(q=Sum("qty"))}


def open_remaining(product_ids=None):
    """Невыполненный остаток открытых заявок: [(заявка, product_id, шт)] в порядке очереди."""
    lines = RequestLine.objects.filter(request__status=CustomerRequest.Status.OPEN).select_related("request")
    if product_ids is not None:
        lines = lines.filter(product_id__in=product_ids)
    lines = list(lines.order_by("request__date", "request_id"))
    shipped = request_shipped({line.request_id for line in lines})
    result = []
    for line in lines:
        left = line.qty - shipped.get((line.request_id, line.product_id), 0)
        if left > 0:
            result.append((line.request, line.product_id, left))
    return result


def reserved(product_ids=None):
    """{product_id: шт под открытые заявки}."""
    totals = defaultdict(int)
    for _, product_id, left in open_remaining(product_ids):
        totals[product_id] += left
    return totals


def request_rows(req):
    """Строки заявки: отгружено, осталось, на складе, резерв под более ранние заявки, свободно, в пути, не хватает."""
    lines = list(req.lines.select_related("product__customer"))
    ids = [line.product_id for line in lines]
    shipped = request_shipped([req.pk])
    have, transit = stock.on_hand(ids), stock.in_transit(ids)
    before = defaultdict(int)  # очередь: более ранние открытые заявки резервируют первыми
    for other, product_id, left in open_remaining(ids):
        if other.pk == req.pk:
            continue
        if (other.date, other.pk) < (req.date, req.pk):
            before[product_id] += left
    prices = sale_prices(((line.product_id, req.customer_id) for line in lines), req.date)
    for line in lines:
        if line.price_byn is not None:
            line.price, line.price_source = line.price_byn, "цена заявки"
        else:
            line.price, line.price_source = prices[line.product_id, req.customer_id]
        line.shipped = shipped.get((req.pk, line.product_id), 0)
        line.left = max(line.qty - line.shipped, 0)
        line.on_hand = have.get(line.product_id, 0)
        line.reserved_before = before[line.product_id] if req.is_open else 0
        line.free = max(line.on_hand - line.reserved_before, 0)
        line.covered = min(line.left, line.free)
        line.in_transit = transit.get(line.product_id, 0)
        line.short = line.left - line.covered
        line.state = "done" if not line.left else ("ok" if not line.short else ("transit" if line.short <= line.in_transit else "short"))
        p = line.price
        line.amount = line.qty * p if p is not None else None
        line.shipped_amount = min(line.shipped, line.qty) * p if p is not None else None
        line.left_amount = line.left * p if p is not None else None
        line.covered_amount = line.covered * p if p is not None else None
        line.short_amount = line.short * p if p is not None else None
    return lines


def request_summary(lines):
    total = sum(line.qty for line in lines)
    shipped = sum(min(line.shipped, line.qty) for line in lines)
    return {
        "qty": total, "shipped": shipped, "percent": round(shipped * 100 / total) if total else 0,
        "left": sum(line.left for line in lines), "covered": sum(line.covered for line in lines),
        "short": sum(line.short for line in lines),
        "amount": sum((line.amount or 0) for line in lines),
        "shipped_amount": sum((line.shipped_amount or 0) for line in lines),
        "left_amount": sum((line.left_amount or 0) for line in lines),
        "covered_amount": sum((line.covered_amount or 0) for line in lines),
        "short_amount": sum((line.short_amount or 0) for line in lines),
        "no_price": sum(1 for line in lines if getattr(line, "price", None) is None),
    }


def specs_for(customer):
    return Specification.objects.filter(contract__customer=customer).select_related("contract")
