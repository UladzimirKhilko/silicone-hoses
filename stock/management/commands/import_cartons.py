"""Загружает данные о коробках партий из data/cartons.csv (для бирок).

Партия ищется по номеру (02/25) или по инвойсу (TS2407202); изделие — по ЯПИБ.
Повторный запуск обновляет значения, не создавая дублей. Партии, которых нет, пропускаются.
"""

import csv
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q

from catalog.models import Product
from stock.models import Batch, CartonSpec


class Command(BaseCommand):
    help = "Загрузить коробки партий из data/cartons.csv"

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(Path(settings.BASE_DIR) / "data" / "cartons.csv"))

    def handle(self, file, **opts):
        done, skipped = 0, set()
        with open(file, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                batch = Batch.objects.filter(Q(number=row["batch"]) | Q(invoice=row["batch"])).first()
                product = Product.objects.filter(yapib=row["yapib"]).first()
                if batch is None or product is None:
                    skipped.add(row["batch"])
                    continue
                CartonSpec.objects.update_or_create(
                    batch=batch,
                    product=product,
                    qty_per_carton=int(row["qty_per_carton"]),
                    defaults={
                        "gross_kg": Decimal(row["gross_kg"]),
                        "net_kg": Decimal(row["net_kg"]) if row["net_kg"] else None,
                        "cartons": int(row["cartons"]) if row["cartons"] else None,
                        "source": row["source"],
                    },
                )
                done += 1
        msg = f"Коробок: {done}"
        if skipped:
            msg += f"; пропущено (нет партии): {', '.join(sorted(skipped))}"
        self.stdout.write(self.style.SUCCESS(msg))
