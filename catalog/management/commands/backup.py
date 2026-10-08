"""Резервная копия для установки без Docker: база SQLite и файлы (чертежи, сканы ТТН) в один ZIP.

    python manage.py backup D:\\Backups\\Патрубки --keep-days 30

Копия базы делается средствами SQLite (корректна даже во время работы пользователей).
"""

import datetime as dt
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Сделать резервную копию базы и файлов в ZIP"

    def add_arguments(self, parser):
        parser.add_argument("target")
        parser.add_argument("--keep-days", type=int, default=30)

    def handle(self, target, keep_days, **opts):
        if connection.vendor != "sqlite":
            raise CommandError("Для PostgreSQL используйте deploy/backup.sh (pg_dump).")
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M")
        out = target / f"patrubki-{stamp}.zip"
        with tempfile.TemporaryDirectory() as tmp:
            db_copy = Path(tmp) / "db.sqlite3"
            src = sqlite3.connect(settings.DATABASES["default"]["NAME"])
            dst = sqlite3.connect(db_copy)
            with dst:
                src.backup(dst)
            src.close()
            dst.close()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_copy, "db.sqlite3")
                media = Path(settings.MEDIA_ROOT)
                for f in media.rglob("*"):
                    if f.is_file():
                        zf.write(f, Path("media") / f.relative_to(media))
        cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
        for old in target.glob("patrubki-*.zip"):
            if dt.datetime.fromtimestamp(old.stat().st_mtime) < cutoff:
                old.unlink()
        self.stdout.write(self.style.SUCCESS(f"Копия: {out} ({out.stat().st_size // 1024} КБ)"))
