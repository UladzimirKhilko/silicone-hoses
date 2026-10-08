"""Запуск сервиса на Windows (без Docker): веб-сервер waitress.

Адрес и порт — из .env: HOST (по умолчанию 0.0.0.0), PORT (по умолчанию 8000).
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))
os.chdir(BASE_DIR)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hoses.settings")

from waitress import serve  # noqa: E402

from hoses.wsgi import application  # noqa: E402  (загружает и .env)

serve(
    application,
    host=os.environ.get("HOST", "0.0.0.0"),
    port=int(os.environ.get("PORT", "8000")),
    threads=8,
    max_request_body_size=60 * 1024 * 1024,
)
