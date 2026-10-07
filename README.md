# Silicone Hoses — учёт силиконовых патрубков из Китая

Сервис учёта поставок силиконовых патрубков от китайских заводов: от заказа на заводе
до остатков на складе, себестоимости и ответа на заявку клиента.

**Статус:** этап 1 (справочник изделий и чертежи) — готов к проверке. План — [docs/06-roadmap.md](docs/06-roadmap.md).

## Документация

| Документ | О чём |
|---|---|
| [docs/01-vision.md](docs/01-vision.md) | Видение: что делает сервис, для кого, границы |
| [docs/02-processes.md](docs/02-processes.md) | Бизнес-процессы: заказ → инвойс → упаковочный → ТТН → приход → остатки → заявка |
| [docs/03-data-model.md](docs/03-data-model.md) | Модель данных (сущности и связи) |
| [docs/04-cost-allocation.md](docs/04-cost-allocation.md) | Разнесение расходов и расчёт себестоимости |
| [docs/05-architecture.md](docs/05-architecture.md) | Архитектура и технологии |
| [docs/06-roadmap.md](docs/06-roadmap.md) | План разработки по этапам |
| [docs/07-what-i-need.md](docs/07-what-i-need.md) | Что нужно от заказчика, вопросы, доступы, навыки |
| [docs/08-sample-analysis.md](docs/08-sample-analysis.md) | Разбор реальных документов поставки TS2407202 и чертежей |
| [docs/09-product-identification.md](docs/09-product-identification.md) | Как различать изделия: чертёж = изделие, синонимы вместо «нормализации» |
| [docs/10-shipments-and-paperwork.md](docs/10-shipments-and-paperwork.md) | Отгрузки (ТТН), паспорта, бирки, заявка МТЗ — разбор образцов |
| [docs/11-current-excel.md](docs/11-current-excel.md) | Разбор текущего учёта МТЗ в Excel, найденные ошибки |
| [docs/12-ready-solutions.md](docs/12-ready-solutions.md) | Готовые решения (1С, МойСклад, InvenTree) vs своя разработка |
| [docs/13-interview.md](docs/13-interview.md) | Итоги интервью: пользователи, склад, приход, отгрузки, чертежи, заявки, отчёты |
| [docs/14-amkador-belaz.md](docs/14-amkador-belaz.md) | Чертежи Амкадора (BSI, завода, заказчика) и БелАЗа; расхождения |
| [docs/glossary.md](docs/glossary.md) | Глоссарий терминов |

## Папка `samples/`

Сюда кладём образцы документов (инвойсы, упаковочные листы, ТТН, чертежи, заявки),
по которым будут писаться и тестироваться парсеры. См. [samples/README.md](samples/README.md).

## Запуск для разработки

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py setup_roles          # роли «Руководитель», «Менеджер»
.venv/bin/python manage.py seed_catalog         # 103 изделия и синонимы из data/
.venv/bin/python manage.py import_drawings /путь/к/папке/Патрубки   # PDF чертежей (копия папки с Яндекс.Диска)
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
.venv/bin/pytest                                # тесты
```

Чертежи и документы заказчика в репозиторий не кладутся: `import_drawings` берёт их из локальной
копии папки «Патрубки», файлы хранятся в `media/`.

## Устройство

| Где | Что |
|---|---|
| `catalog/codes.py` | Нормализация названий (только безопасные отличия) и разбор кодов `BSI 90-D38L150/140-1` |
| `catalog/models.py` | Заказчики, изделия (ключ — ЯПИБ), синонимы, файлы чертежей с PNG-превью |
| `catalog/views.py` | Список с поиском, карточка, просмотр/скачивание чертежа, ZIP чертежей |
| `data/catalog.csv` | Справочник: код по штампу, ЯПИБ, заказчик, параметры, пути к чертежам |
| `data/aliases.csv` | Подтверждённые синонимы из поставки TS2407202 |
