"""Приходы из Китая по документам папки «Закупки» на Яндекс.Диске (разобрано 27.10.2026).

    python manage.py fetch_yandex data/source                      # докачать папку
    python manage.py import_purchases data/source/Закупки          # проверка, ничего не меняет
    python manage.py import_purchases data/source/Закупки --apply  # записать

Что делает:
- поставки, которые уже перенесены из Excel МТЗ (партии без номера инвойса), дополняет:
  номер инвойса, точные цены завода, коробки из упаковочного листа, недостающие строки
  (образцы ГСМ/ММЗ/БелАЗ в TS2407202). Количества не меняет — расхождения только показывает;
- остальные создаёт: принятые — с датой приёмки, заказы в пути и на производстве — «в пути»;
- изделия, которых нет в каталоге, создаёт со статусом «чертёж ожидается».

Повторный запуск ничего не дублирует: партия с таким инвойсом уже есть — только доводится до описания
ниже (заказчик созданных изделий, номер партии, отгрузка), строки не добавляются.
Ошибки в документах завода исправлены явно (FIXES) и записаны в примечание партии.
"""

import datetime as dt
import unicodedata
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F

from catalog.codes import normalize_name, parse_parametric
from catalog.models import Customer, Product
from stock import receipt_import, services
from stock.importers import Invoice, InvoiceLine, ParseError, parse_document, read_rows
from stock.models import Adjustment, Batch, ReceiptLine, Shipment


@dataclass
class Purchase:
    invoice: str
    customer: str | None  # None — в поставке изделия разных заказчиков
    files: tuple  # инвойс, упаковочный лист — пути внутри «Закупок»
    received: str | None = None  # дата приёмки; None — ещё в пути / на производстве
    existing: str | None = None  # дата приёмки партии, перенесённой из Excel без номера инвойса
    lines: bool = True  # False — строки инвойса без кодов изделий, берём только номер
    only_invoice_items: bool = False  # упаковочный лист общий на две поставки
    pick: dict = field(default_factory=dict)  # код → заказчик для кодов, которые есть у нескольких
    fixes: dict = field(default_factory=dict)  # опечатка в документе → правильный код
    batch_info: dict = field(default_factory=dict)  # номер партии, дата изготовления, паспорт
    shipped: tuple | None = None  # (дата, примечание) — вся поставка отгружена заказчику
    items: tuple = ()  # строки инвойса (наименование, шт, цена CNY), если файлов нет на Яндекс.Диске
    notes: str = ""


