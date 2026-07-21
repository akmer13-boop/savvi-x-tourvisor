# Suvvy ↔ Tourvisor Bridge v0.5.0

FastAPI-прослойка между SUVVY и Tourvisor с обязательным
серверным whitelist контрактных туроператоров, строгими бюджетными
режимами и защитой от дублирующих поисков.

Bitrix24 не входит в контур Bridge v0.5.0 и этим релизом не меняется.

## Endpoints

```text
POST /tour-search
GET /health
GET /ready
```

Текущий production URL:

```text
https://suvvy-tourvisor.premium-world.ru/tour-search
```

`/health` и `/ready` не запускают поиск Tourvisor. Debug endpoints, API docs
и CORS по умолчанию отключены.

## Что даёт v0.5.0

- Сохраняет fail-closed whitelist baseline v0.4.0 и двойную
  постфильтрацию `operator_id`.
- Добавляет бюджетный контракт `budget_type`, `budget_from`, `budget_to`.
- Поддерживает режимы `max`, `min`, `approx`, `range`, `unknown`.
- Блокирует Китай и запросы с 4+ детьми до обращения к
  Tourvisor.
- Вводит до двух фактических поисков на `chat_id` за 72 часа.
- Схлопывает параллельные и технические дубли, но не выдаёт
  устаревшие цены как свежие.
- Возвращает исправимые ошибки как HTTP 200 с
  `status=needs_clarification`.
- Отключает access log Uvicorn, чтобы URL/IP не попадали в обычный
  HTTP-лог.

## Бюджетный контракт

Канонические поля:

```json
{
  "budget_type": "range",
  "budget_from": 300000,
  "budget_to": 500000
}
```

| `budget_type` | Обязательные поля | Строгое правило |
|---|---|---|
| `max` | `budget_to` | Цена не выше потолка. Внутри полученной порции результатов сначала варианты в коридоре `budget_to - 100000 ... budget_to`, затем более дешёвые. |
| `min` | `budget_from` | Цена не ниже границы. |
| `approx` | `budget_to` | `budget_to` — опорная сумма; допускаются только цены в коридоре ±10%. |
| `range` | `budget_from`, `budget_to` | Обе границы включительны и строги. |
| `unknown` | нет | Технический поиск без ценового ограничения; whitelist продолжает действовать. |

Переходное поле `budget` трактуется как `max`. При использовании нового
контракта поле `budget` должно отсутствовать или быть `null`; исключение —
временный совместимый дубль `max`, где `budget` точно равен `budget_to`.
Серверная страховка
сохраняет нормализацию `50–999 → ×1000`. Подозрительные суммы не
запускают Tourvisor и требуют уточнения.

Нижняя граница должна передаваться в Tourvisor как `priceFrom`, а верхняя как
`priceTo`. Bridge в любом случае повторно фильтрует цены локально.
Фактическую сериализацию `priceFrom`, границы, пагинацию и сортировку
текущей версии Tourvisor API нужно подтвердить отдельным живым
контрактным тестом. Без прямого разрешения такой тест не запускается.
Пока внешний контракт не подтверждён,
`TOURVISOR_API_CONTRACT_VERSION=unverified` и
`TOURVISOR_PRICE_FROM_ENABLED=false`. В real-режиме `min`, `approx` и `range`
отказывают fail-closed до обращения к Tourvisor; `max` сохраняет строгий
потолок, а `unknown` не передаёт ценовых границ. Глобальный приоритет
100-тысячного коридора для `max` нельзя считать подтверждённым, пока не
проверены порядок и пагинация внешней выдачи. Mock/unit-тесты
остаются доступны.

## Защита от дублей и свежесть цен

- SUVVY передаёт стабильный `chat_id`; открытый `chat_id` не логируется и
  в guard-базе хранится только HMAC.
- На один `chat_id` разрешены два фактических поиска за 72 часа.
- `needs_clarification`, блокировка Китая и 4+ детей поиском не
  считаются.
- Если Tourvisor уже был вызван, timeout/error расходует попытку; Bridge
  не повторяет её автоматически.
- Одинаковый набор нормализованных параметров не запускается дважды
  из-за технического дубля.
- Цены и подборки не записываются в SQLite. Техническое восстановление
  уже полученного ответа хранится только в памяти процесса и допустимо
  не больше 60 секунд.
- Явная просьба клиента обновить цены передаётся как
  `refresh_requested=true` и запускает новый поиск, если осталась вторая
  попытка.
- Через 72 часа guard-записи удаляются; технические дубли не продлевают TTL.

Для одной production-реплики guard использует SQLite в постоянном томе
`/data`. До увеличения числа реплик guard нужно перенести в общее
хранилище.

## Whitelist и входной контракт

