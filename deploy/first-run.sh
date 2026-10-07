#!/bin/sh
# Первичное наполнение: справочник, чертежи с Яндекс.Диска, история из Excel МТЗ.
# Запуск:  docker compose exec web deploy/first-run.sh
# Можно запускать повторно: справочник и чертежи обновятся, история из Excel не задвоится.
set -e
SRC=/data/source
echo "1/4 Скачиваю папку «Патрубки» с Яндекс.Диска (~50 МБ, несколько минут)…"
python manage.py fetch_yandex "$SRC"
echo "2/4 Справочник изделий…"
python manage.py seed_catalog
echo "3/4 Чертежи…"
python manage.py import_drawings "$SRC"
echo "4/4 История приходов и отгрузок МТЗ…"
if python manage.py shell -v 0 -c "from stock.models import Batch; raise SystemExit(0 if Batch.objects.exists() else 1)"; then
  echo "    уже перенесена — пропускаю"
else
  python manage.py import_mtz_excel "$SRC/Учет патрубков МТЗ.xlsx"
fi
echo "Готово. Создайте пользователей: docker compose exec web python manage.py createsuperuser"