MTZ = "МТЗ"
NEW_PRODUCT_NOTE = "Создано по документам завода (папка «Закупки»), чертежа в сервисе нет."
PURCHASES = [
    Purchase("TS231230", MTZ, ("МТЗ/Опытные образцы/русс инвойс с доставкой.xlsx",
                               "МТЗ/Опытные образцы/RUSупаковочный лист с печатью.xlsx"),
             existing="2024-03-04", lines=False),
    Purchase("TS2407202", None, ("МТЗ/Опытная партия/Всего_русс инвойс_ноябрь.xlsx",
                                 "МТЗ/Опытная партия/Packing List-23ggbykv-TS2407202 (2).xls"),
             existing="2025-02-08", pick={"90-D60L100/100-2": "Гомсельмаш"},
             batch_info={"number": "01/25", "manufactured": "11.2024"},
             notes="Добавлены образцы ГСМ, ММЗ, БелАЗ по инвойсу TS2407202."),
    Purchase("TS2506227", MTZ, ("МТЗ/Опытная партия -воздух + МАЗ-МАН/Invoice_TS2506227_русс.pdf",
                                "МТЗ/Опытная партия -воздух + МАЗ-МАН/TS2503154 & TS2506227_RUSупаковочный лист.xlsx"),
             existing="2025-09-18", only_invoice_items=True),
    Purchase("TS250869", MTZ, ("МТЗ/август25/No_4_Proforma_Invoice_Счет_за_силиконовую_трубку_2025_8_8.xlsx",
                               "МТЗ/август25/Packing List-23ggbykv-TS250869.xls"), existing="2025-11-04"),
    Purchase("TS250963", MTZ, ("МТЗ/сентябрь 25/TS250963_RUS_invoice.pdf",
                               "МТЗ/сентябрь 25/Updated Packing List-23ggbykv-TS250963.xls"), existing="2025-11-19"),
    Purchase("TS2503154", "МАЗ МАН", ("МТЗ/Опытная партия -воздух + МАЗ-МАН/TS2503154русс инвойс_опытн.pdf",
                                      "МТЗ/Опытная партия -воздух + МАЗ-МАН/Packing List-23ggbykv-TS2503148 (2).xls"),
             received="2025-09-18", batch_info={"number": "02/25", "manufactured": "06.2025", "passport_number": "09/002/25"},
             shipped=("2025-09-19", "Паспорт № 09/002/25 от 19.09.2025, получатель СП ЗАО «МАЗ-МАН». Номер ТТН в паспорте не указан."),
             notes="Опытная партия для МАЗ-МАН (инвойс по договору 09/185 вместе с МТЗ). В инвойсе опечатка в номере — TS2403154, "
                   "китайский упаковочный лист — под номером TS2503148. Пришла вместе с TS2506227 "
                   "(общий русский упаковочный лист от 30.07.2025)."),
    Purchase("TS2601093", MTZ, ("МТЗ/Опытные образцы март 26/Invoice_TS2601093.pdf",
                                "МТЗ/Опытные образцы март 26/TS2601093_RUSупаковочный лист.xlsx"),
             notes="Опытные образцы. Упаковочный лист от 24.09.2026 — похоже, едут вместе с TS2608242."),
    Purchase("TS260595", MTZ, ("МТЗ/май26/Proforma_Invoice_TS260595_рус.pdf",
                               "МТЗ/май26/Packing List-23ggbykv-TS260695 (3).xls"), received="2026-06-19",
             notes="Упаковочный лист завода — под номером TS260695. Дата приёмки — дата упаковочного листа "
                   "(19.06.2026), фактическую уточните."),
    Purchase("TS2608242", MTZ, ("МТЗ/август 26 в дороге(доставка)/No.11-Proforma Invoice-TS2608242-2026.8.31.pdf",
                                "МТЗ/август 26 в дороге(доставка)/Packing List-23ggbykv-TS2608242.xls"),
             notes="В дороге (упаковочный лист от 28.09.2026)."),
    # файлы присланы в переписке 27.10.2026, на Яндекс.Диске их нет
    Purchase("TS260887", MTZ, (), items=(("Патрубок силиконовый BSI D100L160-COR-3", 6, "39.40"),),
             notes="Инвойс TS260887 от 12.08.2026 к договору 09/44, упаковочный лист от 24.08.2026 (1 коробка, 3,2 кг). "
                   "Образцы для МТЗ (ЯПИБ 26.1561)."),
    Purchase("TS260294", "Амкадор", ("Амкадор -Пинск/этап 1/Proforma_Invoice_TS260294_рус_1этап.xlsx",
                                     "Амкадор -Пинск/этап 1/Packing List-23ggbykv-TS260294.xls"), received="2026-04-21",
             pick={"D70L100-2": MTZ},
             notes="Амкадор (Пинск), этап 1: 599 шт Амкадору; D70L100-2 (100 шт) в учёте Амкадора нет — это изделие МТЗ. "
                   "Дата приёмки — дата упаковочного листа (21.04.2026), фактическую уточните."),
    Purchase("TS2605143", "Амкадор", ("Амкадор -Пинск/2 этап/Order No.8-Proforma_Invoice_рус_2этап.pdf",
                                      "Амкадор -Пинск/2 этап/Packing List-23ggbykv-TS2605143 (3).xls"), received="2026-06-19",
             notes="Амкадор (Пинск), этап 2. Дата приёмки — дата упаковочного листа (19.06.2026), фактическую уточните."),
    Purchase("JH-J-20261009", "Амкадор", ("Амкадор/Заказ в производстве/Инвойс JINHU JH-J-20261009 v2 1 стр..pdf",),
             notes="Заказ на производстве, завод XINGTAI JINHU TECHNOLOGY."),
    Purchase("TS2406188", "БелАЗ", ("Белаз/июнь26/TS2406188_русс инвойс.pdf", "Белаз/июнь26/TS2406188_RUSупаковочный лист.pdf"),
             received="2026-07-15", fixes={"SI D89L1000-2": "BSI D89L1000-2", "BSI D50L100-2": "BSI D89L1000-2"},
             notes="В упаковочном листе завода D89L1000-2 записан двумя строками с опечатками: «SI D89L1000-2» (165 шт) "
                   "и «BSI D50L100-2» (70 шт) — по инвойсу это 235 шт D89L1000-2. Дата приёмки — дата упаковочного "
                   "листа (15.07.2026), фактическую уточните."),
    Purchase("JH-J-20260925", "БелАЗ", ("Белаз/сентябрь 2026 _ на производстве/Инвойс_JH-J-20260925_рус.xlsx",),
             notes="Заказ на производстве, завод XINGTAI JINHU TECHNOLOGY. В инвойсе количество в метрах — "
                   "переведено в штуки по длине изделия (L10000 — 10 м, L5000 — 5 м, L1000 — 1 м), цена — за штуку."),
]


