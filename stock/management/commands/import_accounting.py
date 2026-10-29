"""Отчёт бухгалтерии (1С) «Отчет о движении матценностей» → остатки и движение в сервисе.

    python manage.py import_accounting "Отчет.xls"            # проверка, ничего не меняет
    python manage.py import_accounting "Отчет.xls" --apply    # записать

Бухгалтерия — главный источник (решение 28.10.2026). Команда:
1. приходы: дата приёмки и учётная цена (завод + пошлина + оформление + доставка) по каждой поставке;
   поставка, которой нет в сервисе, создаётся принятой;
2. остатки на начало отчёта: если в сервисе на эту дату больше или меньше, чем у бухгалтерии, —
   одна корректировка «по инвентаризации» на день раньше, с пометкой; учётная цена остатка;
3. расходы: отгрузки по номерам ТТН — уже внесённые сверяются (без ТТН — ищутся по заказчику и дате,
   номер ТТН проставляется), количество правится по бухгалтерии; новых — создаются, цена — из
   спецификации или последней продажи этому заказчику; опытные образцы — списанием «бесплатные образцы»;
4. в конце сравнивает конечные остатки.
Повторный запуск ничего не дублирует.
"""

import csv
import datetime as dt
from collections import defaultdict
from decimal import Decimal
from functools import cache
from pathlib import Path

from django.conf import settings

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.codes import normalize_name
from catalog.models import Customer, Product
from sales import services as sales
from stock import services
from stock.accounting import parse_ledger
from stock.importers import read_rows
from stock.models import Adjustment, AdjustmentLine, Batch, ReceiptLine, Shipment, ShipmentLine

# Наименования бухгалтерии, отличающиеся от кодов каталога
NAMES = {"102*1000": "D102L1000-2", "60*1000": "D60L1000-2", "89*1000": "D89L1000-2", "90-60": "90/60",
         "141-50-63.5": "141-50/63.5", "110-50-63.5": "110-50/63.5", "D63.5": "63.5",
         "D90L180COR-2": "D90L180-COR-2", "D90L180COR-3": "D90L180-COR-3"}
INVOICES = {"TS260193": "TS2601093"}  # номер в 1С → номер инвойса в сервисе
CUSTOMERS = [("Амкодор", "Амкадор"), ("Торговый Дом МТЗ-Ставрополь", "ТД МТЗ-Ставрополь"), ("БЕЛАЗ", "БелАЗ"), ("МТЗ", "МТЗ")]
SAMPLE_TTNS = {  # опытные образцы: в 1С — расход по цене для таможни, денег не было
    "1247285": "опытный образец D80/90L140-3",
    "1247286": "опытные образцы по инвойсу TS2601093",
    "3077456": "соглашение № 59 от 12.08.2026",
    "3077636": "соглашение о передаче образцов D100L160-COR-3",
    "3077953": "соглашение № 30 от 11.04.2024",
}
# Опытные образцы, которые в 1С числились не на складе ТЭСО (в его отчёт не входят), — списываются по своей ТТН.
# До даты ТТН в сервисе они на складе, поэтому к остаткам ТЭСО на начало и конец отчёта прибавляются.
OTHER_STORE = [
    {"ttn": "3077953", "date": dt.date(2026, 10, 29), "customer": "МТЗ", "qty": 2,
     "notes": "ТТН № 3077953 ЮВ от 29.10.2026: опытные образцы МТЗ по соглашению № 30 от 11.04.2024 (без выставления счёта)",
     "codes": ("30-D38L95/55-1", "90-D38L150/140-1", "90-D53L210/140-1", "90-D70L110/130-2", "90-D90L120/120-3", "D102L80-2",
               "D50L100-3", "D54L80-1", "D60L80-2", "D60L80-3", "D63L80-2", "D63L80-3", "D70L100-2", "D75L100-2", "D75L100-3",
               "D80L100-3", "D90L120-2", "D90L120-3", "D90L180-COR-2", "D90L180-COR-3")},
]
# В 1С числятся, но фактически переданы БелАЗу без документов (списаны в сервисе)
KEEP_SERVICE = {"D60L1000-2", "D89L1000-2", "D102L1000-2"}
NOTE = "по отчёту бухгалтерии (1С)"
MTZ_PRICES = Path(settings.BASE_DIR) / "docs" / "data" / "mtz-stock-from-excel.csv"  # «Цена МТЗ» из Excel МТЗ


