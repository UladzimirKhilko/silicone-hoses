# Silicone Hoses — учёт силиконовых патрубков из Китая

Сервис учёта поставок силиконовых патрубков от китайских заводов: от заказа на заводе
до остатков на складе, себестоимости и ответа на заявку клиента.

**Статус:** анализ и интервью завершены, решение — своя разработка. Следующий шаг — этап 1 (см. docs/06-roadmap.md).

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
| [docs/glossary.md](docs/glossary.md) | Глоссарий терминов |

## Папка `samples/`

Сюда кладём образцы документов (инвойсы, упаковочные листы, ТТН, чертежи, заявки),
по которым будут писаться и тестироваться парсеры. См. [samples/README.md](samples/README.md).
