"""Скачивает публичную папку «Патрубки» с Яндекс.Диска (чертежи, документы, Excel).

    python manage.py fetch_yandex /data/source

Уже скачанные файлы того же размера пропускаются — команду можно запускать повторно,
чтобы докачать новые или недокачанные файлы.

Яндекс.Диск иногда отвечает временной ошибкой (500, 429, 503) при частых запросах —
такие запросы повторяются с паузой; файл, который так и не скачался, не прерывает
загрузку остальных.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

API = "https://cloud-api.yandex.net/v1/disk/public/resources"
RETRY_STATUSES = {429, 500, 502, 503, 504}
ATTEMPTS = 6  # паузы 2, 4, 8, 16, 32 с — около минуты на один файл в худшем случае
HEADERS = {"User-Agent": "patrubki-bsi/1.0"}
PAUSE_BETWEEN_FILES = 0.3


def _with_retries(action, sleep=time.sleep):
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return action()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES or attempt == ATTEMPTS:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == ATTEMPTS:
                raise
        sleep(2**attempt)


def _get(url, **params):
    def action():
        req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)

    return _with_retries(action)


def _download(href, dst):
    def action():
        req = urllib.request.Request(href, headers=HEADERS)
        tmp = dst.with_name(dst.name + ".part")
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as fh:
            while chunk := resp.read(1 << 16):
                fh.write(chunk)
        tmp.replace(dst)

    _with_retries(action)


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
        failed = []
        for item in walk(url):
            dst = root / item["path"].lstrip("/")
            if dst.exists() and dst.stat().st_size == item["size"]:
                skipped += 1
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                href = _get(f"{API}/download", public_key=url, path=item["path"])["href"]
                _download(href, dst)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                failed.append(f"{item['path']} ({e})")
                self.stderr.write(f"  не скачался: {item['path']} — {e}")
                continue
            got += 1
            self.stdout.write(f"  {item['path']}")
            time.sleep(PAUSE_BETWEEN_FILES)
        self.stdout.write(self.style.SUCCESS(f"Скачано {got}, уже были {skipped}"))
        if failed:
            raise CommandError(
                f"Не скачались {len(failed)} файл(ов). Запустите команду ещё раз — "
                "уже скачанные файлы повторно не загружаются."
            )
