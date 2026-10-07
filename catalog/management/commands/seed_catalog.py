"""Загружает справочник изделий и синонимы из data/catalog.csv и data/aliases.csv.

Повторный запуск обновляет изделия по коду и не создаёт дублей.
"""

import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from catalog.codes import parse_parametric
from catalog.models import Customer, Product, ProductAlias

CUSTOMERS = ["МТЗ", "Гомсельмаш", "ММЗ", "БелАЗ", "Амкадор", "МАЗ МАН"]


def _num(value):
    return Decimal(value) if value not in ("", None) else None


class Command(BaseCommand):
    help = "Загрузить справочник изделий из data/catalog.csv"

    def add_arguments(self, parser):
        parser.add_argument("--catalog", default=str(Path(settings.BASE_DIR) / "data" / "catalog.csv"))
        parser.add_argument("--aliases", default=str(Path(settings.BASE_DIR) / "data" / "aliases.csv"))

    @transaction.atomic
    def handle(self, *args, **opts):
        for name in CUSTOMERS:
            Customer.objects.get_or_create(name=name)

        created = updated = 0
        with open(opts["catalog"], newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                fields = {
                    "yapib": row["yapib"] or None,
                    "customer": Customer.objects.get_or_create(name=row["customer"])[0],
                    "status": row["status"],
                    "notes": row["notes"],
                }
                parsed = parse_parametric(row["code"])
                if parsed:
                    fields.update(
                        kind=parsed.kind, angle=parsed.angle, d1=parsed.d1, d2=parsed.d2,
                        l1=parsed.l1, l2=parsed.l2, color=parsed.color,
                    )
                for key in ("kind", "color"):
                    if row[key]:
                        fields[key] = row[key]
                for key in ("angle", "d1", "d2", "l1", "l2"):
                    if row[key]:
                        fields[key] = int(row[key]) if key == "angle" else _num(row[key])
                _, was_created = Product.objects.update_or_create(code=row["code"], defaults=fields)
                created += was_created
                updated += not was_created

        aliases = 0
        alias_path = Path(opts["aliases"])
        if alias_path.exists():
            with open(alias_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    product = Product.objects.get(yapib=row["yapib"])
                    alias = ProductAlias(product=product, text=row["text"], source=row["source"])
                    if not ProductAlias.objects.filter(product=product, text=row["text"]).exists():
                        alias.full_clean(exclude=["normalized"])
                        alias.save()
                        aliases += 1

        self.stdout.write(self.style.SUCCESS(f"Изделий: новых {created}, обновлено {updated}; синонимов добавлено {aliases}"))
