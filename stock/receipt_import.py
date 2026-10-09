"""Поставка из файлов завода: сопоставление строк инвойса и упаковочного листа с изделиями,
сверка количеств, создание партии «в пути» (docs/06, этап 4).

Изделие определяется только по коду чертежа или подтверждённому синониму (docs/09). Всё
остальное — выбор человека; выбранное написание запоминается как синоним.
"""

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction

from catalog.codes import normalize_name
from catalog.models import Product, ProductAlias, find_product

from .models import Batch, CartonSpec, ReceiptLine


@dataclass
class DraftRow:
    key: str
    invoice_name: str = ""
    pl_name: str = ""
    pl_mark: str = ""
    qty_invoice: int | None = None
    qty_pl: int | None = None
    price: str | None = None
    product_id: int | None = None
    candidate_ids: list = field(default_factory=list)
    cartons: list = field(default_factory=list)  # [{qty_per_carton, cartons, gross_kg, net_kg}]
    in_mixed: int = 0
    note: str = ""

    @property
    def qty(self):
        return self.qty_invoice if self.qty_invoice is not None else self.qty_pl

    @property
    def qty_mismatch(self):
        return self.qty_invoice is not None and self.qty_pl is not None and self.qty_invoice != self.qty_pl


def _match(name):
    """(изделие, кандидаты): изделие — если однозначно; кандидаты — изделия с тем же кодом."""
    product = find_product(name)
    if product:
        return product.pk, []
    same_code = list(Product.objects.filter(code_key=normalize_name(name)).values_list("pk", flat=True))
    return None, same_code


def build_draft(invoice=None, packing=None):
    rows, by_product = [], {}

    def row_for(name, source):
        product_id, candidates = _match(name)
        if product_id and product_id in by_product:
            return by_product[product_id]
        row = DraftRow(key=f"{source}:{len(rows)}", product_id=product_id, candidate_ids=candidates)
        rows.append(row)
        if product_id:
            by_product[product_id] = row
        return row

    for line in (invoice.lines if invoice else []):
        row = row_for(line.name, "inv")
        row.invoice_name = row.invoice_name or line.name
        row.qty_invoice = (row.qty_invoice or 0) + line.qty
        if line.price is not None:
            row.price = str(line.price)
    for item in (packing.items.values() if packing else []):
        row = row_for(item.name, "pl")
        row.pl_name, row.pl_mark = item.name, item.mark
        row.qty_pl = (row.qty_pl or 0) + item.qty
        row.in_mixed += item.in_mixed
        row.cartons += [
            {"qty_per_carton": c.qty_per_carton, "cartons": c.cartons,
             "gross_kg": str(c.gross_kg) if c.gross_kg is not None else None,
             "net_kg": str(c.net_kg) if c.net_kg is not None else None}
            for c in item.cartons
        ]
    rows = _pair_ambiguous(rows)
    header = {}
    if invoice:
        header.update(invoice=invoice.number, invoice_date=invoice.date.isoformat() if invoice.date else "",
                      contract=invoice.contract, currency=invoice.currency, invoice_total=str(invoice.total))
    if packing:
        header.update(pl_invoice=packing.invoice, total_cartons=packing.total_cartons, total_qty=packing.total_qty,
                      total_gross=str(packing.total_gross or ""), total_net=str(packing.total_net or ""),
                      mixed=[(mark, items) for mark, items in packing.mixed_cartons])
        header.setdefault("invoice", packing.invoice)
    return {"header": header, "rows": [asdict(r) for r in rows]}


def _absorb(target, row):
    """Слить строку из одного документа в строку из другого (одно и то же изделие)."""
    if row.qty_invoice is not None:
        target.invoice_name, target.qty_invoice, target.price = row.invoice_name, row.qty_invoice, row.price
    if row.qty_pl is not None:
        target.pl_name, target.pl_mark, target.qty_pl = row.pl_name, row.pl_mark, row.qty_pl
        target.cartons, target.in_mixed = row.cartons, row.in_mixed


def _pair_ambiguous(rows):
    """Строка с кодом, который есть у нескольких изделий (BSI D63L80-2 у МТЗ и Амкадора), не опознаётся сама.
    Её пара из другого документа:
    - опознана подтверждённым синонимом, и код изделия совпадает → это и есть изделие;
    - тоже не опознана, с тем же кодом → одна строка, изделие выберет человек один раз."""
    products = Product.objects.in_bulk([r.product_id for r in rows if r.product_id])
    result = list(rows)
    for row in rows:
        if row.product_id or row not in result:
            continue
        from_invoice = row.qty_invoice is not None
        key = normalize_name(row.invoice_name if from_invoice else row.pl_name)
        for other in result:
            if other is row or (other.qty_invoice is not None) == from_invoice:
                continue  # пару ищем только в другом документе
            if other.qty_invoice is not None and other.qty_pl is not None:
                continue  # уже полная строка
            if other.product_id in row.candidate_ids and products[other.product_id].code_key == key:
                _absorb(other, row)
                other.note = "изделие определено по синониму из другого документа"
                result.remove(row)
                break
            other_key = normalize_name(other.invoice_name if other.qty_invoice is not None else other.pl_name)
            if not other.product_id and other_key == key:
                _absorb(other, row)
                result.remove(row)
                break
    return result


