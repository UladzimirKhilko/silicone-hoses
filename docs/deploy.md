# Установка на сервер предприятия — инструкция для администратора

Сервис «Патрубки BSI» — веб-приложение (Python/Django) в Docker: три контейнера —
база PostgreSQL, приложение, веб-сервер Caddy. Пользователи открывают его в браузере
на компьютере или телефоне.

## Требования

| | Минимум |
|---|---|
| ОС | Linux (Ubuntu 22.04/24.04, Debian 12 — проще всего) или Windows Server 2019+ / Windows 10/11 с Docker Desktop (WSL 2) |
| Память | 2 ГБ (сервис использует ~0,5–1 ГБ) |
| Диск | 5 ГБ свободно (сейчас данные ~0,3 ГБ, рост — чертежи и сканы ТТН) |
| Docker | Docker Engine 24+ и плагин Docker Compose |
| Интернет | Нужен при установке и обновлении (скачать образы, библиотеки и папку с Яндекс.Диска). Для работы — не нужен |

Порты: **80** (и **443**, если будет доступ из интернета с HTTPS).

## 1. Установка Docker (Linux)

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
```

Windows: установить Docker Desktop (https://www.docker.com/products/docker-desktop/),
включить WSL 2. Дальше команды те же — в PowerShell.

## 2. Получение программы

```bash
sudo mkdir -p /opt/silicone-hoses && cd /opt/silicone-hoses
git clone https://github.com/UladzimirKhilko/silicone-hoses.git .
git checkout claude/busy-bohr-hd5pfq        # до слияния в основную ветку
```

Репозиторий закрытый — нужен доступ к GitHub (логин владельца или токен только на чтение).

## 3. Настройка

```bash
cp .env.example .env
nano .env
```

| Параметр | Что указать |
|---|---|
| `DJANGO_SECRET_KEY` | случайная строка: `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | пароль базы: `openssl rand -hex 16` |
| `DJANGO_ALLOWED_HOSTS` | IP сервера в сети (например `192.168.1.10`) и домен, если есть, через запятую |
| `SITE_ADDRESS` | `:80` — только внутри сети предприятия. Домен (`sklad.bpabsi.by`) — доступ из интернета, HTTPS-сертификат Caddy получит сам |
| `DJANGO_HTTPS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | только для домена: `DJANGO_HTTPS=1`, `DJANGO_CSRF_TRUSTED_ORIGINS=https://sklad.bpabsi.by` |
| `HTTP_PORT` | если порт 80 занят — другой (например `8080`); тогда адрес `http://192.168.1.10:8080` |

## 4. Запуск

```bash
docker compose up -d --build
docker compose ps                      # все три сервиса — Up
```

## 5. Первичное наполнение (один раз, 3–10 минут)

```bash
docker compose exec web deploy/first-run.sh
```

Скрипт скачает папку «Патрубки» с Яндекс.Диска, загрузит справочник (103 изделия), прикрепит
чертежи и перенесёт историю приходов и отгрузок МТЗ из Excel.

Пользователи:

```bash
docker compose exec web python manage.py createsuperuser     # администратор (руководитель)
```

Остальных пользователей удобно создать в браузере: `http://<адрес>/admin/` → «Пользователи» →
добавить, указать группу «Руководитель» или «Менеджер».

Проверка: открыть `http://<IP сервера>` (или `:8080`) → страница входа.

## 6. Доступ с телефона вне офиса

Два варианта (выбирает администратор):

1. **VPN** в сеть предприятия — сервис остаётся внутренним, телефон подключается к VPN. Безопаснее.
2. **Домен + HTTPS**: DNS-запись (например `sklad.bpabsi.by`) на внешний IP, проброс портов
   80 и 443 на сервер, в `.env` — `SITE_ADDRESS=sklad.bpabsi.by`, `DJANGO_HTTPS=1`,
   `DJANGO_ALLOWED_HOSTS=sklad.bpabsi.by`, `DJANGO_CSRF_TRUSTED_ORIGINS=https://sklad.bpabsi.by`.
   Затем `docker compose up -d`. Сертификат Let's Encrypt Caddy получит и продлит сам.

## 7. Резервное копирование

```bash
deploy/backup.sh          # база + файлы в папку backups/, хранятся 30 дней
```

Ежедневно в 02:30 (cron):

```bash
crontab -e
30 2 * * * cd /opt/silicone-hoses && deploy/backup.sh >> backups/backup.log 2>&1
```

Папку `backups/` желательно копировать на другой диск или сетевое хранилище.

Восстановление (проверено):

```bash
docker compose stop web
docker compose exec -T db dropdb -U hoses hoses
docker compose exec -T db createdb -U hoses hoses
gunzip -c backups/db-ДАТА.sql.gz | docker compose exec -T db psql -q -U hoses hoses
docker compose start web
docker compose exec -T web tar xzf - -C /data < backups/media-ДАТА.tar.gz
```

## 8. Обновление (новые этапы)

```bash
cd /opt/silicone-hoses
deploy/backup.sh
git pull
docker compose up -d --build
```

Изменения базы применяются автоматически при запуске.

Новые чертежи, добавленные на Яндекс.Диск, подтягиваются командой
`docker compose exec web deploy/first-run.sh` (повторный запуск безопасен).

## Полезное

```bash
docker compose logs -f web          # журнал приложения
docker compose restart web          # перезапуск
docker compose down                 # остановить (данные сохраняются)
```
