"""Прикрепляет файлы чертежей к изделиям по путям из data/catalog.csv.

    python manage.py import_drawings /путь/к/папке/Патрубки

Папка — копия папки «Патрубки» с Яндекс.Диска. Путь вида `файл.pdf#page=3` берёт одну страницу
из многостраничного PDF (так прислан пакет чертежей БелАЗа). Уже прикреплённый файл заменяется.
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
                product = Product.objects.get(**lookup)
                for column, kind in COLUMNS.items():
                    ref = row.get(column)
                    if not ref:
                        continue
                    content = read_ref(root, ref)
                    if content is None:
                        missing.append(ref)
                        continue
                    name, data = content
                    attach(product, kind, name, data)
                    attached += 1
        self.stdout.write(self.style.SUCCESS(f"Прикреплено файлов: {attached}"))
        for m in missing:
            self.stdout.write(self.style.WARNING(f"Не найден: {m}"))


def read_ref(root, ref):
    """`путь` или `путь#page=N` → (имя файла, байты); None, если файла нет."""
    m = re.fullmatch(r"(.+?)(?:#page=(\d+))?", ref)
    path = root / m.group(1)
    if not path.exists():
        return None
    if not m.group(2):
        return path.name, path.read_bytes()
    page = int(m.group(2))
    with pymupdf.open(path) as src, pymupdf.open() as out:
        out.insert_pdf(src, from_page=page - 1, to_page=page - 1)
        return f"{path.stem} стр.{page}.pdf", out.tobytes()


def attach(product, kind, name, data):
    drawing = DrawingFile.objects.filter(product=product, kind=kind).first() or DrawingFile(product=product, kind=kind)
    if drawing.file:
        drawing.file.delete(save=False)
    drawing.file.save(name, ContentFile(data), save=False)
    drawing.original_name = name
    drawing.build_preview()
    drawing.save()
    return drawing