def rows_from(draft):
    return [DraftRow(**r) for r in draft["rows"]]


def draft_warnings(draft, rows=None):
    rows = rows if rows is not None else rows_from(draft)
    h, warnings = draft["header"], []
    if h.get("invoice") and h.get("pl_invoice") and h["invoice"] != h["pl_invoice"]:
        warnings.append(f"Номер в инвойсе ({h['invoice']}) и в упаковочном листе ({h['pl_invoice']}) не совпадают.")
    if h.get("invoice") and Batch.objects.filter(invoice=h["invoice"]).exists():
        warnings.append(f"Поставка по инвойсу {h['invoice']} уже есть в сервисе — не загружаете ли второй раз?")
    for r in rows:
        if r.qty_mismatch:
            warnings.append(f"{r.invoice_name or r.pl_name}: в инвойсе {r.qty_invoice} шт, в упаковочном листе {r.qty_pl} шт.")
        elif r.qty_invoice is None and h.get("invoice_total"):
            warnings.append(f"{r.pl_name}: есть в упаковочном листе, нет в инвойсе.")
        elif r.qty_pl is None and h.get("total_qty"):
            warnings.append(f"{r.invoice_name}: есть в инвойсе, нет в упаковочном листе.")
    if h.get("total_qty") is not None:
        listed = sum(r.qty_pl or 0 for r in rows)
        if listed != h["total_qty"]:
            warnings.append(f"Сумма строк упаковочного листа {listed} шт не равна его итогу {h['total_qty']} шт.")
    return warnings


@transaction.atomic
def create_batch(draft, choices, header, remember=True, user=None):
    """choices: {row.key: product_id} для строк, где изделие выбрал человек.
    Возвращает партию «в пути»."""
    rows = rows_from(draft)
    merged = {}
    for r in rows:
        product_id = r.product_id or choices.get(r.key)
        if not product_id:
            raise ValueError(f"Не выбрано изделие для «{r.invoice_name or r.pl_name}».")
        m = merged.setdefault(product_id, {"inv": None, "pl": None, "price": None, "cartons": [], "names": []})
        # одна позиция может прийти двумя строками (из инвойса и из PL) — не складываем документы между собой
        if r.qty_invoice is not None:
            m["inv"] = (m["inv"] or 0) + r.qty_invoice
        if r.qty_pl is not None:
            m["pl"] = (m["pl"] or 0) + r.qty_pl
        m["price"] = m["price"] or r.price
        m["cartons"] += r.cartons
        m["names"] += [(r.invoice_name, ProductAlias.Source.INVOICE), (r.pl_name, ProductAlias.Source.PACKING_LIST),
                       (r.pl_mark, ProductAlias.Source.CARTON)]
    batch = Batch.objects.create(status=Batch.Status.IN_TRANSIT, **header)
    products = Product.objects.in_bulk(merged)
    for product_id, m in merged.items():
        product = products[product_id]
        ReceiptLine.objects.create(batch=batch, product=product, qty_expected=m["inv"] if m["inv"] is not None else m["pl"],
                                   price_cny=Decimal(m["price"]) if m["price"] else None)
        for c in m["cartons"]:
            if c["gross_kg"] is None:
                continue
            CartonSpec.objects.update_or_create(
                batch=batch, product=product, qty_per_carton=c["qty_per_carton"],
                defaults={"gross_kg": Decimal(c["gross_kg"]), "net_kg": Decimal(c["net_kg"]) if c["net_kg"] else None,
                          "cartons": c["cartons"], "source": f"упаковочный лист завода {batch.invoice}".strip()},
            )
        if remember:
            for name, source in m["names"]:
                _remember(product, name, source, user)
    return batch


def _remember(product, name, source, user):
    """Запомнить написание как синоним — если оно новое и ни с чем не конфликтует."""
    if not name or find_product(name) == product:
        return
    alias = ProductAlias(product=product, text=name, source=source, created_by=user)
    try:
        alias.full_clean(exclude=["normalized", "created_by"])
    except ValidationError:
        return
    alias.save()
