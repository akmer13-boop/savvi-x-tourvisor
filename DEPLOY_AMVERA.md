# Amvera deployment and rollback runbook — Bridge v0.5.0

Этот runbook не содержит значений секретов и не разрешает
автоматический деплой или live-поиск. Каждое production-действие и
каждый запрос к живому Tourvisor требуют отдельного прямого
подтверждения.

Bitrix24, домен, DNS и ingress в релиз v0.5.0 не входят и не меняются.

## Зафиксированная исходная точка

- Рабочий Bridge baseline: v0.4.0.
- Git commit baseline: `fec185adba39bb97b3e06d10b2948315ac3b1850`.
- Whitelist: `2026-07-21.1`, 15 `active_contract` ID.
- Публичный endpoint: `https://suvvy-tourvisor.premium-world.ru/tour-search`.
- Контейнер слушает порт `80`; `amvera.yml` не используется.

Точный commit v0.5.0 дописывается в этот протокол только после
сборки и прохождения CI. На каждом этапе фиксируется единая пара:

```text
<exact Git commit> + <configuration snapshot/version>
```

Откат только кода или только переменных запрещён: они откатываются
совместимой парой.

## Production gates

Деплой запрещён, пока не выполнены все условия:

1. `config/operator_registry.json` содержит утверждённый непустой
   `active_contract` whitelist с ожидаемой версией/hash.
2. Unit, contract, concurrency tests, Ruff, compileall, secret scan и Docker build
   прошли.
3. Зафиксирован точный Git commit v0.5.0 и подготовлен rollback на
   `fec185adba39bb97b3e06d10b2948315ac3b1850`.
4. Сохранены только имена текущих переменных и их версия;
   значения секретов не копируются в чат, тикет или лог.
5. В Amvera подключен постоянный том `/data`, а production остаётся в
   режиме одной реплики до переноса guard на общее хранилище.
6. Подготовлен парный rollback SUVVY custom tool/rules на предыдущий
   контракт.
7. Проверено, что Bitrix24, домен, DNS и ingress не входят в diff.

## Переменные v0.5.0

Добавить или проверить только имена и несекретные режимы:

