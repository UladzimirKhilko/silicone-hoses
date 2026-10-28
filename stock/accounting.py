"""Отчёт бухгалтерии (1С) «Отчет о движении матценностей» по складу: остатки, приходы, расходы.

Строки отчёта:
- изделие (в колонке «Ед. изм» — «шт»): начальный остаток, приход, расход, конечный остаток (кол-во и сумма);
- под ним документы: «Приходная 09.06.26 TS260294 Qinghe…» — приход по инвойсу (кол-во и сумма завода);
  «Приходная 09.06.26 TS260294Там 2010-таможенная пошлина» (Офо — сбор за оформление, Тра — доставка) —
  затраты на эту же поставку, только сумма; «Расходная 10.06.26 3077121 Амкодор-Пинск» — отгрузка по ТТН.
Суммы — в BYN по учётной (себестоимостной) оценке, цен продажи в отчёте нет.
"""

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

from .importers import ParseError, _num, _text

DOC_RE = re.compile(r"^(Приходная|Расходная)\s+(\d\d\.\d\d\.\d\d)\s+([A-Za-z]+(?:-[A-Za-z]+)*-?\d+|\d+)(.*)$")
PERIOD_RE = re.compile(r"с\s*(\d\d\.\d\d\.\d\d)\s*по\s*(\d\d\.\d\d\.\d\d)")


def _date(text):
    return dt.datetime.strptime(text, "%d.%m.%y").date()


@dataclass
class Doc:
    kind: str  # "in" | "out"
    date: dt.date
    number: str  # инвойс прихода или № ТТН расхода
    party: str = ""  # поставщик / получатель
    cost: str = ""  # для строк затрат прихода: «Там 2010-таможенная пошлина» и т. п.
    qty: int = 0
    amount: Decimal = Decimal(0)


@dataclass
class Item:
    name: str
    opening_qty: int = 0
    opening_sum: Decimal = Decimal(0)
    closing_qty: int = 0
    closing_sum: Decimal = Decimal(0)
    docs: list = field(default_factory=list)

    def receipts(self):
        """{инвойс: (дата, шт, сумма вместе с затратами)}."""
        result = {}
        for d in self.docs:
            if d.kind == "in":
                date, qty, amount = result.get(d.number, (d.date, 0, Decimal(0)))
                result[d.number] = (date, qty + d.qty, amount + d.amount)
        return result

    def issues(self):
        return [d for d in self.docs if d.kind == "out"]


@dataclass
class Ledger:
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    items: list = field(default_factory=list)


def parse_ledger(rows):
    """rows — строки листа (read_rows). Колонки: 1 — наименование, 4 — ед. изм., 5/6 — нач. остаток
    (шт/сумма), 7/8 — приход, 9/10 — расход, 12/13 — конечный остаток."""
    ledger = Ledger()
    head = " ".join(_text(c) for row in rows[:6] for c in row)
    if "движении матценностей" not in head.replace("  ", " "):
        raise ParseError("Это не «Отчет о движении матценностей» из 1С.")
    if m := PERIOD_RE.search(head):
        ledger.date_from, ledger.date_to = _date(m.group(1)), _date(m.group(2))

    def cell(row, i):
        return row[i] if i < len(row) else None

    def qty(row, i):
        v = _num(cell(row, i))
        return int(v) if v is not None else 0

    def amount(row, i):
        v = _num(cell(row, i))
        return v if v is not None else Decimal(0)

    item = None
    for row in rows:
        name = _text(cell(row, 1))
        if not name:
            continue
        if _text(cell(row, 4)).lower() == "шт":
            item = Item(name=name, opening_qty=qty(row, 5), opening_sum=amount(row, 6),
                        closing_qty=qty(row, 12), closing_sum=amount(row, 13))
            ledger.items.append(item)
            continue
        m = DOC_RE.match(name)
        if not m or item is None:
            continue
        kind = "in" if m.group(1) == "Приходная" else "out"
        rest = m.group(4)
        doc = Doc(kind=kind, date=_date(m.group(2)), number=m.group(3))
        if rest.startswith(" ") or not rest:
            doc.party = rest.strip()
        else:
            doc.cost = rest.strip()  # «Там 2010-…», «Офо 1010-…», «Тра ИНТРАГЛОБ»
        if kind == "in":
            doc.qty, doc.amount = qty(row, 7), amount(row, 8)
        else:
            doc.qty, doc.amount = qty(row, 9), amount(row, 10)
        item.docs.append(doc)
    if not ledger.items:
        raise ParseError("В отчёте не нашлось ни одного изделия.")
    return ledger
