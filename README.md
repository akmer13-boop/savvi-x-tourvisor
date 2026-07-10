# Suvvy ↔ Tourvisor Bridge v0.3.1

Готовая FastAPI-прослойка для связки Suvvy.ai и Tourvisor API.

## Что изменено в v0.3.1

- В поиск Tourvisor передаётся `hotelRating=4`.
- Дополнительно отбрасываются отели без рейтинга и с рейтингом ниже `4.0`.
- Рейтинг и туроператор не выводятся клиенту.
- Ответ оформлен иконками.
- Даты выводятся в понятном виде: `19 августа 2026 года`.
- Главное/продающее фото запрашивается через `GET /search/api/v1/hotels/{hotelId}` и выводится первым.
- Затем выводятся фотографии выбранного номера из `/search/api/v1/rooms`.
- Если API описаний отелей не подключён, поиск не падает и выводятся только фото номера.
- Из ответа удалён дублирующий вопрос о передаче менеджеру.
- При технической ошибке клиент получает безопасное закрывающее сообщение без кода ошибки.
- Добавлен отключаемый белый список туроператоров по Tourvisor ID.
- Интервал опроса статуса по умолчанию уменьшен с 3 до 2 секунд.

## Рабочий endpoint для Suvvy

```text
POST /tour-search
```

Пример внешнего URL:

```text
https://suvvy-tourvisor.premium-world.ru/tour-search
```

Короткий ответ для Suvvy:

```json
{
  "status": "ok",
  "found": true,
  "client_text": "...",
  "tours_count": 5,
  "search_id": "..."
}
```

Полный ответ для Swagger/диагностики:

```text
POST /tour-search-full
```

## Переменные Amvera

Обязательные:

```text
SUVVY_WEBHOOK_TOKEN=...
MOCK_TOURVISOR=false
TOURVISOR_API_BASE_URL=https://api.tourvisor.ru
TOURVISOR_JWT=...
TOURVISOR_CURRENCY=RUB
```

Рекомендуемые:

```text
TOURVISOR_TIMEOUT_SECONDS=20
TOURVISOR_POLL_ATTEMPTS=4
TOURVISOR_POLL_INTERVAL_SECONDS=2
TOURVISOR_RESULTS_LIMIT=25
TOURVISOR_MIN_HOTEL_RATING=4.0
TOURVISOR_ENABLE_HOTEL_IMAGES=true
TOURVISOR_HOTEL_IMAGES_LIMIT=1
TOURVISOR_ENABLE_ROOM_IMAGES=true
TOURVISOR_ROOM_IMAGES_LIMIT=2
LOG_LEVEL=INFO
```

## Белый список туроператоров

До получения финального маппинга оставьте:

```text
TOURVISOR_OPERATOR_WHITELIST_ENABLED=false
TOURVISOR_ALLOWED_OPERATOR_IDS=
```

После маппинга названий на ID Tourvisor:

```text
TOURVISOR_OPERATOR_WHITELIST_ENABLED=true
TOURVISOR_ALLOWED_OPERATOR_IDS=12,45,78
```

Сервис передаст `operatorIds` в поиск и дополнительно проверит ID оператора в полученных результатах.

Если флаг включён, но список ID пустой, фильтр не активируется, чтобы случайно не обнулить выдачу.

Шаблон для маппинга: `operator_whitelist_template.csv`.

## Фото

Порядок в клиентском ответе:

1. Первая фотография из `GET /search/api/v1/hotels/{hotelId}` — главное/продающее фото отеля.
2. До двух фотографий выбранного номера из `/search/api/v1/rooms`.

Метод описания отеля доступен только при подключённом платном разделе Tourvisor «API — описания отелей». Если он недоступен, в логах будет предупреждение HTTP 401/402/403, а клиент увидит только фотографии номера.

## Развёртывание

1. Распаковать архив в корень GitHub-репозитория.
2. Закоммитить изменения.
3. В Amvera запустить сборку и перезапуск.
4. Проверить:

```text
GET /health
POST /tour-search
```

## Важное для Suvvy

- В параметрах ответа: `client_text` → `$.client_text`.
- Успешный статус: `200`.
- Искусственную задержку перед вызовом действия отключить в интерфейсе Suvvy.
- Итоговая инструкция для бота находится в `SUVVY_FINAL_PROMPT_V3.txt`.

## v0.3.2: optimisation for Suvvy 1024-token output cap

The public Suvvy endpoints (`/tour-search`, `/suvvy`, `/api/suvvy/tour-search`)
return a compact selection by default: 3 hotels, one official hotel cover and
one room image per hotel. The full debug endpoint remains `/tour-search-full`.

Environment variables:

```env
SUVVY_TOURS_LIMIT=3
SUVVY_ROOM_IMAGES_PER_TOUR=1
SUVVY_COMPACT_OUTPUT=true
```

Amvera logs include `SUVVY_RESPONSE_METRICS` with character count, URL count and
a rough output-token estimate. This estimate is diagnostic only; Suvvy controls
the actual model tokenizer.
