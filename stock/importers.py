"""Разбор файлов завода: инвойс (русский, xlsx) и упаковочный лист (xls/xlsx).

Форматы — по поставке TS2407202 (docs/08). Колонки ищутся по заголовкам, а не по номерам,
чтобы мелкие изменения шаблона завода не ломали загрузку.

Особенности упаковочного листа:
- одно изделие повторяется во многих строках (разные коробки) — суммируем;
- смешанные коробки (образцы): первая строка группы с маркировкой коробки, весом и общим
  количеством, следующие строки — только модель и количество в этой коробке;
- справа бывают служебные промежуточные итоги, внизу — строка «IN TOTAL» — пропускаем.
"""

import datetime as dt
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path


class ParseError(Exception):
    """Файл не похож на ожидаемый документ."""


# --- Чтение таблиц ---

def read_rows(file, name=""):
    """Первый лист .xlsx/.xls → список строк (списки значений)."""
    suffix = Path(name or getattr(file, "name", "")).suffix.lower()
    data = file.read() if hasattr(file, "read") else Path(file).read_bytes()
    if suffix == ".xls" or data[:4] == b"\xd0\xcf\x11\xe0":  # старый формат Excel
        import xlrd

        sheet = xlrd.open_workbook(file_contents=data).sheet_by_index(0)
        return [sheet.row_values(r) for r in range(sheet.nrows)]
    import io

    import openpyxl

    ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True).worksheets[0]
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _text(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v not in (None, "") else ""


def _num(v):
    if v in (None, ""):
        return None
    if isinstance(v, float):
        # Excel хранит числа как float: 2951.3999999999996 → 2951.4; цены завода неокруглённые
        # (12.6760942760943) — 10 знаков сохраняем.
        return Decimal(repr(round(v, 10))).normalize() if v != int(v) else Decimal(int(v))
    if isinstance(v, int):
        return Decimal(v)
    try:
        return Decimal(str(v).replace(",", ".").replace(" ", ""))
    except InvalidOperation:
        return None


def _find_header(rows, must, limit=30):
    for i, row in enumerate(rows[:limit]):
        cells = [_text(c).lower() for c in row]
        if all(any(word in c for c in cells) for word in must):
            return i, cells
    return None, None


def _col(cells, *words, exclude=()):
    for i, c in enumerate(cells):
        if any(w in c for w in words) and not any(x in c for x in exclude):
            return i
    return None


# --- Инвойс ---

@dataclass
class InvoiceLine:
    name: str
    qty: int
    price: Decimal | None
    amount: Decimal | None
    hs_code: str = ""


@dataclass
class Invoice:
    number: str = ""
    date: dt.date | None = None
    contract: str = ""
    currency: str = ""
    lines: list = field(default_factory=list)

    @property
    def total(self):
        return sum((line.amount or 0) for line in self.lines)


def _parse_date(text):
    for fmt in ("%Y/%m/%d", "%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def parse_invoice(rows):
    head, cells = _find_header(rows, ["наименование"])
    if head is None:
        raise ParseError("Не нашёл в файле таблицу с колонкой «Наименование» — это точно инвойс?")
    c_name = _col(cells, "наименование")
    c_qty = _col(cells, "к-во", "кол-во", "количество", "qty")
    c_price = _col(cells, "цена", "price")
    c_amount = _col(cells, "сумма", "amount")
    c_hs = _col(cells, "тнвэд", "тн вэд", "hs")
    inv = Invoice()
    header_text = " ".join(_text(c) for row in rows[:head] for c in row)
    if m := re.search(r"Инвойс\s*№\.?\s*([A-Za-z0-9\-/]+)", header_text, re.IGNORECASE):
        inv.number = m.group(1)
    if m := re.search(r"Дата:?\s*([\d./\-]{8,10})", header_text):
        inv.date = _parse_date(m.group(1))
    if m := re.search(r"к договору\s*([\w/\-]+\s*от\s*[\d.]+)", header_text, re.IGNORECASE):
        inv.contract = m.group(1)
    if m := re.search(r"\((CNY|USD|EUR)\)", " ".join(cells), re.IGNORECASE):
        inv.currency = m.group(1).upper()
    for row in rows[head + 1:]:
        name = _text(row[c_name]) if c_name is not None and c_name < len(row) else ""
        qty = _num(row[c_qty]) if c_qty is not None and c_qty < len(row) else None
        if not name or qty is None or name.lower().startswith("итого"):
            if inv.lines and name.lower().startswith("итого"):
                break
            continue
        inv.lines.append(InvoiceLine(
            name=name,
            qty=int(qty),
            price=_num(row[c_price]) if c_price is not None else None,
            amount=_num(row[c_amount]) if c_amount is not None else None,
            hs_code=_text(row[c_hs]) if c_hs is not None else "",
        ))
    if not inv.lines:
        raise ParseError("В инвойсе не нашлось строк с изделиями.")
    return inv


# --- Упаковочный лист завода ---

@dataclass
class PackingCarton:
    qty_per_carton: int
    cartons: int
    gross_kg: Decimal | None
    net_kg: Decimal | None


@dataclass
class PackingItem:
    name: str
    mark: str = ""  # маркировка на коробке (кит. описание)
    color: str = ""
    qty: int = 0
    cartons: list = field(default_factory=list)  # только коробки с одним изделием
    in_mixed: int = 0  # штук в смешанных коробках


@dataclass
class PackingList:
    title: str = ""
    invoice: str = ""
    items: dict = field(default_factory=dict)  # name -> PackingItem
    total_cartons: int | None = None
    total_qty: int | None = None
    total_gross: Decimal | None = None
    total_net: Decimal | None = None
    mixed_cartons: list = field(default_factory=list)  # [(маркировка, [(name, qty)])]


def parse_packing_list(rows):
    head, cells = _find_header(rows, ["model", "qty/ctn"])
    if head is None:
        raise ParseError("Не нашёл заголовков упаковочного листа завода (matched models, QTY/CTN).")
    c_mark = _col(cells, "item on the carton", "item")
    c_model = _col(cells, "model")
    c_color = _col(cells, "color")
    c_qty = _col(cells, "qty/ctn")
    c_ctn = _col(cells, "cartons")
    c_nw = _col(cells, "n.w/ctn")
    c_gw = _col(cells, "g.w/ctn")
    c_total = _col(cells, "total qty")
    c_tgw = _col(cells, "total g.w")
    c_tnw = _col(cells, "total n.w")

    pl = PackingList(title=" ".join(_text(c) for c in rows[0] if _text(c)) if rows else "")
    if m := re.search(r"/\s*([A-Z]{2}\d{5,})", pl.title):
        pl.invoice = m.group(1)

    def get(row, col):
        return row[col] if col is not None and col < len(row) else None

    carton_specs = defaultdict(lambda: defaultdict(Counter))  # name -> шт./коробку -> {(брутто, нетто): коробок}
    group = None
    for row in rows[head + 1:]:
        model = _text(get(row, c_model))
        if not model:
            total_ctn, total_qty = _num(get(row, c_ctn)), _num(get(row, c_total))
            if total_ctn and total_qty and pl.total_cartons is None:  # строка итогов
                pl.total_cartons, pl.total_qty = int(total_ctn), int(total_qty)
                pl.total_gross, pl.total_net = _num(get(row, c_tgw)), _num(get(row, c_tnw))
            group = None
            continue
        qty, cartons, total = _num(get(row, c_qty)), _num(get(row, c_ctn)), _num(get(row, c_total))
        item = pl.items.setdefault(model, PackingItem(name=model))
        if _text(get(row, c_color)):
            item.color = item.color or _text(get(row, c_color))
        if cartons is None and group is not None:  # продолжение смешанной коробки
            n = int(qty or 0)
            item.qty += n
            item.in_mixed += n
            group[1].append((model, n))
            continue
        if cartons and qty and total is not None and total != qty * cartons:  # начало смешанной коробки
            group = (_text(get(row, c_mark)), [(model, int(qty))])
            pl.mixed_cartons.append(group)
            item.qty += int(qty)
            item.in_mixed += int(qty)
            continue
        group = None
        if not (qty and cartons):
            continue
        item.mark = item.mark or _text(get(row, c_mark))
        item.qty += int(qty * cartons)
        carton_specs[model][int(qty)][(_num(get(row, c_gw)), _num(get(row, c_nw)))] += int(cartons)
    for name, sizes in carton_specs.items():
        result = []
        for q, weights in sorted(sizes.items()):
            # один размер коробки с разным весом: вес — по большинству коробок
            (gw, nw), _ = weights.most_common(1)[0]
            result.append(PackingCarton(q, sum(weights.values()), gw, nw))
        pl.items[name].cartons = result
    if not pl.items:
        raise ParseError("В упаковочном листе не нашлось строк с изделиями.")
    return pl
