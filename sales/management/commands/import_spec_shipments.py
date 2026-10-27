"""Договор, спецификации и отгрузка из таблицы «Исполнение спецификаций» (учёт Амкодор-Пинск, 27.10.2026).

    python manage.py import_spec_shipments "Движение патрубков.xlsx" --customer Амкадор           # проверка
    python manage.py import_spec_shipments "Движение патрубков.xlsx" --customer Амкадор --apply

Лист «Спецификации …»: в заголовке — договор («к Договору № 09/064 от 23.03.2026») и расходная
накладная («накладной №3077121 ЕМ от 10.06.2026»); по строкам — изделие, цена без НДС и по каждой
спецификации «Кол-во по спец.» и «Отгружено». Создаёт договор, спецификации (цена, лимит = кол-во)
и одну отгрузку по накладной со строками, привязанными к спецификациям. Повторный запуск ничего
не дублирует: отгрузка с этим номером накладной уже есть — не создаётся.
"""

import datetime as dt
import re
from decimal import Decimal

import openpyxl
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import Customer, find_product
from sales.models import Contract, SpecLine, Specification
from stock import services
from stock.models import Shipment


def _date(text):
    d, m, y = text.split(".")
    return dt.date(int(y) + (2000 if len(y) == 2 else 0), int(m), int(d))


def _dec(value):
    return Decimal(str(value)).quantize(Decimal("0.01")) if value not in (None, "") else None


def read_sheet(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = next((ws for ws in wb if ws.title.lower().startswith("спецификац")), None)
    if ws is None:
        raise CommandError("Нет листа «Спецификации …»")
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    text = " ".join(str(c) for r in rows for c in r if c is not None)
    contract = re.search(r"Договор\w*\s*№\s*([\w/\-]+)\s*от\s*(\d\d\.\d\d\.\d{2,4})", text)
    waybill = re.search(r"накладн\w*\s*№\s*(\d+(?:\s*[А-ЯA-Z]{1,3})?)\s*от\s*(\d\d\.\d\d\.\d{4})", text)
    if not contract or not waybill:
        raise CommandError("Не нашёл в заголовке номер договора или расходной накладной")
    spec_dates = {n: _date(d) for n, d in re.findall(r"Спецификаци\w*\s*№\s*(\d+)\s*от\s*(\d\d\.\d\d\.\d{2,4})", text)}
    head = next(i for i, r in enumerate(rows) if str(r[0] or "").strip().lower().startswith("наименование"))
    specs = [(m.group(1), col) for col, c in enumerate(rows[head]) if (m := re.match(r"СПЕЦИФИКАЦИЯ\s*№\s*(\d+)", str(c or "")))]
    lines = []
    for r in rows[head + 2:]:
        name = str(r[0] or "").strip()
        if not name or name.upper().startswith("ИТОГО"):
            break
        for number, col in specs:  # в блоке спецификации: кол-во по спец., отгружено
            lines.append({"name": name, "price": _dec(r[1]), "spec": number, "limit": int(r[col] or 0),
                          "shipped": int(r[col + 1] or 0)})
    return {"contract": (contract.group(1), _date(contract.group(2))), "waybill": (waybill.group(1), _date(waybill.group(2))),
            "spec_dates": spec_dates, "lines": lines}


class Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "Загрузить договор, спецификации и отгрузку из таблицы исполнения спецификаций"

    def add_arguments(self, parser):
        parser.add_argument("xlsx")
        parser.add_argument("--customer", required=True)
        parser.add_argument("--apply", action="store_true", help="Записать в базу (без него — только проверка)")

    def handle(self, xlsx, customer, apply, **opts):
        customer = Customer.objects.filter(name=customer).first() or self.fail(f"Нет заказчика «{customer}»")
        data = read_sheet(xlsx)
        try:
            with transaction.atomic():
                self.load(customer, data)
                if not apply:
                    raise Rollback
        except Rollback:
            self.stdout.write(self.style.WARNING("Проверка: в базу ничего не записано. Для записи добавьте --apply."))
        else:
            self.stdout.write(self.style.SUCCESS("Записано."))

    def fail(self, message):
        raise CommandError(message)

    def load(self, customer, data):
        products = {}
        for line in data["lines"]:
            if line["name"] not in products:
                products[line["name"]] = find_product(line["name"], customer) or self.fail(f"Нет изделия «{line['name']}»")
        number, date = data["contract"]
        contract, created = Contract.objects.get_or_create(customer=customer, number=number, defaults={"date": date})
        self.stdout.write(f"{contract}: {'создан' if created else 'уже есть'}")
        specs = {}
        for spec_no in dict.fromkeys(line["spec"] for line in data["lines"]):
            spec, created = Specification.objects.get_or_create(
                contract=contract, number=spec_no, defaults={"date": data["spec_dates"].get(spec_no, date)})
            specs[spec_no] = spec
            spec_lines = [line for line in data["lines"] if line["spec"] == spec_no and line["limit"]]
            for line in spec_lines:
                SpecLine.objects.update_or_create(spec=spec, product=products[line["name"]],
                                                  defaults={"price_byn": line["price"], "qty_limit": line["limit"]})
            self.stdout.write(f"  {spec}: {len(spec_lines)} поз., {sum(line['limit'] for line in spec_lines)} шт")

        ttn, ship_date = data["waybill"]
        if Shipment.objects.filter(customer=customer, ttn_number=ttn).exists():
            self.stdout.write(f"Отгрузка по накладной {ttn}: уже есть")
            return
        shipment = Shipment.objects.create(customer=customer, date=ship_date, ttn_number=ttn,
                                           notes=f"Расходная накладная №{ttn} от {ship_date:%d.%m.%Y}")
        rows = [{"product": products[line["name"]], "qty": line["shipped"], "price_byn": line["price"],
                 "spec_line": SpecLine.objects.get(spec=specs[line["spec"]], product=products[line["name"]])}
                for line in data["lines"] if line["shipped"]]
        try:
            services.post_shipment(shipment, rows)
        except services.InsufficientStock as e:
            raise CommandError(f"Отгрузка не проходит по остаткам: {e}") from e
        amount = sum(r["qty"] * r["price_byn"] for r in rows)
        self.stdout.write(f"Отгрузка {ship_date:%d.%m.%Y}, накладная {ttn}: {sum(r['qty'] for r in rows)} шт, {amount} руб. без НДС")
