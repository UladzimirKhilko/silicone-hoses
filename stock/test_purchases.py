"""Приходы из папки «Закупки»: команда import_purchases, инвойс в метрах."""

import datetime as dt
from decimal import Decimal

import openpyxl
import pytest
from django.core.management import call_command

from catalog.models import Customer, Product, ProductAlias

from . import receipt_import
from .importers import parse_document
from .management.commands import import_purchases as cmd
from .models import Batch, ReceiptLine


def _invoice(number, lines, unit=False):
    head = ["No.:", "ТНВЭД код", "Наименование", "К-во", "Цена за ед.\n(CNY)EXW", "Сумма\n(CNY)"]
    if unit:
        head.insert(4, "Ед. изм")
    rows = [[f"Инвойс № {number} к договору 09/185 от 02.11.2024    Дата: 2026/9/25"], head]
    for i, (name, qty, price) in enumerate(lines, 1):
        row = [i, 3917310008, f"Патрубок силиконовый BSI {name}", qty, price, qty * price]
        if unit:
            row.insert(4, "м/п")
        rows.append(row)
    return rows + [["Итого:"]]


def _ru_pl(number, lines):
    rows = [["Упаковочный лист"], ["Инвойс №", f"{number} к договору 09/185", "Дата:", "2026-07-15"],
            ["No.:", "Наименование (обозначение)", "К-вр\n(шт)", "Упаковка", "N.W\n(KGS)", "G.W\n(KGS)"]]
    for i, (name, qty, cartons) in enumerate(lines, 1):
        rows.append([i, f"Патрубок силиконовый {name}", qty, cartons, 10 * cartons, 11 * cartons])
    return rows + [["Общий итог", sum(q for _, q, _ in lines), sum(c for _, _, c in lines)]]


def _save(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(path)


@pytest.fixture
def belaz(db):
    return Customer.objects.create(name="БелАЗ")


def test_invoice_in_meters_becomes_pieces(belaz):
    Product.objects.create(code="BSI D10L10000-2", customer=belaz)
    Product.objects.create(code="BSI D89L1000-2", customer=belaz)
    kind, inv = parse_document(_invoice("JH-J-20260925", [("D10L10000-2", 1200, 7.35), ("D89L1000-2", 600, 35.28)], unit=True))
    assert kind == "invoice" and inv.number == "JH-J-20260925" and inv.lines[0].unit == "м/п"
    rows = receipt_import.rows_from(receipt_import.build_draft(inv))
    assert [(r.qty_invoice, Decimal(r.price)) for r in rows] == [(120, Decimal("73.50")), (600, Decimal("35.28"))]
    assert rows[0].note == "в инвойсе 1200 м → 120 шт по 10 м"
    # обычный инвойс в штуках не трогаем
    _, inv = parse_document(_invoice("TS1", [("D89L1000-2", 5, 95)]))
    assert receipt_import.rows_from(receipt_import.build_draft(inv))[0].qty_invoice == 5


def test_import_purchases(tmp_path, belaz, monkeypatch):
    mtz = Customer.objects.create(name="МТЗ")
    d89 = Product.objects.create(code="BSI D89L1000-2", customer=belaz)
    Product.objects.create(code="BSI D50L100-2", customer=Customer.objects.create(name="Амкадор"))
    d75 = Product.objects.create(code="BSI D75L100-2", customer=mtz)
    old = Batch.objects.create(status=Batch.Status.RECEIVED, received_date=dt.date(2025, 11, 4))  # из Excel, без инвойса
    ReceiptLine.objects.create(batch=old, product=d75, qty_expected=100, qty_received=100, price_cny=Decimal("6.67"))

    root = tmp_path / "Закупки"
    _save(root / "МТЗ/август25/инвойс.xlsx", _invoice("TS250869", [("D75L100-2", 100, 6.6747), ("D60L80-3", 5, 4.74)]))
    _save(root / "Белаз/июнь26/инвойс.xlsx", _invoice("TS2406188", [("D89L1000-2", 235, 95)]))
    # опечатки завода: та же позиция двумя строками под чужими кодами
    _save(root / "Белаз/июнь26/упаковочный.xlsx", _ru_pl("TS2406188", [("SI D89L1000-2", 165, 11), ("BSI D50L100-2", 70, 5)]))
    _save(root / "Белаз/на производстве /инвойс.xlsx", _invoice("JH-J-1", [("D51L1000-2", 120, 23.52)], unit=True))
    monkeypatch.setattr(cmd, "PURCHASES", [
        cmd.Purchase("TS250869", "МТЗ", ("МТЗ/август25/инвойс.xlsx",), existing="2025-11-04"),
        cmd.Purchase("TS2406188", "БелАЗ", ("Белаз/июнь26/инвойс.xlsx", "Белаз/июнь26/упаковочный.xlsx"), received="2026-07-15",
                     fixes={"SI D89L1000-2": "BSI D89L1000-2", "BSI D50L100-2": "BSI D89L1000-2"}, notes="Опечатки исправлены."),
        cmd.Purchase("JH-J-1", "БелАЗ", ("Белаз/на производстве/инвойс.xlsx",)),
    ])

    call_command("import_purchases", str(tmp_path))  # проверка — ничего не пишет
    assert Batch.objects.count() == 1 and not Product.objects.filter(code="BSI D51L1000-2").exists()

    call_command("import_purchases", str(tmp_path), "--apply")
    old.refresh_from_db()
    assert old.invoice == "TS250869"
    assert {(line.product.code, line.qty_expected, line.qty_received, line.price_cny) for line in old.lines.all()} == {
        ("BSI D75L100-2", 100, 100, Decimal("6.6747")),  # точная цена вместо округлённой
        ("BSI D60L80-3", 5, 5, Decimal("4.74")),  # недостающая строка
    }
    belaz_batch = Batch.objects.get(invoice="TS2406188")
    assert belaz_batch.is_received and belaz_batch.received_date == dt.date(2026, 7, 15) and "Опечатки" in belaz_batch.notes
    line = belaz_batch.lines.get()
    assert (line.product, line.qty_expected, line.qty_received) == (d89, 235, 235)
    assert sorted(belaz_batch.cartons.values_list("qty_per_carton", "cartons")) == [(14, 5), (15, 11)]
    assert not ProductAlias.objects.filter(text__contains="D50L100-2").exists()  # опечатку не запомнили
    new = Product.objects.get(code="BSI D51L1000-2")
    assert new.customer == belaz and new.status == Product.Status.DRAWING_PENDING and new.l1 == 1000
    order = Batch.objects.get(invoice="JH-J-1")
    assert order.status == Batch.Status.IN_TRANSIT and order.lines.get().qty_expected == 120

    call_command("import_purchases", str(tmp_path), "--apply")  # повторно — без дублей
    assert Batch.objects.count() == 3 and ReceiptLine.objects.count() == 4
