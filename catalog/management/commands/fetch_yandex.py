"""Скачивает публичную папку «Патрубки» с Яндекс.Диска (чертежи, документы, Excel).

    python manage.py fetch_yandex /data/source

Уже скачанные файлы того же размера пропускаются — команду можно запускать повторно,
чтобы докачать новые чертежи.
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

API = "https://cloud-api.yandex.net/v1/disk/public/resources"


def _get(url, **params):
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=60) as resp:
        return json.load(resp)


def walk(public_key, path="/"):
    offset = 0
    while True:
        data = _get(API, public_key=public_key, path=path, limit=200, offset=offset)
        items = data["_embedded"]["items"]
        for item in items:
            if item["type"] == "dir":
                yield from walk(public_key, item["path"])
            else:
                yield item
        offset += len(items)
        if offset >= data["_embedded"]["total"]:
            return


class Command(BaseCommand):
    help = "Скачать папку «Патрубки» с Яндекс.Диска"

    def add_arguments(self, parser):
        parser.add_argument("target")
        parser.add_argument("--url", default=settings.YANDEX_PUBLIC_URL)

    def handle(self, target, url, **opts):
        root = Path(target)
        got = skipped = 0
        for item in walk(url):
            dst = root / item["path"].lstrip("/")
            if dst.exists() and dst.stat().st_size == item["size"]:
                skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            href = _get(f"{API}/download", public_key=url, path=item["path"])["href"]
            tmp = dst.with_name(dst.name + ".part")
            urllib.request.urlretrieve(href, tmp)
            tmp.replace(dst)
            got += 1
            self.stdout.write(f"  {item['path']}")
        self.stdout.write(self.style.SUCCESS(f"Скачано {got}, уже были {skipped}"))
