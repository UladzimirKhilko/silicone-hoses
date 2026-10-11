"""Цены и прибыль.

Три цены изделия (docs/15-prices-sales.md):
- цена завода, CNY — в строке партии (из инвойса), справочно;
- учётная цена, BYN/шт — в строке партии: цена завода × курс + доля расходов партии
  (доставка, таможня…), либо введена вручную;
- цена реализации, BYN без НДС — в строке отгрузки (из спецификации или прошлой отгрузки).

Прибыль строки отгрузки = (цена реализации − учётная цена её партии) × кол-во.
Если у партии учётной цены нет, берётся последняя известная учётная цена этого изделия
(так считал Excel: одна приходная цена на изделие) — такая себестоимость помечается «≈».

Учётные цены и прибыль видит только руководитель (право stock.view_costs).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import F

from .models import Batch, ReceiptLine

CENT = Decimal("0.01")


def can_see_costs(user):
    return user.is_authenticated and (user.is_superuser or user.has_perm("stock.view_costs"))


def money(value):
    return value.quantize(CENT, ROUND_HALF_UP) if value is not None else None


# --- Учётная цена партии ---

def calc_landed(lines, rate, extra_costs=None):
    """{line.pk: учётная цена} для строк с ценой завода.

    Цена = цена завода × курс × k, где k = 1 + расходы / (стоимость партии в BYN).
    То есть расходы делятся пропорционально стоимости строк. Возвращает (цены, k).
    """
    priced = [line for line in lines if line.price_cny]
    value = sum(line.price_cny * rate * line.qty for line in priced)
    k = 1 + (Decimal(extra_costs or 0) / value if value else 0)
    return {line.pk: money(line.price_cny * rate * k) for line in priced}, k


def landed_costs(pairs):
    """{(product_id, batch_id): (учётная цена, приблизительно?)} для пар изделие-партия."""
    pairs = set(pairs)
    product_ids = {p for p, _ in pairs}
    exact, latest = {}, {}
    rows = (
        ReceiptLine.objects.filter(product_id__in=product_ids, landed_price_byn__isnull=False)
        .order_by(F("batch__received_date").asc(nulls_last=True), "batch_id")
        .values_list("product_id", "batch_id", "landed_price_byn")
    )
    for product_id, batch_id, price in rows:
        exact[product_id, batch_id] = price
        latest[product_id] = price  # последняя по дате приёмки
    result = {}
    for pair in pairs:
        if pair in exact:
            result[pair] = (exact[pair], False)
        elif pair[0] in latest:
            result[pair] = (latest[pair[0]], True)
        else:
            result[pair] = (None, False)
    return result


# --- Прибыль по отгрузкам ---

@dataclass
class Totals:
    qty: int = 0
    amount: Decimal = Decimal(0)  # сумма реализации без НДС по всем строкам с ценой
    revenue: Decimal = Decimal(0)  # то же, но только строки с известной себестоимостью
    cost: Decimal = Decimal(0)
    qty_no_price: int = 0  # штук без цены реализации
    qty_no_cost: int = 0  # штук без учётной цены — в прибыль не входят
    approx: int = 0  # строк с приблизительной себестоимостью
    lines: int = 0

    @property
    def profit(self):
        return self.revenue - self.cost

    @property
    def margin(self):
        """Доля прибыли в выручке, %."""
        return (self.profit / self.revenue * 100).quantize(Decimal("0.1")) if self.revenue else None

    @property
    def complete(self):
        return not (self.qty_no_price or self.qty_no_cost)

    def add(self, line):
        self.lines += 1
        self.qty += line.qty
        if line.price_byn is None:
            self.qty_no_price += line.qty
            return
        self.amount += line.qty * line.price_byn
        if getattr(line, "unit_cost", None) is None:
            self.qty_no_cost += line.qty
            return
        # выручка и себестоимость — только по строкам, где известны обе цены, чтобы прибыль была честной
        self.revenue += line.qty * line.price_byn
        self.cost += line.qty * line.unit_cost
        self.approx += line.cost_approx


def with_costs(lines):
    """Проставляет строкам отгрузки unit_cost, cost_approx, cost, profit."""
    lines = list(lines)
    costs = landed_costs((line.product_id, line.batch_id) for line in lines)
    for line in lines:
        line.unit_cost, line.cost_approx = costs.get((line.product_id, line.batch_id), (None, False))
        line.cost = line.qty * line.unit_cost if line.unit_cost is not None else None
        line.profit = (line.amount - line.cost) if line.amount is not None and line.cost is not None else None
        line.unit_profit = (line.price_byn - line.unit_cost) if line.price_byn is not None and line.unit_cost is not None else None
    return lines


def totals(lines):
    t = Totals()
    for line in lines:
        t.add(line)
    return t


@dataclass
class Group:
    key: object
    label: str
    totals: Totals = field(default_factory=Totals)


def group_by(lines, key, label):
    groups = {}
    for line in lines:
        k = key(line)
        g = groups.get(k) or groups.setdefault(k, Group(k, label(line)))
        g.totals.add(line)
    return list(groups.values())


# --- Склад по учётным ценам ---

def stock_value(balances):
    """balances: {(product_id, batch_id): шт} → ({product_id: сумма}, итог, шт без цены)."""
    pairs = [pair for pair, q in balances.items() if q > 0]
    costs = landed_costs(pairs)
    by_product, missing = defaultdict(Decimal), 0
    for pair in pairs:
        price, _ = costs[pair]
        if price is None:
            missing += balances[pair]
        else:
            by_product[pair[0]] += price * balances[pair]
    return by_product, sum(by_product.values(), Decimal(0)), missing


# --- Цены изделия для карточки ---

def product_prices(product, balances):
    """Строки партий изделия: цена завода, учётная цена, остаток (новые сверху)."""
    lines = (
        ReceiptLine.objects.filter(product=product)
        .select_related("batch")
        .order_by(F("batch__received_date").desc(nulls_first=True), "-batch_id")
    )
    rows = []
    for line in lines:
        rows.append({
            "batch": line.batch, "price_cny": line.price_cny, "landed": line.landed_price_byn,
            "left": balances.get((product.pk, line.batch_id), 0) if line.batch.status == Batch.Status.RECEIVED else None,
        })
    return rows