@dataclass
class Samples:
    """Бесплатные образцы заказчику по письму (списание «бесплатные образцы заказчику»)."""
    customer: str
    date: str
    document: str
    qty: int  # штук каждого наименования
    codes: tuple  # коды как в каталоге (в письмах встречается «90-60» вместо «90/60» и т. п.)


SAMPLES = [
    Samples("ММЗ", "2025-03-12", "Письмо № 09/381 от 12.03.2025, ТТН № 2229415 от 12.03.2025", 3, (
        "BSI 90/60", "BSI 141-50/63,5", "BSI 63,5", "BSI 90-63,5", "BSI 90-63,5-01", "BSI 90-63,5-02", "BSI 110-50/63,5",
        "BSI 90-38", "BSI 90-38-01", "BSI 90-38-02", "BSI 90-38-03", "BSI 90-38-04", "BSI 80-38")),
    Samples("Гомсельмаш", "2025-04-04", "Письмо № 09/510 от 04.04.2025, ТТН № 2229545 от 04.04.2025", 2, (
        "BSI D32L70-3", "BSI D32L190-3", "BSI D32L200-3", "BSI D90L130-3", "BSI D42L120-2", "BSI D60L120-2", "BSI D65L150-2",
        "BSI D80L150-2", "BSI 90-D38L100/50-2", "BSI 90-D55L100/100-2", "BSI 90-D60L100/100-2", "BSI 90-D60L150/150-2",
        "BSI 90-D75L150/150-3", "BSI 90-D85L100/100-3", "BSI 90-D75/50L100/100-3", "BSI 45-D75L196/81-3",
        "BSI 45-D100L205/85-3", "BSI D76L160-COR-3", "BSI D89L160-COR-3", "BSI D100L160-COR-3", "BSI D100L210-COR-3",
        "BSI D114L200-COR-3")),
    # документов нет — переданы как образцы; дата условная (приход TS2407202)
    Samples("МТЗ", "2026-08-14", "Соглашение № 59 от 12.08.2026 о передаче образцов для испытаний, ТТН ЕМ № 3077456 "
            "от 14.08.2026", 4, ("BSI D25L140/67-1-M",)),
    Samples("БелАЗ", "2025-02-08", "Без документов: переданы БелАЗу как образцы (дата условная)", 1, (
        "BSI D60L1000-2", "BSI D89L1000-2", "BSI D102L1000-2")),
]


# Сведения об изделиях, созданных по документам завода, из документов заказчика (пустые поля заполняются)
PRODUCT_INFO = {
    (MTZ, "BSI D25L140/67-1-M"): {"yapib": "26.1564"},  # соглашение № 59 от 12.08.2026
    (MTZ, "BSI D100L160-COR-3"): {"yapib": "26.1561"},  # чертёж МТЗ; такой же код есть у Гомсельмаша
}


@dataclass
class Sale:
    """Отгрузка заказчику по документу (ТТН или паспорт). Договор и спецификация создаются, если указаны
    и цены в BYN; цены в другой валюте — только в примечании."""
    customer: str
    date: str
    document: str  # по нему отгрузка узнаётся при повторном запуске
    lines: tuple  # (код, шт, цена без НДС BYN или None, партия — инвойс, номер партии или None)
    ttn: str = ""
    contract: tuple | None = None  # (номер, дата)
    spec: tuple | None = None  # (номер, дата)
    products_of: str | None = None  # чьи изделия (торговым домам МТЗ — изделия МТЗ)
    rub_rate: str | None = None  # цены в строках — руб. РФ; курс НБ РБ, BYN за 100 RUB на дату отгрузки
    notes: str = ""


