#!/bin/sh
# Резервная копия базы и файлов (чертежи, сканы ТТН). Хранится 30 дней.
# Запуск из папки проекта; для ежедневного запуска добавьте в cron:
#   30 2 * * * cd /opt/silicone-hoses && deploy/backup.sh >> backups/backup.log 2>&1
set -e
cd "$(dirname "$0")/.."
mkdir -p backups
STAMP=$(date +%Y-%m-%d_%H%M)
docker compose exec -T db pg_dump -U hoses hoses | gzip > "backups/db-$STAMP.sql.gz"
docker compose exec -T web tar czf - -C /data media > "backups/media-$STAMP.tar.gz"
find backups -name 'db-*.sql.gz' -mtime +30 -delete
find backups -name 'media-*.tar.gz' -mtime +30 -delete
echo "$(date) копия готова: backups/db-$STAMP.sql.gz, backups/media-$STAMP.tar.gz"
