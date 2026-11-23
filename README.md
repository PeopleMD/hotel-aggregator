# Hotel aggregator

[![CI](https://github.com/PeopleMD/hotel-aggregator/actions/workflows/ci.yml/badge.svg)](https://github.com/PeopleMD/hotel-aggregator/actions/workflows/ci.yml)

Сервис собирает отели из нескольких источников, объединяет дубликаты, хранит историю цен и отдаёт всё через API.

**Стек:** Python 3.12, FastAPI, PostgreSQL, RabbitMQ, SQLAlchemy 2 (async), Alembic, httpx, BeautifulSoup, Docker Compose, pytest, GitHub Actions.

## Как устроено

```mermaid
flowchart LR
    U[Клиент] --> API[FastAPI]
    S[Scheduler] --> DB[(PostgreSQL)]
    S --> Q
    API --> DB
    API -- задача --> Q[[RabbitMQ: очередь на каждый источник]]
    Q --> W1[Worker 1]
    Q --> W2[Worker 2]
    W1 & W2 --> SRC[OSM / Wikivoyage / demo]
    W1 & W2 --> DB
    Q -- временная ошибка --> R[[retry 30 с / 2 мин / 10 мин]] --> Q
    Q -- постоянная ошибка --> D[[dead]]
```

| Источник | Формат | Лицензия | Что даёт |
|---|---|---|---|
| OpenStreetMap (Overpass) | JSON API | ODbL, © участники OpenStreetMap | координаты, звёзды |
| Wikivoyage | HTML | CC BY-SA | названия, координаты, часть цен |
| demo-supplier | HTML, пагинация | свой, **данные выдуманы** | цены, которые меняются, и сбои |

Открытые источники почти не меняют цены, поэтому для истории цен и проверки сбоев рядом поднимается `demo_supplier`: фейковый сайт, который отдаёт 10% ответов с ошибкой 503.

Wikivoyage выключен, пока не задан `CONTACT_URL`: Wikimedia требует, чтобы бот указывал контакт в User-Agent.

## Запуск

```bash
cp .env.example .env      # заполнить пароли и JWT_SECRET
docker compose up -d --build
docker compose exec api python -m aggregator.cli create-admin you@example.com
```

- Swagger: http://localhost:8000/docs (кнопка Authorize, затем `POST /jobs`)
- RabbitMQ: http://localhost:15672

```bash
curl "localhost:8000/hotels?city=chisinau&currency=EUR&price_max=60&sort=price"
```

## Решения

**Вежливый парсинг.** Проверка robots.txt, пауза между запросами к одному хосту, учёт `Retry-After`. Ограничения одного процесса мало: при запуске двух воркеров лимит нарушался и Overpass отвечал 429. Поэтому у каждого источника своя очередь с single active consumer: с сайтом работает только один воркер, остальные обслуживают другие источники и подхватывают работу, если он упал.

**Повторы.** Ошибки делятся на временные (таймаут, 429, 5xx) и постоянные (404, robots.txt, изменилась вёрстка). Временные уходят в retry-очередь с TTL, постоянные сразу в `parse.dead`. Пустой ответ считается ошибкой, чтобы сломанный парсер не выключил все предложения.

**Доставка at-least-once.** `ack` только после commit. Если воркер убит, RabbitMQ отдаст сообщение другому. Повтор безопасен: запись идемпотентна (`ON CONFLICT` по `source + external_id`).

**Задача не теряется между БД и очередью.** Сначала commit, потом publish. Если процесс упал между ними, scheduler найдёт зависшую задачу и опубликует её снова.

**Дубликаты.** Имя нормализуется (диакритика, слова вроде hotel), затем сравнивается через rapidfuzz вместе с расстоянием: похожее имя и не дальше 400 м. Без координат нужно почти полное совпадение. Две записи одного источника не объединяются. Параллельные воркеры одного города ждут друг друга через `pg_advisory_xact_lock`.

**Поиск.** `pg_trgm` с GIN-индексом, keyset-пагинация вместо OFFSET. Цены в разных валютах не сравниваются: для фильтра по цене нужно указать валюту.

**Безопасность.** argon2, JWT без ключа по умолчанию, контейнер не от root, порты открыты только на localhost.

## Тесты

```bash
docker compose up -d postgres rabbitmq
docker compose exec postgres createdb -U app hotels_test
export TEST_DATABASE_URL=postgresql+asyncpg://app:ПАРОЛЬ@localhost:5432/hotels_test
export TEST_RABBIT_URL=amqp://app:ПАРОЛЬ@localhost:5672/
uv run pytest
```

106 тестов, из них интеграционные идут на настоящих PostgreSQL и RabbitMQ: повторы, dead-letter, дубли сообщений, гонка двух воркеров, обрыв БД. CI дополнительно поднимает весь `docker compose` и прогоняет парсинг от запроса до результата.

## Ограничения

- Сопоставление перебирает все отели города. Для десятков тысяч отелей нужен предварительный отбор по геосетке или PostGIS.
- Пороги похожести подобраны вручную, без размеченной выборки.
- Валюты не конвертируются.
