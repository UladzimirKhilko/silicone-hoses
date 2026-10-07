"""Остатки и проведение документов.

Остаток по (изделие, партия) = принято − отгружено ± списания/корректировки.
Отгрузить или списать больше, чем лежит в партии, нельзя.
"""

from collections import defaultdict
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Coalesce

from .models import Adjustment, AdjustmentLine, Batch, ReceiptLine, ShipmentLine


class InsufficientStock(Exception):
    pass


def _filter(qs, product_ids):
    return qs.filter(product_id__in=product_ids) if product_ids is not None else qs


def batch_balances(product_ids=None):
    """{(product_id, batch_id): остаток} по принятым партиям."""
    bal = defaultdict(int)
    received = _filter(ReceiptLine.objects.filter(batch__status=Batch.Status.RECEIVED), product_ids)
    for row in received.values("product_id", "batch_id").annotate(q=Sum(Coalesce("qty_received", "qty_expected"))):
        bal[row["product_id"], row["batch_id"]] += row["q"]
    for row in _filter(ShipmentLine.objects, product_ids).values("product_id", "batch_id").annotate(q=Sum("qty")):
        bal[row["product_id"], row["batch_id"]] -= row["q"]
    adj = _filter(AdjustmentLine.objects, product_ids).values("product_id", "batch_id", "adjustment__kind").annotate(q=Sum("qty"))
    for row in adj:
        sign = 1 if row["adjustment__kind"] == Adjustment.Kind.INVENTORY_PLUS else -1
        bal[row["product_id"], row["batch_id"]] += sign * row["q"]
    return bal


def on_hand(product_ids=None):
    """{product_id: остаток на складе}."""
    totals = defaultdict(int)
    for (product_id, _), qty in batch_balances(product_ids).items():
        totals[product_id] += qty
    return totals


def in_transit(product_ids=None):
    """{product_id: шт в пути} — партии, ещё не принятые на складе."""
    qs = _filter(ReceiptLine.objects.filter(batch__status=Batch.Status.IN_TRANSIT), product_ids)
    return {r["product_id"]: r["q"] for r in qs.values("product_id").annotate(q=Sum("qty_expected"))}


def pick_batches(slots, qty):
    """Выбор партий для отгрузки. slots — [(партия, остаток)] от старой к новой.

    Сначала — самая старая партия, где хватает всего количества (одна партия = один номер
    в паспорте; остатки образцов из старых поставок не «подмешиваются»). Если ни одной такой
    нет — по порядку из нескольких партий.
    """
    for batch, have in slots:
        if have >= qty:
            return [(batch, qty)]
    result, left = [], qty
    for batch, have in slots:
        take = min(left, have)
        if take > 0:
            result.append((batch, take))
            left -= take
        if not left:
            return result
    return None


def allocate(product, qty, balances=None):
    """Раскладывает количество по партиям (см. pick_batches). [(batch, qty), …]"""
    balances = balances if balances is not None else batch_balances([product.pk])
    batches = Batch.objects.filter(pk__in=[b for (p, b), q in balances.items() if p == product.pk and q > 0])
    slots = [(b, balances[product.pk, b.pk]) for b in batches.order_by(F("received_date").asc(nulls_last=True), "pk")]
    result = pick_batches(slots, qty)
    if result is None:
        raise InsufficientStock(f"{product}: нужно {qty} шт, на складе {sum(q for _, q in slots)} шт.")
    return result


def _spread(lines, balances):
    """Строки [{product, qty, batch?, …}] → строки с конкретными партиями; проверка остатков."""
    out = []
    for line in lines:
        product, qty, batch = line["product"], line["qty"], line.get("batch")
        if batch is not None:
            avail = balances.get((product.pk, batch.pk), 0)
            if qty > avail:
                raise InsufficientStock(f"{product}: в партии «{batch}» осталось {avail} шт, а нужно {qty}.")
            balances[product.pk, batch.pk] = avail - qty
            out.append(line)
            continue
        for b, q in allocate(product, qty, balances):
            out.append({**line, "batch": b, "qty": q})
            balances[product.pk, b.pk] -= q
    return out


@transaction.atomic
def post_shipment(shipment, lines):
    """Создаёт строки отгрузки. Партию можно не указывать — возьмётся самая старая."""
    balances = batch_balances([line["product"].pk for line in lines])
    for line in _spread(lines, balances):
        ShipmentLine.objects.create(
            shipment=shipment, product=line["product"], batch=line["batch"], qty=line["qty"], price_byn=line.get("price_byn")
        )


@transaction.atomic
def post_adjustment(adjustment, lines):
    if adjustment.kind == Adjustment.Kind.INVENTORY_PLUS:
        for line in lines:
            batch = line.get("batch") or latest_batch(line["product"])
            if batch is None:
                raise InsufficientStock(f"{line['product']}: нет ни одной принятой партии — укажите партию.")
            AdjustmentLine.objects.create(adjustment=adjustment, product=line["product"], batch=batch, qty=line["qty"])
        return
    balances = batch_balances([line["product"].pk for line in lines])
    for line in _spread(lines, balances):
        AdjustmentLine.objects.create(adjustment=adjustment, product=line["product"], batch=line["batch"], qty=line["qty"])


def latest_batch(product):
    line = (
        ReceiptLine.objects.filter(product=product, batch__status=Batch.Status.RECEIVED)
        .select_related("batch")
        .order_by(F("batch__received_date").desc(nulls_last=True), "-batch_id")
        .first()
    )
    return line.batch if line else None


@dataclass
class Movement:
    date: object
    kind: str
    title: str
    url: str
    batch: Batch
    qty: int
    balance: int = 0


def movements(product):
    """Лента движений изделия с остатком после каждой операции (старые сверху)."""
    from django.urls import reverse

    items = []
    for r in ReceiptLine.objects.filter(product=product, batch__status=Batch.Status.RECEIVED).select_related("batch"):
        qty = r.qty_received if r.qty_received is not None else r.qty_expected
        items.append(Movement(r.batch.received_date, "in", str(r.batch), reverse("stock:batch_detail", args=[r.batch_id]), r.batch, qty))
    for s in ShipmentLine.objects.filter(product=product).select_related("shipment__customer", "batch"):
        items.append(Movement(s.shipment.date, "out", str(s.shipment), reverse("stock:shipment_detail", args=[s.shipment_id]), s.batch, -s.qty))
    for a in AdjustmentLine.objects.filter(product=product).select_related("adjustment", "batch"):
        kind = "in" if a.adjustment.sign > 0 else "adj"
        items.append(Movement(a.adjustment.date, kind, str(a.adjustment), reverse("stock:adjustment_detail", args=[a.adjustment_id]), a.batch, a.adjustment.sign * a.qty))
    order = {"in": 0, "adj": 1, "out": 2}
    items.sort(key=lambda m: (m.date is None, m.date, order[m.kind]))
    running = 0
    for m in items:
        running += m.qty
        m.balance = running
    return items
