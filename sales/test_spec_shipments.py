"""Договор, спецификации и отгрузка из таблицы исполнения спецификаций (import_spec_shipments)."""

import datetime as dt
from decimal import Decimal

import openpyxl
import pytest
from django.core.management import call_command

from catalog.models import Customer, Product
from stock.models import Batch, ReceiptLine, Shipment

from .models import Contract, SpecLine


def _workbook(path):
    wb = openpyxl.Workbook()
    wb.active.title = "Движение патрубков"
    ws = wb.create_sheet("Спецификации 1 и 2")
    for row in [
        ["Исполнение Спецификаций №1 и №2 к Договору № 09/064 от 23.03.2026"],
        ["На основании расходной накладной №3077121 ЕМ от 10.06.2026."],
        [],
        ["Наименование", "Цена без\nНДС, руб.", "СПЕЦИФИКАЦИЯ № 1 (30 шт)", None, None, None, None, "СПЕЦИФИКАЦИЯ № 2 (5 шт)"],
        [None, None, "Кол-во по\nспец., шт", "Отгружено,\nшт", None, None, None, "Кол-во по\nспец., шт", "Отгружено,\nшт"],
        ["Патрубок силиконовый BSI D63L80-2", 6.9, 20, 8, None, None, None, 5, 5],
        ["Патрубок силиконовый BSI D50L100-2", 6.54, 10, 0, None, None, None, 0, 0],
        ["ИТОГО", None, 30, 8, None, None, None, 5, 5],
        [],
        ["1. «Отгружено» — из пометок в накладной: «Спецификация №1 от 23.03.26» и «Спецификация №2 от 23.03.26»."],
    ]:
        ws.append(row)
    wb.save(path)


@pytest.fixture
def amk(db):
    amk = Customer.objects.create(name="Амкадор")
    Product.objects.create(code="BSI D63L80-2", customer=Customer.objects.create(name="МТЗ"))  # тот же код у МТЗ
    d63 = Product.objects.create(code="BSI D63L80-2", customer=amk)
    d50 = Product.objects.create(code="BSI D50L100-2", customer=amk)
    batch = Batch.objects.create(status=Batch.Status.RECEIVED, received_date=dt.date(2026, 4, 21))
    for p in (d63, d50):
        ReceiptLine.objects.create(batch=batch, product=p, qty_expected=20, qty_received=20)
    return amk, d63


def test_import_spec_shipments(tmp_path, amk):
    customer, d63 = amk
    _workbook(tmp_path / "движение.xlsx")
    call_command("import_spec_shipments", str(tmp_path / "движение.xlsx"), "--customer", "Амкадор")
    assert not Contract.objects.exists()  # проверка — ничего не пишет
    for _ in range(2):
        call_command("import_spec_shipments", str(tmp_path / "движение.xlsx"), "--customer", "Амкадор", "--apply")
    contract = Contract.objects.get()
    assert (contract.customer, contract.number, contract.date) == (customer, "09/064", dt.date(2026, 3, 23))
    assert sorted(SpecLine.objects.values_list("spec__number", "product__code", "qty_limit", "price_byn")) == [
        ("1", "BSI D50L100-2", 10, Decimal("6.54")), ("1", "BSI D63L80-2", 20, Decimal("6.90")), ("2", "BSI D63L80-2", 5, Decimal("6.90"))]
    shipment = Shipment.objects.get()
    assert (shipment.date, shipment.ttn_number) == (dt.date(2026, 6, 10), "3077121 ЕМ")
    assert sorted((line.product, line.qty, line.spec_line.spec.number) for line in shipment.lines.all()) == [(d63, 5, "2"), (d63, 8, "1")]
