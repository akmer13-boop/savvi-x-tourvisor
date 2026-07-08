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
4. Возвращает готовый текст в `client_text` и структурированный массив `tours`.

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

## Фото номеров

Tourvisor отдаёт фото номеров через метод `GET /search/api/v1/rooms` по `roomId` из результатов поиска. Если раздел API описания номеров не подключён у Tourvisor, сервис не падает: варианты туров вернутся без `room_images`.

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
  "hotel_stars": 5
}
```

## Ответ

```json
{
  "status": "ok",
  "found": true,
  "client_text": "...",
  "tours_count": 5,
  "search_id": "...",
  "tours": [
    {
      "hotel": "...",
      "room": "...",
      "room_id": 123,
      "room_images": ["https://..."],
      "price": 250000
    }
  ]
}
```

## Важные ограничения

- Сервис не подтверждает бронирование.
- Цены и наличие нужно проверять менеджером перед продажей.
- Если есть дети, Tourvisor требует возраст каждого ребёнка. Передавайте `children_ages`.
