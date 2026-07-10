# Suvvy ↔ Tourvisor Bridge

FastAPI-сервис-прослойка для интеграции Suvvy с Tourvisor Search API.

## Что делает

1. Принимает параметры тура по `POST /api/suvvy/tour-search`.
2. Проверяет токен Suvvy через `Authorization: Bearer ...` или `auth_token` в JSON.
3. В real-режиме обращается в Tourvisor API:
   - получает справочники городов вылета, стран, курортов, питания;
   - запускает поиск туров;
   - получает `searchId`;
   - ждёт результаты;
   - забирает результаты поиска;
   - выбирает 3–5 вариантов;
   - при наличии `roomId` запрашивает `/search/api/v1/rooms` и подтягивает фото/описания номеров.
4. Возвращает:
   - `client_text` — чистый текст без сырых ссылок на фото;
   - `tours` — структурированные данные по турам;
   - `cards` — компактные карточки;
   - `images` — отдельный массив фото с полными `https://` URL;
   - `messages` — упорядоченные блоки `text` / `image` для дальнейшего маппинга.

## Почему фото вынесены из client_text

Suvvy webhook-действие возвращает результат боту. По документации вебхуков результат можно вернуть как ответ/настраиваемые переменные, но универсального формата “покажи attachment из webhook response” в этом действии не описано. Поэтому в `client_text` не вставляем ссылки на фото, чтобы клиент не видел сырой URL. Фото возвращаем отдельно в `images` и `messages`.

Если канал/сценарий Suvvy умеет отправлять изображения из переменных — используйте `images[0].url`, `images[1].url` и т.д. Если нет — нужны отдельные шаги/канальный API, который умеет отправлять image attachments.

## Endpoints

- `GET /` — быстрый ответ сервиса.
- `GET /ping` — быстрый ping.
- `GET /health` — проверка работоспособности.
- `POST /api/suvvy/tour-search` — основной endpoint для Suvvy.

## Переменные Amvera

```env
SUVVY_WEBHOOK_TOKEN=savvi-tourvisor-test-2026
MOCK_TOURVISOR=false
TOURVISOR_API_BASE_URL=https://api.tourvisor.ru
TOURVISOR_JWT=jwt_токен_от_Tourvisor
TOURVISOR_CURRENCY=RUB
TOURVISOR_TIMEOUT_SECONDS=20
TOURVISOR_POLL_ATTEMPTS=4
TOURVISOR_POLL_INTERVAL_SECONDS=3
TOURVISOR_RESULTS_LIMIT=25
TOURVISOR_ENABLE_ROOM_IMAGES=true
TOURVISOR_ROOM_IMAGES_LIMIT=2
LOG_LEVEL=INFO
```

## Пример POST-запроса

```json
{
  "auth_token": "savvi-tourvisor-test-2026",
  "departure_city": "Москва",
  "country": "Турция",
  "resort": "Сиде",
  "date_from": "2026-08-10",
  "date_to": "2026-08-20",
  "nights_from": 7,
  "nights_to": 9,
  "adults": 2,
  "children": 1,
  "children_ages": [7],
  "budget": 250000,
  "meal": "all inclusive",
  "hotel_stars": 5,
  "image_mode": "structured"
}
```

## image_mode

- `structured` — по умолчанию. Текст чистый, фото отдельно в `images/messages`.
- `links_in_text` — добавляет ссылки на фото в `client_text`. Удобно для отладки, хуже для клиента.
- `none` — не возвращает фото в `images/messages`.

## Пример ответа

```json
{
  "status": "ok",
  "found": true,
  "client_text": "Нашёл предварительные варианты...",
  "tours_count": 5,
  "search_id": "...",
  "images": [
    {
      "tour_index": 1,
      "url": "https://static.tourvisor.ru/hotel_pics/rooms/...jpg",
      "caption": "1. HOTEL NAME — standard room",
      "file_name": "tour_1_hotel_name.jpg",
      "file_type": "image",
      "mime_type": "image/jpeg"
    }
  ],
  "cards": [
    {
      "index": 1,
      "title": "HOTEL NAME 5★",
      "image_url": "https://static.tourvisor.ru/hotel_pics/rooms/...jpg",
      "price": 202370
    }
  ],
  "messages": [
    {"type": "text", "text": "Нашёл предварительные варианты..."},
    {"type": "image", "url": "https://static.tourvisor.ru/...jpg", "caption": "1. HOTEL NAME — standard room"}
  ]
}
```

## Важные ограничения

- Сервис не подтверждает бронирование.
- Цены и наличие нужно проверять менеджером перед продажей.
- Если есть дети, Tourvisor требует возраст каждого ребёнка. Передавайте `children_ages`.
- Если у Tourvisor не подключён API описаний/номеров, туры вернутся без `room_images`, сервис не падает.

## Suvvy / Amvera routing fallback

If Suvvy receives an Amvera HTML `404 Not Found` for `/api/suvvy/tour-search`, use the root alias instead:

```text
https://<your-amvera-domain>/
```

This version accepts the same JSON body on all of these POST routes:

- `/`
- `/tour-search`
- `/suvvy`
- `/api/suvvy/tour-search`

Recommended Suvvy URL for problematic routing: `https://<your-amvera-domain>/`.


## Suvvy diagnostics endpoints

This build adds ultra-fast diagnostic endpoints for webhook debugging:

- `GET/POST/HEAD/OPTIONS /suvvy-debug`
- `GET/POST/HEAD/OPTIONS /debug`
- `GET/POST/HEAD/OPTIONS /tour-search-fast`

They return 200 immediately and do not call Tourvisor. Amvera logs will show `INCOMING` as soon as any request reaches FastAPI.


## Short response fix for Suvvy

This build makes Suvvy-facing endpoints return a compact response only:

- `status`
- `found`
- `client_text`
- `tours_count`
- `search_id`

Full payload with `tours/cards/images/messages` is available only on:

- `POST /tour-search-full`
- `POST /api/suvvy/tour-search-full`

Use `POST /tour-search` in Suvvy to avoid the “response length exceeds maximum allowed” error.


## 0.2.3 — structured image links for Suvvy

В этой сборке `POST /tour-search` остаётся коротким, но поле `client_text` теперь содержит прямые ссылки на 1–2 фотографии под каждым отелем.

Формат ответа для Suvvy:

```json
{
  "status": "ok",
  "found": true,
  "client_text": "Отель + параметры + прямые ссылки на фото",
  "tours_count": 5,
  "search_id": "..."
}
```

Что поменять в Suvvy после деплоя:

1. В настройках бота включить `Использовать структурированный ответ`, если доступно на тарифе.
2. В действии Tourvisor оставить параметр ответа: `client_text` → `$.client_text`.
3. В системную инструкцию добавить: `Если в ответе Tourvisor есть прямые ссылки на фото, не удаляй и не изменяй их. Отправь client_text клиенту без изменений.`
4. В теле запроса можно не указывать `image_mode`; по умолчанию используется `structured`.

Полный JSON с массивами `tours/cards/images/messages` по-прежнему доступен на `/tour-search-full` для отладки.


## Stakeholder update

Client-facing responses no longer include tour operator information.
Tour operator data may still be present only in internal Tourvisor raw payloads and is not displayed in `/tour-search` client_text, cards, or public tour dictionaries.
