# agent_1-ingestion Specification

## Purpose

Parsers360 ingestion → `agent_1.raw_items` (`parsers360_ingest.py`).
Снимок «как есть» на 2026-07-17 (change `document-agent-1-pipeline`).

## Requirements

### Requirement: Parsers360 ingestion в raw_items
Ingestion-скрипт (`parsers360_ingest.py`) MUST забирать новости из
вендорского API Parsers360 через `POST` на `PARSERS360_API_URL` и записывать
их в `agent_1.raw_items`. Каждый запрос MUST всегда включать
`service=parser`, а по умолчанию — `summary=true` и `company=true`.

#### Scenario: Обычный запуск ingestion
- **WHEN** скрипт запускается без переопределений через переменные окружения
- **THEN** он отправляет `POST` на `PARSERS360_API_URL` с
  `service=parser`, `summary=true`, `company=true` и параметром
  `start_at`, равным вчерашней UTC-дате

### Requirement: Пагинация и фиксация страниц
Ingestion MUST запрашивать страницы с фиксированным `limit=200` и
увеличивающимся `page`, коммитя в Postgres каждую успешно полученную
страницу немедленно (а не всё скачивание целиком).

#### Scenario: Ответ короче limit
- **WHEN** очередная страница от Parsers360 содержит меньше строк, чем
  `limit=200`, или пустая
- **THEN** ingestion прекращает пагинацию для текущего запуска

#### Scenario: Сбой между страницами
- **WHEN** запись страницы N в `raw_items` завершилась успешно, а запрос
  страницы N+1 завершился ошибкой
- **THEN** строки страницы N остаются зафиксированными в БД независимо от
  исхода последующих страниц

### Requirement: Временное окно ingestion
Ingestion MUST на каждом запуске вычислять `start_at` как текущую UTC-дату
минус один день и отправлять это значение вендору как есть, без
дополнительной постфильтрации по времени на стороне клиента. CLI-выбор
произвольного интервала дат MUST NOT поддерживаться текущим боевым
скриптом.

#### Scenario: Запуск в любой момент суток UTC
- **WHEN** скрипт запускается в любое время текущих суток UTC
- **THEN** `start_at`, переданный вендору, равен вчерашней UTC-дате, и
  результат не фильтруется повторно по временным меткам после получения

### Requirement: Обработка вендорских особенностей ответа
Ingestion MUST корректно обрабатывать следующие особенности ответа
Parsers360:

- тело ответа может быть JSON-значением или JSON-закодированной строкой;
- `created_at` разбирается как unix-таймстамп в секундах, когда это
  возможно;
- исходный `id` элемента от парсера сохраняется как `external_id`;
- `summary`, `companies`, `source`, `is_duplicated` и `original_id`
  сохраняются в `source_metadata`.

#### Scenario: Тело ответа — JSON-строка
- **WHEN** Parsers360 возвращает тело ответа как JSON-закодированную строку
  вместо прямого JSON-значения
- **THEN** ingestion разбирает её так же корректно, как и обычный JSON

### Requirement: Аутентификация, устойчивость к сбоям и TLS
Ingestion MUST поддерживать вендорскую `Basic Auth` через
`PARSERS360_BASIC_USER`/`PARSERS360_BASIC_PASSWORD`, повторять неудачные
HTTP/JSON-запросы до 10 раз с паузой 10 секунд, использовать фиксированный
HTTP timeout 20 секунд и по умолчанию проверять TLS-сертификат (с
возможностью отключить проверку через `PARSERS360_VERIFY_SSL=false`).

#### Scenario: Временный сбой сети
- **WHEN** HTTP- или JSON-запрос к Parsers360 завершается ошибкой не более
  9 раз подряд
- **THEN** ingestion повторяет запрос с паузой 10 секунд и продолжает
  работу после успешного ответа

#### Scenario: Сбой исчерпывает лимит повторов
- **WHEN** запрос к Parsers360 завершается ошибкой 10 раз подряд для одной
  страницы
- **THEN** ingestion прекращает попытки для этой страницы согласно
  настроенному лимиту повторов

### Requirement: Триггер preprocess
Каждая новая строка `raw_items.id` MUST автоматически порождать ровно одну
строку `agent_1.processing_jobs` с `job_type='preprocess'`,
`entity_type='raw_item'`, `entity_id=raw_items.id`, `status='pending'` — это
делает БД-триггер, а не сам ingestion-скрипт.

#### Scenario: Вставка новой raw-строки
- **WHEN** ingestion вставляет новую строку в `agent_1.raw_items`
- **THEN** БД-триггер создаёт ровно одну соответствующую `preprocess`
  job-строку без участия ingestion-кода

### Requirement: Обязательные поля raw_items
Каждая строка `raw_items` MUST иметь `source`, `document_type`, хотя бы
одно из `external_id`/`url` и хотя бы одно из `raw_text`/`raw_payload`.
`title`, `published_at` и `source_metadata` являются опциональными, но
полезными.

#### Scenario: Строка без external_id и без url
- **WHEN** документ не имеет ни `external_id`, ни `url`
- **THEN** он не может быть вставлен как валидная строка `raw_items`

### Requirement: Логирование ingestion
Ingestion MUST писать логи в stdout и, если задан `PARSERS360_LOG_FILE`,
дополнительно в этот файл; по умолчанию — в `logs/parsers360.log`.

#### Scenario: PARSERS360_LOG_FILE не задан
- **WHEN** переменная окружения `PARSERS360_LOG_FILE` не установлена
- **THEN** ingestion пишет логи в stdout и в `logs/parsers360.log`
