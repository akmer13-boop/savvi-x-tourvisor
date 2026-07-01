# Suvvy ↔ Tourvisor Bridge

MVP-прослойка между Suvvy и Tourvisor: принимает параметры тура от Suvvy, обращается к Tourvisor API/DDAPI, выбирает 3–5 вариантов и возвращает готовый текст для клиента.

## Что уже готово

- `POST /api/suvvy/tour-search` — основной endpoint для Suvvy.
- `GET /health` — проверка живости сервиса.
- Авторизация входящего запроса через `Authorization: Bearer <SUVVY_WEBHOOK_TOKEN>`.
- MOCK-режим для тестирования до получения реального Tourvisor API.
- Место для подключения настоящего Tourvisor DDAPI: `app/tourvisor_client.py`.
- Dockerfile для деплоя на Amvera.

## Переменные окружения

Скопируйте `.env.example` в `.env` для локального запуска или задайте переменные в Amvera:

```env
SUVVY_WEBHOOK_TOKEN=change-me
TOURVISOR_API_KEY=put-tourvisor-key-here
TOURVISOR_SEARCH_URL=https://tourvisor.ru/api/search-placeholder
TOURVISOR_TIMEOUT_SECONDS=15
MOCK_TOURVISOR=true
TOURVISOR_PUBLIC_SEARCH_URL=
LOG_LEVEL=INFO
```

До получения документации Tourvisor оставьте `MOCK_TOURVISOR=true`.
После получения API-методов Tourvisor:

1. Установить `MOCK_TOURVISOR=false`.
2. Указать реальный `TOURVISOR_SEARCH_URL`.
3. Указать `TOURVISOR_API_KEY`.
4. Исправить `_build_tourvisor_payload()` и `_parse_tourvisor_response()` под официальный формат Tourvisor.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Проверка:

```bash
curl http://localhost:8000/health
```

Тестовый запрос как от Suvvy:

```bash
curl -X POST http://localhost:8000/api/suvvy/tour-search \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer change-me" \
  -d '{
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
    "meal": "Всё включено",
    "hotel_stars": 5,
    "hotel_preferences": "семейный отель, анимация",
    "beach_preferences": "песчаный пляж"
  }'
```

## Docker

```bash
docker build -t suvvy-tourvisor-bridge .
docker run --env-file .env -p 8000:8000 suvvy-tourvisor-bridge
```

## Настройка действия в Suvvy

Создать действие `find_tour` / `tour_search` типа **Вебхук**.

- Метод: `POST`
- URL: `https://<ваш-домен-amvera>/api/suvvy/tour-search`
- Headers:
  - `Content-Type: application/json`
  - `Authorization: Bearer <SUVVY_WEBHOOK_TOKEN>`
- Тело запроса: JSON с переменными клиента.
- Возвращаемый боту результат: поле `client_text`.

Пример тела:

```json
{
  "departure_city": "{{departure_city}}",
  "country": "{{country}}",
  "resort": "{{resort}}",
  "date_from": "{{date_from}}",
  "date_to": "{{date_to}}",
  "nights_from": "{{nights_from}}",
  "nights_to": "{{nights_to}}",
  "adults": "{{adults}}",
  "children": "{{children}}",
  "children_ages": "{{children_ages}}",
  "budget": "{{budget}}",
  "meal": "{{meal}}",
  "hotel_stars": "{{hotel_stars}}",
  "hotel_preferences": "{{hotel_preferences}}",
  "beach_preferences": "{{beach_preferences}}",
  "client_name": "{{client_name}}",
  "client_phone": "{{client_phone}}",
  "chat_id": "{{chat_id}}"
}
```

## Инструкция для бота Suvvy

```text
Перед вызовом функции поиска тура собери: город вылета, направление, даты или диапазон дат, количество ночей, взрослых, детей и возраст детей, бюджет, питание и пожелания к отелю.

После вызова функции показывай клиенту только результат из поля client_text. Не придумывай цены, отели, даты, наличие и рейсы самостоятельно.

Всегда сохраняй формулировку: “Цены актуальны на момент поиска. Перед бронированием менеджер проверит наличие, перелёт и финальную стоимость.”

Если клиент хочет бронировать — передай диалог менеджеру.
```
