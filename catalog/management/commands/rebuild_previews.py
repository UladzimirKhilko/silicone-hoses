"""Пересобирает картинки для просмотра всех чертежей."""

from django.core.management.base import BaseCommand

from catalog.models import DrawingFile


class Command(BaseCommand):
    help = "Пересобрать картинки чертежей"

    def handle(self, *args, **opts):
        done = 0
        for drawing in DrawingFile.objects.all():
            drawing.build_preview()
            drawing.save()
            done += bool(drawing.preview)
        self.stdout.write(self.style.SUCCESS(f"Картинок: {done}"))