STAVROPOL = "ТД МТЗ-Ставрополь"
STAVROPOL_NOTES = "Договор № 07/203 от 09.12.2024, цены в руб. РФ — в сервис не перенесены."
STAVROPOL_RUB = "Договор № 07/203 от 09.12.2024."
RATE_20260325 = "3.6507"  # НБ РБ, BYN за 100 RUB на 25.03.2026
SALES = [
    Sale("БелАЗ", "2026-09-07", "ТТН ЕМ № 3077598 от 07.09.2026", ttn="3077598",
         contract=("450-01509", "2026-04-25"), spec=("10", "2026-07-06"), lines=(
             ("BSI D51L1000-2", 45, "38.00", "TS2406188"), ("BSI D89L1000-2", 235, "66.00", "TS2406188"),
             ("BSI D127L1000-2", 21, "90.00", "TS2406188"))),
    # паспорта отгрузок, номер ТТН в них не заполнен
    Sale(STAVROPOL, "2025-12-09", "Паспорт № 09/002/25 от 09.12.2025, спецификация № 17", products_of=MTZ,
         notes=STAVROPOL_NOTES, lines=(
             ("BSI D50L100-3", 10, None, "02/25"), ("BSI D60L80-3", 10, None, "02/25"), ("BSI D60L80-2", 10, None, "02/25"),
             ("BSI D63L80-2", 20, None, "02/25"), ("BSI D75L100-3", 10, None, "02/25"), ("BSI D75L100-2", 10, None, "02/25"))),
    Sale(STAVROPOL, "2025-12-09", "Паспорт № 09/002/25 от 09.12.2025, спецификация № 20", products_of=MTZ,
         notes=STAVROPOL_NOTES, lines=(
             ("BSI D50L100-3", 2, None, "02/25"), ("BSI D60L80-3", 1, None, "02/25"), ("BSI D90L120-3", 1, None, "01/25"))),
    Sale(STAVROPOL, "2025-12-09", "Паспорт № 09/002/25 от 09.12.2025, спецификация № 21", products_of=MTZ,
         notes=STAVROPOL_NOTES, lines=(("BSI D50L100-3", 10, None, "02/25"), ("BSI D75L100-3", 10, None, "02/25"))),
    # спецификации № 28, 29, 30 от 05.03.2026; по № 28 в паспорте D63L80-2 — 20 шт, в спецификации — 10
    Sale(STAVROPOL, "2026-03-25", "Паспорт № 09/002/25 от 25.03.2026, спецификация № 28", products_of=MTZ,
         rub_rate=RATE_20260325, notes=STAVROPOL_RUB + " D63L80-2 — 10 шт по спецификации и бухгалтерии (в паспорте 20).", lines=(
             ("BSI D50L100-3", 10, "202.14", "02/25"), ("BSI D60L80-2", 10, "179.47", "02/25"),
             ("BSI D63L80-2", 10, "179.47", "02/25"), ("BSI D75L100-3", 5, "279.14", "02/25"),
             ("BSI D75L100-2", 5, "253.06", "02/25"), ("BSI D90L120-3", 10, "402.72", "02/25"),
             ("BSI D90L120-2", 10, "382.60", "02/25"))),
    Sale(STAVROPOL, "2026-03-25", "Паспорт № 09/002/25 от 25.03.2026, спецификация № 29", products_of=MTZ,
         rub_rate=RATE_20260325, notes=STAVROPOL_RUB, lines=(
             ("BSI D60L80-3", 10, "191.27", "02/25"), ("BSI D60L80-2", 10, "179.47", "02/25"))),
    Sale(STAVROPOL, "2026-03-25", "Паспорт № 09/002/25 от 25.03.2026, спецификация № 30", products_of=MTZ,
         rub_rate=RATE_20260325, notes=STAVROPOL_RUB, lines=(
             ("BSI D60L80-3", 1, "191.27", "02/25"), ("BSI D60L80-2", 1, "179.47", "02/25"))),
    # Казахстан: в 1С отгрузки в 2026 нет, остатки на 01.01.2026 меньше ровно на спецификацию — отгружено в 2025.
    # Точной даты нет — дата счёт-фактуры 09/267/1; курс НБ РБ на 05.12.2025.
    Sale("ТД Белорусский Трактор (Казахстан)", "2025-12-05", "Счёт-фактура № 09/267/1 от 05.12.2025, спецификация № 1",
         products_of=MTZ, rub_rate="3.7627",
         notes="Договор № 09/267 от 05.12.2025, ТОО «Торговый Дом Белорусский Трактор», г. Астана. Дата отгрузки "
               "условная (по счёт-фактуре)", lines=(
             ("BSI D50L100-3", 5, "203", None), ("BSI D63L80-2", 5, "180", None), ("BSI D63L80-3", 4, "192", None),
             ("BSI D75L100-2", 5, "253", None), ("BSI D75L100-3", 5, "280", None),
             ("BSI 90-D53L210/140-1", 5, "835", None), ("BSI 90-D38L150/140-1", 5, "543", None),
             ("BSI 30-D38L95/55-1", 5, "383", None), ("BSI D54L80-1", 5, "177", None),
             ("BSI 90-D38L170/150-1", 5, "630", None), ("BSI 30-D38L120/55-1", 5, "460", None))),
]
# Отгрузки, внесённые прежней версией по ошибке: файл паспорта «18» перезаписан паспортом спецификации № 29
OBSOLETE_SALES = [(STAVROPOL, "Паспорт № 09/002/25 от 25.03.2026 (номер спецификации не указан")]


