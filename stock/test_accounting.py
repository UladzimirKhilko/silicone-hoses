"""Отчёт бухгалтерии (1С) «Отчет о движении матценностей» → сервис (import_accounting)."""

import datetime as dt
from decimal import Decimal

import openpyxl
import pytest
from django.core.management import call_command

from catalog.models import Customer, Product

from . import services
from .accounting import parse_ledger
from .models import Adjustment, Batch, ReceiptLine, Shipment


def _row(name, unit="", opening=(None, None), receipt=(None, None), issue=(None, None), closing=(None, None)):
    return [None, name, None, None, unit, *opening, *receipt, *issue, None, *closing]


LEDGER = [
    [None, 'ОАО "БПА БЕЛСТРОЙИНДУСТРИЯ"'],
    [None] * 7 + ["Отчет о движении матценностей "],
    [None] * 7 + [" с 01.01.26  по 28.10.26"],
    [None, "Склады: Склад  (ТЭСО)   "],
    [None, "Товар/Партия/Документ", None, None, "Ед.\nизм", "Начальный остаток", None, "Приход", None, "Расход", None, "Цена",
     "Конечный остаток"],
    _row("Патрубок силиконовый BSI D75L100-2", "шт", (100, 345), (20, 80), (85, 300), (35, 125)),
    _row("Приходная 09.06.26 TS9000001 Qinghe County Tushun Auto Parts CO.,LTD.", receipt=(20, 60)),
    _row("Приходная 09.06.26 TS9000001Там 2010-таможенная пошлина", receipt=(None, 5)),
    _row("Приходная 09.06.26 TS9000001Тра ИНТРАГЛОБ", receipt=(None, 15)),
    _row("Расходная 19.03.26 1247029 МТЗ", issue=(80, 280)),
    _row("Расходная 12.05.26 1247286 МТЗ", issue=(5, 20)),
    _row("Патрубок силиконовый BSI  89*1000", "шт", (1, 63.95), closing=(1, 63.95)),
]


@pytest.fixture
def ledger_file(tmp_path):
    wb = openpyxl.Workbook()
    for row in LEDGER:
        wb.active.append(row)
    path = tmp_path / "Отчет.xlsx"
    wb.save(path)
    return str(path)


def test_parse_ledger(ledger_file):
    from .importers import read_rows

    ledger = parse_ledger(read_rows(open(ledger_file, "rb")))
    assert (ledger.date_from, ledger.date_to) == (dt.date(2026, 1, 1), dt.date(2026, 10, 28))
    item = ledger.items[0]
    assert (item.opening_qty, item.closing_qty) == (100, 35)
    assert item.receipts() == {"TS9000001": (dt.date(2026, 6, 9), 20, Decimal(80))}  # завод + пошлина + доставка
    assert [(d.number, d.party, d.qty) for d in item.issues()] == [("1247029", "МТЗ", 80), ("1247286", "МТЗ", 5)]


def test_import_accounting(db, ledger_file, monkeypatch):
    from .management.commands import import_accounting as cmd

    monkeypatch.setattr(cmd, "SAMPLE_TTNS", {"1247286": "опытные образцы"})
    mtz, belaz = Customer.objects.create(name="МТЗ"), Customer.objects.create(name="БелАЗ")
    d75 = Product.objects.create(code="BSI D75L100-2", customer=mtz)
    Product.objects.create(code="BSI D89L1000-2", customer=belaz)  # в 1С 1 шт, в сервисе 0 — образец без документов
    old = Batch.objects.create(status=Batch.Status.RECEIVED, received_date=dt.date(2025, 9, 18))
    ReceiptLine.objects.create(batch=old, product=d75, qty_expected=110, qty_received=110)
    new = Batch.objects.create(invoice="TS9000001")  # в пути
    ReceiptLine.objects.create(batch=new, product=d75, qty_expected=20)
    excel = Shipment.objects.create(customer=mtz, date=dt.date(2026, 3, 19))  # из Excel: без ТТН и с ошибкой
    services.post_shipment(excel, [{"product": d75, "qty": 86, "price_byn": Decimal("8.14")}])

    call_command("import_accounting", ledger_file)  # проверка — ничего не пишет
    assert not Adjustment.objects.exists() and Batch.objects.get(pk=new.pk).status == Batch.Status.IN_TRANSIT

    for _ in range(2):
        call_command("import_accounting", ledger_file, "--apply")
    new.refresh_from_db()
    line = new.lines.get()
    assert (new.status, new.received_date, line.qty_received, line.landed_price_byn) == (
        Batch.Status.RECEIVED, dt.date(2026, 6, 9), 20, Decimal("4.00"))  # 80 BYN / 20 шт
    assert old.lines.get().landed_price_byn == Decimal("3.45")  # 345 / 100 — учётная цена остатка на начало
    opening = Adjustment.objects.get(kind=Adjustment.Kind.INVENTORY_MINUS)  # 110 в сервисе, 100 в 1С
    assert opening.date == dt.date(2025, 12, 31) and opening.lines.get().qty == 10
    excel.refresh_from_db()
    assert excel.ttn_number == "1247029" and sum(line.qty for line in excel.lines.all()) == 80
    sample = Adjustment.objects.get(kind=Adjustment.Kind.FREE_SAMPLE)
    assert (sample.customer, sample.lines.get().qty, sample.date) == (mtz, 5, dt.date(2026, 5, 12))
    assert services.on_hand([d75.pk])[d75.pk] == 35  # как в 1С на конец