```text
APP_ENVIRONMENT=production
SERVICE_VERSION=0.5.0
API_CONTRACT_VERSION=2026-07-21.2
GIT_COMMIT_SHA=<exact v0.5.0 commit>
BUSINESS_TIMEZONE=Europe/Moscow

SUVVY_WEBHOOK_TOKEN=<secret>
SUVVY_PREVIOUS_WEBHOOK_TOKEN=
SUVVY_ALLOW_BODY_TOKEN=true

SEARCH_GUARD_ENABLED=false
SEARCH_GUARD_DB_PATH=/data/search_guard.sqlite3
SEARCH_GUARD_HMAC_SECRET=<independent secret>
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

`SEARCH_GUARD_NAMESPACE` — доверенная серверная константа; она не
берётся из входного `source`. `SEARCH_GUARD_HMAC_SECRET` не должен совпадать
с webhook/JWT или другим секретом.

## Этап A — совместимый деплой Bridge

1. Зафиксировать Git commit и версию конфигурации.
2. Оставить `SUVVY_ALLOW_BODY_TOKEN=true` только на переходный период.
3. Оставить `SEARCH_GUARD_ENABLED=false`, пока SUVVY не передаёт
   стабильный `chat_id`.
4. Оставить `TOURVISOR_PRICE_FROM_ENABLED=false`, пока не пройден
   отдельный Tourvisor contract test. В real-режиме `min`, `approx` и
   `range` до этого отказывают fail-closed до live-поиска; `max` и
   `unknown` не требуют `priceFrom`, mock/unit-тесты работают.
5. Синхронизировать в Amvera только утверждённый commit и собрать
   Docker-образ.
6. Убедиться, что образ содержит `app/` и `config/`, а Uvicorn запущен
   с `--no-access-log`.
7. Проверить `GET /health` и `GET /ready` без обращения к Tourvisor:
   - ожидаемые `SERVICE_VERSION` и `API_CONTRACT_VERSION`;
   - ожидаемые version/hash whitelist;
   - ненулевой `allowed_operator_count`;
   - ожидаемые guard/contract gates.
8. Не выполнять `/tour-search`, пока не получено отдельное разрешение.

## Этап B — отдельная проверка контракта Tourvisor

Этот этап не является частью обычного health-check. После отдельного
разрешения выполняется минимальный заранее согласованный тест,
который подтверждает:

- фактическое имя/сериализацию `priceFrom`;
- работу `priceFrom` без `priceTo` и обеих границ вместе;
- единицу цены и включительность границ;
- пагинацию/лимит результатов одного `search_id`;
- порядок по умолчанию и поддерживаемые параметры сортировки;
- наличие `operatorIds` и отсутствие результатов вне whitelist.

Секреты, персональные данные и открытый `chat_id` не пишутся в трассировку.
После успешной проверки фиксируются версия/дата внешнего контракта и
новая пара кода/конфига. Только затем можно установить
`TOURVISOR_PRICE_FROM_ENABLED=true` и заменить
`TOURVISOR_API_CONTRACT_VERSION=unverified` на зафиксированную версию.

## Этап C — миграция SUVVY и включение guard

1. Обновить SUVVY custom tool на `budget_type`, `budget_from`, `budget_to`,
   стабильный `chat_id` и `refresh_requested`.
   Для нового контракта не передавать legacy `budget` (кроме временного
   совпадающего дубля `max`: `budget == budget_to`).
   Необязательные `resort`/`meal` предпочтительно передавать как `null`,
   если клиент их не называл; распространённые плейсхолдеры Bridge также
   нормализует в `null`.
2. Перевести авторизацию SUVVY на `Authorization: Bearer <secret>`, не
   передавать `auth_token` в JSON.
3. Проверить ветвление SUVVY по `status`, `found`, `reason`, `request_id`
   и отсутствие автоповтора после timeout/error.
4. Создать отдельный HMAC-секрет и подключить постоянный том `/data`.
   Средствами Amvera создать на этом томе несекретный контрольный marker,
   выполнить контролируемый restart приложения и убедиться, что тот же marker
   сохранился. Только после этой проверки установить
   `SEARCH_GUARD_PERSISTENCE_VERIFIED=true` и `SEARCH_GUARD_ENABLED=true`.
5. После подтверждённого Bearer-вызова ротировать webhook-секрет в
   обеих системах и установить `SUVVY_ALLOW_BODY_TOKEN=false`.
6. Зафиксировать итоговую пару Git commit/config и версию SUVVY tool/rules.

Значения цен и готовые подборки не записываются в SQLite. Технический replay
готового ответа хранится только в памяти процесса и разрешён до 60 секунд;
фоновая очистка удаляет 72-часовые HMAC-записи, а явное обновление цен
расходует вторую фактическую попытку.

## Post-deploy checks без live-поиска

- `/health` отвечает и не обращается к Tourvisor.
- `/ready` показывает ожидаемые версии, whitelist и состояние guard.
- `/suvvy-debug` и `/docs` недоступны.
- Запрос без Bearer/body-token отклоняется.
- В Uvicorn access log нет HTTP-строк; application log не содержит секретов,
  PII и открытого `chat_id`.
- Файл guard находится в `/data`, а `/ready` fail-closed реагирует на
  недоступность guard при включённом режиме.

## Paired rollback

Rollback запускается при ошибке startup/readiness, нарушении whitelist,
неверной бюджетной границе, повторном live-поиске или утечке данных в
лог.

1. Отключить автоматическую выдачу и перевести новые обращения к
   менеджеру.
2. Зафиксировать `request_id`, Git/config/whitelist/API contract versions без
   копирования тела, PII или секретов.
3. Вернуть SUVVY custom tool/rules на последнюю совместимую версию.
4. Вернуть код Bridge на
   `fec185adba39bb97b3e06d10b2948315ac3b1850` и одновременно вернуть
   зафиксированный совместимый config snapshot.
5. Если секрет уже ротирован, не восстанавливать старое значение:
   настроить текущий секрет в обеих откатываемых частях.
6. Guard-базу в `/data` не удалять. При `SEARCH_GUARD_ENABLED=false` она не
   участвует в поиске; её можно исследовать/очистить позже по отдельной
   одобренной процедуре.
7. Проверить `/health` и `/ready` без live-поиска.
8. Если rollback вернул версию без enforced whitelist или несовместимый
   SUVVY-контракт, автовыдача остаётся отключённой.
