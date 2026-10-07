#!/bin/sh
# Перед запуском: дождаться базы и применить изменения её структуры.
set -e
until python -c "import django,os;os.environ.setdefault('DJANGO_SETTINGS_MODULE','hoses.settings');django.setup();from django.db import connection;connection.ensure_connection()" 2>/dev/null; do
  echo "Жду базу данных…"; sleep 2
done
python manage.py migrate --noinput
python manage.py setup_roles
exec "$@"
