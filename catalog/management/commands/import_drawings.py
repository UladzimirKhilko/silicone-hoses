"""Прикрепляет файлы чертежей к изделиям по путям из data/catalog.csv.

    python manage.py import_drawings /путь/к/папке/Патрубки

Папка — копия папки «Патрубки» с Яндекс.Диска. Путь вида `файл.pdf#page=3` берёт одну страницу
из многостраничного PDF (так прислан пакет чертежей БелАЗа). Изменённый файл заменяется, тот же —
пропускается, поэтому команду можно запускать при каждом обновлении.
"""

import csv
import re
from pathlib import Path

import pymupdf
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from catalog.models import DrawingFile, Product

COLUMNS = {
    "drawing_pdf": DrawingFile.Kind.PDF,
    "drawing_signed": DrawingFile.Kind.SIGNED,
    "drawing_china": DrawingFile.Kind.CHINA,
    "drawing_customer": DrawingFile.Kind.CUSTOMER,
}


class Command(BaseCommand):
    help = "Прикрепить чертежи из папки «Патрубки»"

    def add_arguments(self, parser):
        parser.add_argument("root")
        parser.add_argument("--catalog", default=str(Path(settings.BASE_DIR) / "data" / "catalog.csv"))

    def handle(self, root, catalog, **opts):
        root = Path(root)
        if not root.is_dir():
            raise CommandError(f"Нет папки {root}")
        attached, missing = 0, []
        with open(catalog, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                lookup = {"yapib": row["yapib"]} if row["yapib"] else {"yapib": None, "code": row["code"]}
                product = Product.objects.filter(**lookup).first()
                if product is None:
                    missing.append(f"изделие {row['code']} ({row['yapib'] or 'без ЯПИБ'})")
                    continue
                for column, kind in COLUMNS.items():
                    ref = row.get(column)
                    if not ref:
                        continue
                    content = read_ref(root, ref)
                    if content is None:
                        missing.append(ref)
                        continue
                    name, data = content
                    if attach(product, kind, name, data, page="#page=" in ref):
                        attached += 1
                        if kind == DrawingFile.Kind.PDF and product.status == Product.Status.DRAWING_PENDING:
                            product.status = Product.Status.SAMPLE  # чертёж пришёл
                            product.save()
        self.stdout.write(self.style.SUCCESS(f"Прикреплено файлов: {attached}"))
        for m in missing:
            self.stdout.write(self.style.WARNING(f"Не найден: {m}"))


def read_ref(root, ref):
    """`путь` или `путь#page=N` → (имя файла, байты); None, если файла нет.
    Путь со звёздочкой (`**/*26.1561*.pdf`) — первый подходящий файл в любой папке: так ждём чертёж,
    которого ещё нет на Яндекс.Диске, не зная, как его назовут."""
    m = re.fullmatch(r"(.+?)(?:#page=(\d+))?", ref)
    if "*" in m.group(1):
        found = sorted(p for p in root.glob(m.group(1)) if p.is_file())
        if not found:
            return None
        path = found[0]
    else:
        path = root / m.group(1)
    if not path.exists():
        return None
    if not m.group(2):
        return path.name, path.read_bytes()
    page = int(m.group(2))
    with pymupdf.open(path) as src, pymupdf.open() as out:
        out.insert_pdf(src, from_page=page - 1, to_page=page - 1)
        return f"{path.stem} стр.{page}.pdf", out.tobytes()


def same_file(drawing, data):
    try:
        with drawing.file.open("rb") as fh:
            return fh.read() == data
    except (FileNotFoundError, ValueError):
        return False


def attach(product, kind, name, data, page=False):
    """Прикрепить файл; тот же самый файл повторно не записывается. Возвращает чертёж или None.
    page — страница, вырезанная из PDF: её байты каждый раз другие, сравниваем по имени."""
    drawing = DrawingFile.objects.filter(product=product, kind=kind).first() or DrawingFile(product=product, kind=kind)
    if drawing.file and (drawing.original_name == name and drawing.file.storage.exists(drawing.file.name)
                         if page else same_file(drawing, data)):
        return None
    if drawing.file:
        drawing.file.delete(save=False)
    drawing.file.save(name, ContentFile(data), save=False)
    drawing.original_name = name
    drawing.build_preview()
    drawing.save()
    return drawing
