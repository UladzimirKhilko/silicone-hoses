"""Подсказка заказа в Китай: чего не хватает под открытые заявки с учётом склада и товара в пути."""

import math
from collections import defaultdict

from django.db.models import F

from catalog.models import Product, ProductAlias
from stock import services as stock
from stock.models import CartonSpec, ReceiptLine

from .services import open_remaining

COLOR_EN = {"black": "black", "blue": "blue", "red": "red"}


def carton_sizes(product_ids):
    """{product_id: шт в основной коробке} — по последней партии, где есть данные о коробках."""
    result = {}
    specs = (CartonSpec.objects.filter(product_id__in=product_ids)
             .order_by(F("batch__received_date").asc(nulls_last=True), "batch_id", "cartons"))
    for spec in specs:
        result[spec.product_id] = spec.qty_per_carton  # последняя партия, больше всего коробок
    return result


def last_factory_prices(product_ids):
    result = {}
    lines = (ReceiptLine.objects.filter(product_id__in=product_ids, price_cny__isnull=False)
             .order_by(F("batch__received_date").asc(nulls_last=True), "batch_id"))
    for line in lines:
        result[line.product_id] = line.price_cny
    return result


def factory_names(product_ids):
    """Как изделие называет завод: написание из упаковочного листа и маркировка коробки."""
    names = defaultdict(dict)
    for alias in ProductAlias.objects.filter(product_id__in=product_ids).order_by("pk"):
        if alias.source in (ProductAlias.Source.PACKING_LIST, ProductAlias.Source.CARTON, ProductAlias.Source.FACTORY):
            names[alias.product_id][alias.source] = alias.text
    return names


def suggestions():
    """Строки «заказать»: изделие, нужно под заявки, есть, в пути, не хватает, предложено (до целой коробки)."""
    demand, sources = defaultdict(int), defaultdict(list)
    for req, product_id, left in open_remaining():
        demand[product_id] += left
        sources[product_id].append((req, left))
    ids = list(demand)
    have, transit = stock.on_hand(ids), stock.in_transit(ids)
    cartons = carton_sizes(ids)
    rows = []
    for product in Product.objects.filter(pk__in=ids).select_related("customer").order_by("customer__name", "code"):
        short = demand[product.pk] - have.get(product.pk, 0) - transit.get(product.pk, 0)
        if short <= 0:
            continue
        per = cartons.get(product.pk)
        rows.append({
            "product": product, "demand": demand[product.pk], "on_hand": have.get(product.pk, 0),
            "in_transit": transit.get(product.pk, 0), "short": short, "carton": per,
            "suggested": math.ceil(short / per) * per if per else short, "requests": sources[product.pk], "reason": "requests",
        })
    return rows


def low_stock_suggestions(exclude=()):
    """Заканчивается (отчёт этапа 6) — изделия, которых нет среди нехватки под заявки."""
    from reports.analytics import low_stock

    rows = []
    for r in low_stock():
        if r["product"].pk in exclude or not r["suggested"]:
            continue
        rows.append({
            "product": r["product"], "demand": 0, "on_hand": r["on_hand"], "in_transit": r["in_transit"], "short": r["suggested"],
            "carton": r["carton"], "suggested": r["suggested"], "requests": [], "reason": "low",
            "months": r["months"], "avg": r["avg"],
        })
    return rows


def wechat_text(lines, date):
    """Текст заказа для мессенджера (на английском, как общается завод)."""
    ids = [line["product"].pk for line in lines]
    names = factory_names(ids)
    out = [f"BSI order {date:%d.%m.%Y}", ""]
    for i, line in enumerate(lines, 1):
        product = line["product"]
        bits = []
        mark = names[product.pk].get(ProductAlias.Source.FACTORY) or names[product.pk].get(ProductAlias.Source.CARTON)
        if mark:
            bits.append(mark)
        if product.color in COLOR_EN:
            bits.append(COLOR_EN[product.color])
        if product.yapib:
            bits.append(f"drawing {product.yapib}")
        extra = f" ({', '.join(bits)})" if bits else ""
        out.append(f"{i}. {product.code}{extra} — {line['qty']} pcs")
    out += ["", f"Total: {len(lines)} items, {sum(line['qty'] for line in lines)} pcs.", "Drawings attached. Please confirm price and delivery time."]
    return "\n".join(out)
