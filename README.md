# Suvvy ↔ Tourvisor Bridge

FastAPI-сервис-прослойка для интеграции Suvvy с Tourvisor Search API.

## Что делает

1. Принимает параметры тура по `POST /api/suvvy/tour-search`.
2. Проверяет токен Suvvy через заголовок `Authorization: Bearer ...`.
3. В mock-режиме возвращает тестовые туры.
4. В real-режиме обращается в Tourvisor API:
   - получает справочники городов вылета, стран, курортов, питания;
   - запускает поиск туров;
   - получает `searchId`;
   - ждёт результаты;
   - забирает результаты поиска;
   - отдаёт 3–5 вариантов в поле `client_text`.

## Endpoints

- `GET /health` — проверка работоспособности.
- `POST /api/suvvy/tour-search` — основной endpoint для Suvvy.

## Переменные Amvera

```env
SUVVY_WEBHOOK_TOKEN=savvi-tourvisor-test-2026
MOCK_TOURVISOR=true
TOURVISOR_API_BASE_URL=https://api.tourvisor.ru
TOURVISOR_JWT=jwt_токен_от_Tourvisor
TOURVISOR_CURRENCY=RUB
TOURVISOR_TIMEOUT_SECONDS=20
TOURVISOR_POLL_ATTEMPTS=4
TOURVISOR_POLL_INTERVAL_SECONDS=3
TOURVISOR_RESULTS_LIMIT=25
LOG_LEVEL=INFO
```

Для реального Tourvisor API нужно поставить:

```env
MOCK_TOURVISOR=false
```

## Пример POST-запроса

```json
{
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
  "meal": "всё включено",
  "hotel_stars": 5
}
```

Заголовки:

```http
Authorization: Bearer savvi-tourvisor-test-2026
Content-Type: application/json
```

## Важные ограничения

- Сервис не подтверждает бронирование.
- Цены и наличие нужно проверять менеджером перед продажей.
- Если есть дети, Tourvisor требует возраст каждого ребёнка. Передавайте `children_ages`.
