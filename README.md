# Suvvy ↔ Tourvisor Bridge v0.4.0

FastAPI-прослойка между SUVVY и Tourvisor с обязательной серверной политикой
контрактных туроператоров.

## Production endpoint

```text
POST /tour-search
```

Текущий внешний URL:

```text
https://suvvy-tourvisor.premium-world.ru/tour-search
```

Служебные проверки не обращаются к Tourvisor:

```text
GET /health
GET /ready
```

## Главное в v0.4.0

- Реальный Tourvisor всегда работает с непустым `active_contract` whitelist.
- Пустой, отсутствующий или повреждённый реестр блокирует старт приложения.
- `operatorIds` формируется только сервером и передаётся в Tourvisor.
- Каждый результат повторно фильтруется по `operator_id`.
- Бюджет является строгим верхним пределом без допуска `+10%`.
- `500` страхуется на сервере как `500000` рублей.
- Окно вылета ограничено семью календарными днями.
- Ночи, рассчитанные SUVVY, валидируются, но не пересчитываются.
- При `children=0` дополнительных вопросов нет; при наличии детей нужны все возраста.
- Ответ содержит `status`, `found`, `reason`, `request_id` и версию whitelist.
- Debug endpoints и CORS отключены по умолчанию.
- Основная авторизация — `Authorization: Bearer`.

## Реестр туроператоров

Файл по умолчанию:

```text
config/operator_registry.json
```

Формат:

```json
{
  "version": "2026-07-20.1",
  "operators": [
    {
      "tourvisor_id": 13,
      "name": "Anex",
      "status": "active_contract",
      "aliases": []
    }
  ]
}
```

Допустимые статусы:

- `active_contract` — разрешён для автоматической выдачи;
- `approved_to_contract` — не участвует в автоматической выдаче;
- `blocked` — исключён.

Текущий реестр версии `2026-07-21.1` содержит 15 подтверждённых
`active_contract` ID. При `MOCK_TOURVISOR=false` пустой или повреждённый список
по-прежнему не позволит приложению запуститься.

## Обязательные переменные Амверы

```text
APP_ENVIRONMENT=production
SERVICE_VERSION=0.4.0
GIT_COMMIT_SHA=<deployed commit>
SUVVY_WEBHOOK_TOKEN=<secret>
SUVVY_ALLOW_BODY_TOKEN=true
MOCK_TOURVISOR=false
TOURVISOR_API_BASE_URL=https://api.tourvisor.ru
TOURVISOR_JWT=<secret>
TOURVISOR_CURRENCY=RUB
OPERATOR_REGISTRY_PATH=config/operator_registry.json
ENABLE_DEBUG_ENDPOINTS=false
EXPOSE_API_DOCS=false
```

Значения секретов нельзя хранить в репозитории или выводить в логи.
`SUVVY_ALLOW_BODY_TOKEN=true` оставлен только для переходного периода. После
переключения SUVVY на Bearer его нужно установить в `false`.

Полный безопасный шаблон находится в `env.example`.

## Валидация запроса

Клиенту достаточно сообщить город вылета, направление, даты и общий бюджет.
SUVVY передаёт рассчитанные ночи и состав по умолчанию.

Сервер применяет правила:

- `50–999` рублей трактуются как тысячи;
- `50000+` передаются без изменений;
- `1–49` и `1000–49999` требуют уточнения;
- окно вылета — максимум семь календарных дат;
- дата начала вылета не может быть в прошлом;
- `nights_from <= nights_to`;
- разница диапазона ночей — максимум 10;
- при `children > 0` нужен возраст каждого ребёнка;
- цена результата не может превышать бюджет;
- результат без цены не показывается;
- результат без разрешённого `operator_id` не показывается.

## Необязательные пожелания

`resort`, `meal` и `hotel_stars` передаются в поддерживаемые параметры
Tourvisor. Свободные `hotel_preferences` и `beach_preferences` сохраняются в
`unverified_preferences` и передаются менеджеру для проверки. Они не считаются
выполненными, пока их нельзя подтвердить данными Tourvisor.

## Короткий ответ SUVVY

```json
{
  "status": "ok",
  "found": true,
  "reason": "FOUND",
  "request_id": "...",
  "client_text": "...",
  "tours_count": 3,
  "search_id": "...",
  "whitelist_version": "2026-07-20.1",
  "whitelist_hash": "...",
  "unverified_preferences": []
}
```

Основные причины:

- `FOUND`;
- `NO_MATCHES`;
- `INVALID_REQUEST`;
- `INVALID_BUDGET`;
- `INVALID_DATES`;
- `INVALID_NIGHTS`;
- `CHILD_AGES_REQUIRED`;
- `CONFIGURATION_ERROR`;
- `UPSTREAM_TIMEOUT`;
- `UPSTREAM_ERROR`.

## Локальная проверка

```text
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check app tests scripts
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q app
.venv/bin/python scripts/check_secrets.py
```

Тесты не обращаются к живому Tourvisor. Проверка реальной сериализации
`operatorIds` выполняется отдельно и только после явного разрешения.

## Развёртывание в Амвере

- Docker запускает `uvicorn app.main:app` на порту `80`.
- В образ копируются `app/` и `config/`.
- Существующий домен и ingress менять не требуется.
- `amvera.yml` не используется.
- Перед сборкой фиксируются Git commit и версия реестра.
- Rollback выполняется на заранее зафиксированный commit и совместимую
  конфигурацию.