def _path_key(text):
    """Имена с Яндекс.Диска: буква «й» бывает разложена (NFD), Windows обрезает пробелы в конце папок."""
    parts = unicodedata.normalize("NFC", str(text)).replace("\\", "/").split("/")
    return "/".join(p.strip() for p in parts).lower()


class Rollback(Exception):
    pass


class Command(BaseCommand):
    help = "Загрузить приходы из Китая по документам папки «Закупки»"

    def add_arguments(self, parser):
        parser.add_argument("folder", help="Папка «Закупки» (или папка, в которой она лежит)")
        parser.add_argument("--apply", action="store_true", help="Записать в базу (без него — только проверка)")

    def handle(self, folder, apply, **opts):
        root = Path(folder)
        if (root / "Закупки").is_dir():
            root = root / "Закупки"
        if not root.is_dir():
            raise CommandError(f"Нет папки {root}")
        self.files = {_path_key(p.relative_to(root).as_posix()): p for p in root.rglob("*") if p.is_file()}
        self.errors = []
        try:
            with transaction.atomic():
                for purchase in PURCHASES:
                    self.stdout.write(f"\n{purchase.invoice}")
                    try:
                        with transaction.atomic():
                            self.load(purchase)
                    except (ValueError, ParseError, OSError) as e:
                        self.errors.append(f"{purchase.invoice}: {e}")
                        self.stdout.write(self.style.ERROR(f"  ошибка: {e}"))
                self.product_info()
                for samples in SAMPLES:
                    self.stdout.write(f"\nОбразцы {samples.customer}")
                    try:
                        with transaction.atomic():
                            self.give_samples(samples)
                    except (ValueError, services.InsufficientStock) as e:
                        self.errors.append(f"образцы {samples.customer}: {e}")
                        self.stdout.write(self.style.ERROR(f"  ошибка: {e}"))
                for customer_name, prefix in OBSOLETE_SALES:
                    for shipment in Shipment.objects.filter(customer__name=customer_name, notes__startswith=prefix):
                        self.stdout.write(f"\nУдалена ошибочная отгрузка: {shipment}")
                        shipment.delete()
                for sale in SALES:
                    self.stdout.write(f"\nОтгрузка {sale.customer}: {sale.document}")
                    try:
                        with transaction.atomic():
                            self.sell(sale)
                    except (ValueError, services.InsufficientStock) as e:
                        self.errors.append(f"{sale.customer}, {sale.document}: {e}")
                        self.stdout.write(self.style.ERROR(f"  ошибка: {e}"))
                if not apply:
                    raise Rollback
        except Rollback:
            self.stdout.write(self.style.WARNING("\nПроверка: в базу ничего не записано. Для записи добавьте --apply."))
        else:
            self.stdout.write(self.style.SUCCESS("\nЗаписано."))
        if self.errors:
            raise CommandError(f"Не загружено {len(self.errors)}: " + "; ".join(self.errors))

    # --- одна поставка ---

    def load(self, purchase):
        docs = [self.parse(name) for name in purchase.files]
        if purchase.items:
            docs.append(("invoice", Invoice(number=purchase.invoice, lines=[
                InvoiceLine(name=name, qty=qty, price=Decimal(price), amount=qty * Decimal(price)) for name, qty, price in purchase.items])))
        invoice = next(d for kind, d in docs if kind == "invoice")
        packing = next((d for kind, d in docs if kind == "packing"), None)
        customer = Customer.objects.get(name=purchase.customer) if purchase.customer else None
        existing = self.existing(purchase)
        if existing is None and (done := Batch.objects.filter(invoice=purchase.invoice).first()):
            self.update_loaded(purchase, done, customer)
            return
        if purchase.existing and existing is None:
            raise ValueError(f"не нашёл партию от {purchase.existing} — сначала перенесите Excel МТЗ")

        rows = receipt_import.rows_from(receipt_import.build_draft(invoice, packing))
        if purchase.lines:
            rows = self.resolve(purchase, rows, customer, existing)
            products = Product.objects.in_bulk([r.product_id for r in rows])
            for product_id, m in receipt_import.merge_rows(rows, {}).items():
                if m["inv"] is not None and m["pl"] is not None and m["inv"] != m["pl"]:
                    self.stdout.write(f"  · {products[product_id].code}: в инвойсе {m['inv']} шт, в упаковочном листе {m['pl']} шт")
        if existing is not None:
            self.enrich(purchase, existing, rows)
        else:
            self.create(purchase, rows)

    def parse(self, name):
        path = self.files.get(_path_key(name))
        if path is None:
            raise ValueError(f"нет файла «{name}» — докачайте папку командой fetch_yandex")
        with open(path, "rb") as fh:
            return parse_document(read_rows(fh, path.name))

    def existing(self, purchase):
        if not purchase.existing:
            return None
        same_day = Batch.objects.filter(received_date=purchase.existing)
        batch = (same_day.filter(invoice=purchase.invoice).first() or same_day.filter(invoice="").first()
                 or Batch.objects.filter(invoice=purchase.invoice).first())
        for twin in Batch.objects.filter(invoice=purchase.invoice).exclude(pk=getattr(batch, "pk", None)):
            self.stdout.write(self.style.WARNING(f"  · есть ещё {twin} ({twin.get_status_display()}) — похоже, загружена "
                                                 "повторно вручную; проверьте и удалите лишнюю"))
        return batch

    def resolve(self, purchase, rows, customer, existing=None):
        """Изделие каждой строки: по коду с учётом заказчика поставки (в смешанной поставке — то, что уже
        есть в партии); нет в каталоге — новое."""
        fixes = {normalize_name(k): v for k, v in purchase.fixes.items()}
        pick = {normalize_name(k): Customer.objects.get(name=v) for k, v in purchase.pick.items()}
        result = []
        for row in rows:
            name = row.invoice_name or row.pl_name
            key = normalize_name(name)
            for attr in ("invoice_name", "pl_name"):
                if normalize_name(getattr(row, attr)) in fixes:
                    key = normalize_name(fixes[normalize_name(getattr(row, attr))])
                    setattr(row, attr, "")  # опечатку не запоминаем как синоним
                    row.pl_mark = "" if attr == "pl_name" else row.pl_mark
                    row.product_id = None
            owner = pick.get(key, customer)
            product = Product.objects.filter(pk=row.product_id).first()
            if product is None or (owner and product.customer_id != owner.pk):
                product = Product.objects.filter(code_key=key, customer=owner).first() if owner else None
            if product is None and owner is None and existing is not None:
                product = Product.objects.filter(code_key=key, receipt_lines__batch=existing).first()
            if product is None:
                if owner is None:
                    raise ValueError(f"«{name}»: не знаю заказчика")
                product = self.new_product(key, owner)
            row.product_id = product.pk
            result.append(row)
        if purchase.only_invoice_items:
            in_invoice = {r.product_id for r in result if r.qty_invoice is not None}
            result = [r for r in result if r.product_id in in_invoice]
        return result

    def new_product(self, key, customer):
        product = Product(code=f"BSI {key}", customer=customer, status=Product.Status.DRAWING_PENDING, notes=NEW_PRODUCT_NOTE)
        if parsed := parse_parametric(key):
            for attr in ("kind", "angle", "d1", "d2", "l1", "l2", "color"):
                setattr(product, attr, getattr(parsed, attr))
        product.save()
        self.stdout.write(f"  + новое изделие {product.code} ({customer})")
        return product

    def create(self, purchase, rows):
        header = {"invoice": purchase.invoice, **purchase.batch_info, "notes": "\n".join(
            n for n in (purchase.notes, "Загружено из папки «Закупки»: " + ", ".join(Path(f).name for f in purchase.files)) if n)}
        draft = {"rows": [asdict(r) for r in rows]}
        if not purchase.lines:
            raise ValueError("в инвойсе нет кодов изделий — новую партию по нему не создать")
        batch = receipt_import.create_batch(draft, {}, header)
        if purchase.received:
            batch.status = Batch.Status.RECEIVED
            batch.received_date = dt.date.fromisoformat(purchase.received)
            batch.save()
            batch.lines.update(qty_received=F("qty_expected"))
        lines = list(batch.lines.all())
        state = f"принята {batch.received_date:%d.%m.%Y}" if batch.is_received else "в пути"
        amount = sum((line.qty_expected * line.price_cny for line in lines if line.price_cny), Decimal(0))
        self.stdout.write(f"  новая партия, {state}: {len(lines)} поз., {sum(line.qty_expected for line in lines)} шт, "
                          f"{amount:.2f} CNY")
        if purchase.shipped:
            self.ship(purchase, batch, Customer.objects.get(name=purchase.customer))

    def ship(self, purchase, batch, customer):
        date, notes = purchase.shipped
        shipment = Shipment.objects.create(customer=customer, date=dt.date.fromisoformat(date), notes=notes)
        services.post_shipment(shipment, [{"product": line.product, "qty": line.qty, "batch": batch}
                                          for line in batch.lines.select_related("product")])
        self.stdout.write(f"  отгружена {customer} {shipment.date:%d.%m.%Y}")

    def sell(self, sale):
        from sales.models import Contract, SpecLine, Specification

        customer, _ = Customer.objects.get_or_create(name=sale.customer)
        done = Shipment.objects.filter(customer=customer)
        done = done.filter(ttn_number=sale.ttn) if sale.ttn else done.filter(notes__startswith=sale.document)
        if existing := done.first():
            self.fill_prices(sale, existing)
            return
        spec = None
        if sale.contract:
            contract, _ = Contract.objects.get_or_create(customer=customer, number=sale.contract[0],
                                                         defaults={"date": dt.date.fromisoformat(sale.contract[1])})
            spec, _ = Specification.objects.get_or_create(contract=contract, number=sale.spec[0],
                                                          defaults={"date": dt.date.fromisoformat(sale.spec[1])})
        notes = ". ".join(n for n in (sale.document, f"Договор № {contract.number}, спецификация № {spec.number}" if spec else "",
                                      sale.notes, self.rate_note(sale)) if n).replace("..", ".")
        shipment = Shipment.objects.create(customer=customer, date=dt.date.fromisoformat(sale.date), ttn_number=sale.ttn, notes=notes)
        owner = Customer.objects.get(name=sale.products_of) if sale.products_of else customer
        rows = []
        for code, qty, price, batch_ref in sale.lines:
            product = Product.objects.filter(code=code, customer=owner).first()
            if product is None:
                raise ValueError(f"нет изделия {code}")
            batch = None  # без партии — из самой старой с остатком
            if batch_ref:
                batch = (Batch.objects.filter(invoice=batch_ref, lines__product=product).first()
                         or Batch.objects.filter(number=batch_ref, lines__product=product).first())
                if batch is None:
                    raise ValueError(f"{code}: нет партии {batch_ref}")
            price = self.byn(sale, price)
            spec_line = SpecLine.objects.get_or_create(spec=spec, product=product, defaults={"price_byn": price})[0] if spec else None
            rows.append({"product": product, "qty": qty, "price_byn": price, "batch": batch, "spec_line": spec_line})
        services.post_shipment(shipment, rows)
        amount = sum(r["qty"] * r["price_byn"] for r in rows if r["price_byn"])
        self.stdout.write(f"  отгружено {sum(r['qty'] for r in rows)} шт" + (f" на {amount} руб. без НДС" if amount else "")
                          + f" ({shipment.date:%d.%m.%Y})")

    @staticmethod
    def byn(sale, price):
        if not price:
            return None
        if sale.rub_rate:  # руб. РФ × курс за 100 RUB
            return (Decimal(price) * Decimal(sale.rub_rate) / 100).quantize(Decimal("0.01"))
        return Decimal(price)

    @staticmethod
    def rate_note(sale):
        if not sale.rub_rate:
            return ""
        return f"Цены в руб. РФ пересчитаны в BYN по курсу НБ РБ {sale.rub_rate.replace('.', ',')} BYN за 100 RUB на дату отгрузки"

    def fill_prices(self, sale, shipment):
        """Отгрузка уже загружена (прежде без цены) — проставить цены, если теперь они известны."""
        prices = {code: self.byn(sale, price) for code, _, price, _ in sale.lines if price}
        filled = 0
        for line in shipment.lines.filter(price_byn__isnull=True).select_related("product"):
            if line.product.code in prices:
                line.price_byn = prices[line.product.code]
                line.save(update_fields=["price_byn"])
                filled += 1
        if filled:
            shipment.notes = ". ".join(n for n in (sale.document, sale.notes, self.rate_note(sale)) if n).replace("..", ".").replace("..", ".")
            shipment.save(update_fields=["notes"])
        self.stdout.write(f"  уже есть — проставлены цены в {filled} строках" if filled else "  уже есть — пропускаю")

    def product_info(self):
        for (customer, code), info in PRODUCT_INFO.items():
            product = Product.objects.filter(customer__name=customer, code=code).first()
            if product is None:
                continue
            changed = {k: v for k, v in info.items() if not getattr(product, k)
                       and not (k == "yapib" and Product.objects.filter(yapib=v).exists())}
            if changed:
                for k, v in changed.items():
                    setattr(product, k, v)
                product.save()
                self.stdout.write(f"\n{product.code}: " + ", ".join(f"{k} = {v}" for k, v in changed.items()))

    def give_samples(self, samples):
        customer = Customer.objects.get(name=samples.customer)
        kind = Adjustment.Kind.FREE_SAMPLE
        if Adjustment.objects.filter(kind=kind, customer=customer, notes__startswith=samples.document).exists():
            self.stdout.write("  уже есть — пропускаю")
            return
        lines = []
        for code in samples.codes:
            product = Product.objects.filter(code=code, customer=customer).first()
            if product is None:
                raise ValueError(f"нет изделия {code}")
            lines.append({"product": product, "qty": samples.qty})
        adjustment = Adjustment.objects.create(kind=kind, date=dt.date.fromisoformat(samples.date), customer=customer,
                                               notes=f"{samples.document}. Опытные образцы на безвозмездной основе.")
        services.post_adjustment(adjustment, lines)
        self.stdout.write(f"  списано {len(lines)} наим. по {samples.qty} шт ({adjustment.date:%d.%m.%Y})")

    def update_loaded(self, purchase, batch, customer):
        """Поставка уже загружена прежней версией команды — доводим до текущего описания."""
        changes = []
        pick = {normalize_name(k): Customer.objects.get(name=v) for k, v in purchase.pick.items()}
        # изделия, созданные командой под другим заказчиком, — к правильному
        for product in Product.objects.filter(receipt_lines__batch=batch, status=Product.Status.DRAWING_PENDING,
                                              notes=NEW_PRODUCT_NOTE):
            owner = pick.get(product.code_key, customer)
            if owner is None or product.customer_id == owner.pk or product.receipt_lines.exclude(batch=batch).exists():
                continue
            twin = Product.objects.filter(code_key=product.code_key, customer=owner).exclude(pk=product.pk).first()
            if twin and not (product.shipment_lines.exists() or product.spec_lines.exists()):
                # у заказчика такое изделие уже есть — созданный дубль убираем
                product.receipt_lines.update(product=twin)
                product.carton_specs.update(product=twin)
                product.aliases.all().delete()
                product.delete()
            else:
                product.customer = owner
                product.save()
            changes.append(f"{product.code} → {owner}")
        for attr, value in purchase.batch_info.items():
            if not getattr(batch, attr):
                setattr(batch, attr, value)
                changes.append(batch._meta.get_field(attr).verbose_name.lower())
        if purchase.notes and purchase.notes not in batch.notes:
            batch.notes = "\n".join(n for n in (purchase.notes, batch.notes) if n)
        batch.save()
        if purchase.shipped and not batch.shipment_lines.exists():
            self.ship(purchase, batch, customer)
            changes.append("отгрузка")
        self.stdout.write("  уже есть" + (": " + "; ".join(changes) if changes else " — пропускаю"))

    def enrich(self, purchase, batch, rows):
        changes = []
        if not batch.invoice:
            batch.invoice = purchase.invoice
            changes.append("номер инвойса")
        for attr, value in purchase.batch_info.items():
            if not getattr(batch, attr):
                setattr(batch, attr, value)
                changes.append(batch._meta.get_field(attr).verbose_name.lower())
        if purchase.notes and purchase.notes not in batch.notes:
            batch.notes = "\n".join(n for n in (batch.notes, purchase.notes) if n)
        batch.save()
        if purchase.lines:
            have = {line.product_id: line for line in batch.lines.all()}
            products = Product.objects.in_bulk([r.product_id for r in rows])
            for product_id, m in receipt_import.merge_rows(rows, {}).items():
                product, qty = products[product_id], m["inv"] if m["inv"] is not None else m["pl"]
                price = Decimal(m["price"]) if m["price"] else None
                line = have.get(product_id)
                if line is None:
                    ReceiptLine.objects.create(batch=batch, product=product, qty_expected=qty, price_cny=price,
                                               qty_received=qty if batch.is_received else None)
                    changes.append(f"+ {product.code} ({product.customer}) {qty} шт")
                else:
                    if line.qty_expected != qty:
                        self.stdout.write(f"  · {product.code}: в партии {line.qty_expected} шт, в документах {qty} шт — не меняю")
                    if price and line.price_cny != price and (line.price_cny is None or abs(line.price_cny - price) < Decimal("0.01")):
                        line.price_cny = price  # в Excel цена была округлена до копеек
                        line.save(update_fields=["price_cny"])
                        changes.append(f"точная цена {product.code}")
                before = batch.cartons.filter(product=product).count()
                receipt_import.save_cartons(batch, product, m["cartons"], replace=False)
                if batch.cartons.filter(product=product).count() > before:
                    changes.append(f"коробки {product.code}")
                for name, source in m["names"]:
                    receipt_import.remember_alias(product, name, source, None)
        self.stdout.write(f"  дополнена партия {batch}: " + ("; ".join(changes) if changes else "без изменений"))
