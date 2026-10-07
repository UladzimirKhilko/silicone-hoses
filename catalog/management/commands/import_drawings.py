"""Прикрепляет PDF чертежей к изделиям по путям из data/catalog.csv.

    python manage.py import_drawings /путь/к/папке/Патрубки

Папка — копия папки «Патрубки» с Яндекс.Диска. Уже прикреплённый файл заменяется.
"""

import csv
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError

from catalog.models import DrawingFile, Product


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
                for kind, column in ((DrawingFile.Kind.PDF, "drawing_pdf"), (DrawingFile.Kind.SIGNED, "drawing_signed")):
                    if not row[column]:
                        continue
                    path = root / row[column]
                    if not path.exists():
                        missing.append(row[column])
                        continue
                    attach(Product.objects.get(code=row["code"]), kind, path)
                    attached += 1
        self.stdout.write(self.style.SUCCESS(f"Прикреплено файлов: {attached}"))
        for m in missing:
            self.stdout.write(self.style.WARNING(f"Не найден: {m}"))


def attach(product, kind, path):
    drawing = DrawingFile.objects.filter(product=product, kind=kind).first() or DrawingFile(product=product, kind=kind)
    if drawing.file:
        drawing.file.delete(save=False)
    with open(path, "rb") as fh:
        drawing.file.save(path.name, File(fh), save=False)
    drawing.original_name = path.name
    drawing.build_preview()
    drawing.save()
    return drawing