Реестр по умолчанию: `config/operator_registry.json`. Только записи со
статусом `active_contract` допускаются в автоматическую выдачу. Пустой,
отсутствующий или повреждённый реестр блокирует production-старт/поиск.

`operatorIds` никогда не принимается от SUVVY: поле отклоняется, а список
для Tourvisor формируется только на сервере.

Обязательные бизнес-данные: город вылета, направление, окно вылета,
ночи и бюджетный режим. `adults` по умолчанию равен 2, `children` — 0.
При 1–3 детях нужен возраст каждого. Свободные `hotel_preferences` и
`beach_preferences` остаются непроверенными пожеланиями для менеджера.
Если явно указанные `resort` или `meal` не находятся в справочнике
Tourvisor, Bridge возвращает `needs_clarification` и не расширяет поиск
молча до всей страны или любого питания. Технические плейсхолдеры
«любой/неважно/нет предпочтений» нормализуются в `null`.

## Ответ SUVVY

Успешный ответ и бизнес-ошибки имеют единую структуру:

```json
{
  "status": "ok",
  "found": true,
  "reason": "FOUND",
  "request_id": "...",
  "client_text": "...",
  "tours_count": 3,
  "search_id": "...",
  "whitelist_version": "2026-07-21.1",
  "whitelist_hash": "...",
  "unverified_preferences": []
}
```

Исправимые входные ошибки возвращают HTTP 200 и
`status=needs_clarification`; Tourvisor при этом не вызывается. Для
`UPSTREAM_TIMEOUT` и `UPSTREAM_ERROR` клиентский текст строго равен:

> Сейчас не удалось получить подборку. Я зафиксировала Ваш запрос — менеджер свяжется с Вами в ближайшее время.

## Авторизация и логи

Целевая авторизация — `Authorization: Bearer <secret>`. Входной `auth_token`
в JSON-теле допустим только во время миграции. После переключения SUVVY и
ротации секрета нужно установить `SUVVY_ALLOW_BODY_TOKEN=false`.

В логи не попадают секреты, ФИО, телефон, сырые пожелания и открытый
`chat_id`. Для трассировки используются `request_id`, версия/hash whitelist,
число разрешённых ID и HMAC-маркер диалога.

## Переменные production

```text
APP_ENVIRONMENT=production
SERVICE_VERSION=0.5.0
API_CONTRACT_VERSION=2026-07-21.2
GIT_COMMIT_SHA=<exact deployed commit>
BUSINESS_TIMEZONE=Europe/Moscow

SUVVY_WEBHOOK_TOKEN=<secret>
SUVVY_PREVIOUS_WEBHOOK_TOKEN=
SUVVY_ALLOW_BODY_TOKEN=true

SEARCH_GUARD_ENABLED=false
SEARCH_GUARD_DB_PATH=/data/search_guard.sqlite3
SEARCH_GUARD_HMAC_SECRET=<separate secret>
SEARCH_GUARD_NAMESPACE=suvvy-tourvisor
SEARCH_GUARD_TTL_SECONDS=259200
SEARCH_GUARD_MAX_SEARCHES=2
SEARCH_RESULT_REPLAY_TTL_SECONDS=45
SEARCH_GUARD_PRUNE_INTERVAL_SECONDS=15
SEARCH_GUARD_PERSISTENCE_VERIFIED=false

MOCK_TOURVISOR=false
TOURVISOR_API_BASE_URL=https://api.tourvisor.ru
TOURVISOR_JWT=<secret>
TOURVISOR_API_CONTRACT_VERSION=unverified
TOURVISOR_PRICE_FROM_ENABLED=false
OPERATOR_REGISTRY_PATH=config/operator_registry.json

ENABLE_DEBUG_ENDPOINTS=false
EXPOSE_API_DOCS=false
```

`SEARCH_GUARD_ENABLED=false` нужен для совместимого первого деплоя. Включать
guard можно только после того, как SUVVY начнёт передавать качественный `chat_id`.
Пустой `SUVVY_PREVIOUS_WEBHOOK_TOKEN` — нормальный режим; его задают только на
краткое согласованное окно ротации и затем очищают.
Шаблон без значений секретов находится в `env.example`.

## Локальная проверка

```text
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check app tests scripts
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app tests scripts
.venv/bin/python scripts/check_secrets.py
docker build .
```

Тесты не обращаются к живому Tourvisor. GitHub Actions можно запустить
вручную через `workflow_dispatch`, но это тоже не запускает live API-поиск.

## Деплой и rollback

Docker копирует `app/` и `config/`, запускает Uvicorn на порту `80` без
access log. Домен, DNS и ingress менять не нужно.

Каждый production-коммит деплоится только в паре с зафиксированной
версией конфигурации. Rollback должен возвращать и код, и совместимую
конфигурацию/контракт SUVVY. Подробный порядок описан в `DEPLOY_AMVERA.md`.