@cache
def mtz_prices():
    with open(MTZ_PRICES, newline="", encoding="utf-8") as fh:
        return {normalize_name(r["Изделие"]): Decimal(r["Цена МТЗ, BYN без НДС"]) for r in csv.DictReader(fh)
                if r["Цена МТЗ, BYN без НДС"] not in ("", "0")}


def code_key(name):
    key = normalize_name(name)
    return normalize_name(NAMES.get(key, key))


def customer_for(party):
    for word, name in CUSTOMERS:
        if word.lower() in party.lower():
            return Customer.objects.get_or_create(name=name)[0]
    raise ValueError(f"не знаю заказчика «{party}»")


def held_elsewhere(key, on_date):
    """Сколько штук изделия на дату лежит вне склада ТЭСО (OTHER_STORE)."""
    return sum(e["qty"] for e in OTHER_STORE if key in e["codes"] and e["date"] > on_date)


def balance(products, on_date):
    """Остаток изделий (одного кода) на дату — по документам сервиса."""
    ids = [p.pk for p in products]
    qty = sum(r.qty for r in ReceiptLine.objects.filter(product_id__in=ids, batch__status=Batch.Status.RECEIVED,
                                                         batch__received_date__lte=on_date))
    qty -= sum(r.qty for r in ShipmentLine.objects.filter(product_id__in=ids, shipment__date__lte=on_date))
    for line in AdjustmentLine.objects.filter(product_id__in=ids, adjustment__date__lte=on_date).select_related("adjustment"):
        qty += line.qty * line.adjustment.sign
    return qty


class Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "Загрузить остатки и движение из отчёта бухгалтерии (1С)"

    def add_arguments(self, parser):
        parser.add_argument("file")
        parser.add_argument("--apply", action="store_true", help="Записать в базу (без него — только проверка)")

    def handle(self, file, apply, **opts):
        with open(file, "rb") as fh:
            ledger = parse_ledger(read_rows(fh))
        self.stdout.write(f"Отчёт 1С с {ledger.date_from:%d.%m.%Y} по {ledger.date_to:%d.%m.%Y}: {len(ledger.items)} изделий")
        self.ledger = ledger
        try:
            with transaction.atomic():
                self.receipts()
                self.opening()
                self.issues()
                self.other_store()
                self.closing()
                if not apply:
                    raise Rollback
        except Rollback:
            self.stdout.write(self.style.WARNING("Проверка: в базу ничего не записано. Для записи добавьте --apply."))
        else:
            self.stdout.write(self.style.SUCCESS("Записано."))

    def products(self, item):
        products = list(Product.objects.filter(code_key=code_key(item.name)).select_related("customer"))
        if not products:
            raise CommandError(f"Нет изделия «{item.name}» в каталоге")
        return products

    # --- 1. Приходы ---

    def receipts(self):
        by_invoice = defaultdict(list)  # инвойс → [(изделия, дата, шт, сумма)]
        for item in self.ledger.items:
            for number, (date, qty, amount) in item.receipts().items():
                by_invoice[INVOICES.get(number, number)].append((self.products(item), date, qty, amount))
        self.stdout.write("\nПриходы:")
        for invoice, rows in sorted(by_invoice.items(), key=lambda kv: kv[1][0][1]):
            date = rows[0][1]
            batch = Batch.objects.filter(invoice=invoice).first()
            changes = []
            if batch is None:
                batch = Batch.objects.create(invoice=invoice, status=Batch.Status.RECEIVED, received_date=date,
                                             notes=f"Создано {NOTE}: приход {date:%d.%m.%Y}.")
                changes.append("новая партия")
            if batch.status != Batch.Status.RECEIVED or batch.received_date != date:
                changes.append(f"принята {date:%d.%m.%Y}" + (f" (было {batch.received_date:%d.%m.%Y})" if batch.received_date else ""))
                batch.status, batch.received_date = Batch.Status.RECEIVED, date
                batch.save()
            lines = {line.product.code_key: line for line in batch.lines.select_related("product")}
            for products, _, qty, amount in rows:
                line = lines.get(products[0].code_key)
                if line is None:
                    line = ReceiptLine(batch=batch, product=self.pick(products, "МТЗ"), qty_expected=qty)
                    changes.append(f"+ {line.product.code} {qty} шт")
                landed = (amount / qty).quantize(Decimal("0.01")) if qty else None
                if line.qty_received != qty:
                    if line.pk and line.qty_received is not None:
                        changes.append(f"{line.product.code}: принято {qty} вместо {line.qty_received}")
                    line.qty_received = qty
                if landed and line.landed_price_byn != landed:
                    line.landed_price_byn = landed
                    changes.append(f"учётная цена {line.product.code} {landed}")
                line.save()
            self.stdout.write(f"  {invoice} ({date:%d.%m.%Y}): " + ("; ".join(changes) if changes else "без изменений"))

    @staticmethod
    def pick(products, customer_name):
        return next((p for p in products if p.customer.name == customer_name), products[0])

    # --- 2. Остаток на начало ---

    def opening(self):
        """Остаток на начало: одна корректировка на разницу с 1С. Прежняя (от прошлой загрузки) пересчитывается."""
        day = self.ledger.date_from - dt.timedelta(days=1)
        prefix = f"Приведение к остаткам бухгалтерии на {self.ledger.date_from:%d.%m.%Y}"
        old = list(Adjustment.objects.filter(date=day, notes__startswith=prefix).prefetch_related("lines"))
        old_effect = defaultdict(int)  # что прежняя корректировка уже сделала с остатком изделия
        for adjustment in old:
            for line in adjustment.lines.all():
                old_effect[line.product_id] += line.qty * adjustment.sign
        wanted = {}
        costs = 0
        for item in self.ledger.items:
            products = self.products(item)
            key = products[0].code_key
            if item.opening_qty:
                costs += self.opening_cost(products, item, day)
            if key in KEEP_SERVICE:
                continue
            have = {p: balance([p], day) - old_effect[p.pk] for p in products}
            diff = item.opening_qty + held_elsewhere(key, day) - sum(have.values())
            if diff > 0:
                wanted[self.pick(products, "МТЗ"), Adjustment.Kind.INVENTORY_PLUS] = diff
            elif diff < 0:
                wanted[max(have, key=have.get), Adjustment.Kind.INVENTORY_MINUS] = -diff
        self.stdout.write(f"\nОстатки на {self.ledger.date_from:%d.%m.%Y}: учётная цена проставлена в {costs} строках партий")
        current = {(line.product, a.kind): line.qty for a in old for line in a.lines.select_related("product")}
        if current == wanted:
            self.stdout.write("  остатки совпадают с бухгалтерией" if not wanted else "  корректировка на начало — без изменений")
            return
        for adjustment in old:
            adjustment.delete()
        notes = (f"{prefix} ({NOTE}). Причина расхождения не установлена (отгрузки или образцы без документов в сервисе).")
        for kind, sign in ((Adjustment.Kind.INVENTORY_MINUS, "−"), (Adjustment.Kind.INVENTORY_PLUS, "+")):
            lines = [{"product": product, "qty": qty} for (product, k), qty in wanted.items() if k == kind]
            if not lines:
                continue
            adjustment = Adjustment.objects.create(kind=kind, date=day, notes=notes)
            services.post_adjustment(adjustment, lines)
            self.stdout.write(f"  корректировка {day:%d.%m.%Y}: " + ", ".join(f"{r['product'].code} {sign}{r['qty']}" for r in lines))
        if not wanted:
            self.stdout.write("  прежняя корректировка на начало удалена — остатки совпадают с бухгалтерией")

    def other_store(self):
        """Образцы вне склада ТЭСО — списанием «бесплатные образцы» по их ТТН."""
        for entry in OTHER_STORE:
            if Adjustment.objects.filter(kind=Adjustment.Kind.FREE_SAMPLE, notes__contains=entry["ttn"]).exists():
                continue
            customer = Customer.objects.get(name=entry["customer"])
            lines = [{"product": self.pick(list(Product.objects.filter(code_key=normalize_name(code)).select_related("customer")),
                                           entry["customer"]), "qty": entry["qty"]} for code in entry["codes"]]
            adjustment = Adjustment.objects.create(kind=Adjustment.Kind.FREE_SAMPLE, date=entry["date"], customer=customer,
                                                   notes=entry["notes"] + ".")
            services.post_adjustment(adjustment, lines)
            self.stdout.write(f"\nОбразцы {customer}, ТТН {entry['ttn']}: списано {sum(r['qty'] for r in lines)} шт ({entry['date']:%d.%m.%Y})")

    def opening_cost(self, products, item, day):
        """Учётная цена бухгалтерии — партиям, из которых остаток на начало (у кого её нет или другая)."""
        price = (item.opening_sum / item.opening_qty).quantize(Decimal("0.01"))
        balances = services.batch_balances([p.pk for p in products])
        changed = 0
        for line in ReceiptLine.objects.filter(product__in=products, batch__status=Batch.Status.RECEIVED,
                                               batch__received_date__lte=day):
            if balances.get((line.product_id, line.batch_id), 0) > 0 and line.landed_price_byn != price:
                line.landed_price_byn = price
                line.save(update_fields=["landed_price_byn"])
                changed += 1
        return changed

    # --- 3. Расходы ---

    def issues(self):
        docs = defaultdict(list)  # ТТН → [(изделия, документ)]
        for item in self.ledger.items:
            for doc in item.issues():
                docs[doc.number].append((self.products(item), doc))
        self.stdout.write("\nРасходы:")
        for ttn, rows in sorted(docs.items(), key=lambda kv: (kv[1][0][1].date, kv[0])):
            date, party = rows[0][1].date, rows[0][1].party
            customer = customer_for(party)
            wanted = defaultdict(int)
            products = {}
            for prods, doc in rows:
                own = next((p for p in prods if p.customer_id == customer.pk), None) or self.pick(prods, "МТЗ")
                wanted[own.code_key] += doc.qty
                products[own.code_key] = own
            label = f"  {date:%d.%m.%Y} ТТН {ttn} {customer}, {sum(wanted.values())} шт: "
            if ttn in SAMPLE_TTNS:
                self.stdout.write(label + self.samples(ttn, date, customer, wanted, products))
            else:
                self.stdout.write(label + self.shipment(ttn, date, customer, wanted, products))

    def samples(self, ttn, date, customer, wanted, products):
        if Adjustment.objects.filter(kind=Adjustment.Kind.FREE_SAMPLE, notes__contains=ttn).exists():
            return "образцы уже списаны"
        adjustment = Adjustment.objects.create(kind=Adjustment.Kind.FREE_SAMPLE, date=date, customer=customer,
                                               notes=f"ТТН № {ttn} от {date:%d.%m.%Y}: {SAMPLE_TTNS[ttn]} ({NOTE}).")
        services.post_adjustment(adjustment, [{"product": products[k], "qty": q} for k, q in wanted.items()])
        return "списаны как бесплатные образцы"

    def shipment(self, ttn, date, customer, wanted, products):
        shipment = Shipment.objects.filter(customer=customer, ttn_number__startswith=ttn).first()
        note = ""
        if shipment is None:
            shipment = self.same_day(customer, date, wanted)
            if shipment is not None:
                shipment.ttn_number = ttn
                shipment.save(update_fields=["ttn_number"])
                note = "проставлен № ТТН"
        if shipment is None:
            return self.new_shipment(ttn, date, customer, wanted, products)
        fixes = self.fix_quantities(shipment, wanted, products)
        priced = self.fill_prices(shipment)
        return "; ".join(n for n in (note, fixes, priced) if n) or "совпадает"

    @staticmethod
    def same_day(customer, date, wanted):
        """Уже внесённая отгрузка без ТТН (из Excel, по паспорту) — тот же заказчик, дата, набор изделий."""
        candidates = Shipment.objects.filter(customer=customer, date=date, ttn_number="")
        for shipment in candidates:
            if {line.product.code_key for line in shipment.lines.select_related("product")} == set(wanted):
                return shipment
        return None

    def fix_quantities(self, shipment, wanted, products):
        have = defaultdict(int)
        for line in shipment.lines.select_related("product"):
            have[line.product.code_key] += line.qty
        fixes = []
        for key in set(have) | set(wanted):
            if have[key] == wanted[key]:
                continue
            lines = list(shipment.lines.filter(product__code_key=key))
            keep = lines[0] if lines else None
            ShipmentLine.objects.filter(pk__in=[line.pk for line in lines]).delete()
            if wanted[key]:
                services.post_shipment(shipment, [{
                    "product": keep.product if keep else products[key], "qty": wanted[key],
                    "price_byn": keep.price_byn if keep else None, "spec_line": keep.spec_line if keep else None}])
            fixes.append(f"{(keep.product if keep else products[key]).code}: {have[key]} → {wanted[key]} шт")
        if fixes:
            shipment.notes = "; ".join(n for n in (shipment.notes, f"Количество исправлено {NOTE}") if n)
            shipment.save(update_fields=["notes"])
        return "; ".join(fixes)

    @staticmethod
    def prices(customer, date, rows):
        """Цена — из спецификации заказчика, иначе последней продажи ему. Возвращает изделия с ценой «по последней»."""
        sales.apply_specs(customer, date, rows)
        last = sales.sale_prices([(r["product"].pk, customer.pk) for r in rows if r.get("price_byn") is None], date)
        guessed = []
        for r in rows:
            if r.get("price_byn") is None:
                r["price_byn"] = last.get((r["product"].pk, customer.pk), (None, ""))[0]
                if r["price_byn"] is None and customer.name == "МТЗ":
                    r["price_byn"] = mtz_prices().get(r["product"].code_key)
                if r["price_byn"] is not None:
                    guessed.append(r["product"].code)
        return guessed

    def fill_prices(self, shipment):
        """Отгрузка из отчёта 1С, внесённая раньше спецификаций, — проставить цену и спецификацию."""
        lines = list(shipment.lines.filter(price_byn__isnull=True).select_related("product"))
        if not lines or NOTE not in shipment.notes:
            return ""
        rows = [{"product": line.product, "qty": line.qty} for line in lines]
        self.prices(shipment.customer, shipment.date, rows)
        filled = 0
        for line, row in zip(lines, rows):
            if row.get("price_byn") is not None:
                line.price_byn, line.spec_line = row["price_byn"], row.get("spec_line")
                line.save(update_fields=["price_byn", "spec_line"])
                filled += 1
        return f"проставлены цены в {filled} строках" if filled else ""

    def new_shipment(self, ttn, date, customer, wanted, products):
        rows = [{"product": products[k], "qty": q} for k, q in wanted.items()]
        guessed = self.prices(customer, date, rows)
        notes = f"Внесено {NOTE}."
        if guessed:
            notes += " Цена — по последней продаже или «Цене МТЗ» из Excel (в отчёте 1С цен нет): " + ", ".join(guessed) + "."
        shipment = Shipment.objects.create(customer=customer, date=date, ttn_number=ttn, notes=notes)
        services.post_shipment(shipment, rows)
        return "новая отгрузка"

    # --- 4. Конечный остаток ---

    def closing(self):
        diffs = []
        for item in self.ledger.items:
            products = self.products(item)
            have = balance(products, self.ledger.date_to) - held_elsewhere(products[0].code_key, self.ledger.date_to)
            if have != item.closing_qty:
                diffs.append(f"{products[0].code}: в сервисе {have}, в 1С {item.closing_qty}")
        self.stdout.write(f"\nКонечные остатки на {self.ledger.date_to:%d.%m.%Y}: " +
                          ("совпадают" if not diffs else "расходятся —\n  " + "\n  ".join(diffs)))
