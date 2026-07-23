# agent_1-ingestion Specification

## Purpose

Parsers360 ingestion → `agent_1_v5.source` / `agent_1_v5.raw_posts`.

> Синкнуто из change `rework-agent-1-v5` (2026-07-23). Описывает целевое
> поведение v5 (`parsers360_ingest_v5.py`), провалидированное разовым
> реальным прогоном (27686 строк, 0 ошибок). Легаси-скрипт
> `parsers360_ingest.py` (пишет в `agent_1.raw_items`) остаётся в репо как
> референс (D6), но не запускается на постоянке — ingestion на v5-схему
> явно не поставлен на cron/systemd (задача 6.7 в архиве change).

## Requirements

### Requirement: Схема source и raw_posts
Агент 1 MUST владеть таблицами `source` и `raw_posts` (Postgres, схема v5,
`docs/architecture/schema_v5.dot`):

- `source`: `ID_source PK, name_source, URL_source, type varchar,
  importance float, Is_active bool` (бывший справочник source_type свёрнут
  в колонку `type`).
- `raw_posts`: `ID_raw_post, ID_source FK, parser varchar, title varchar,
  url varchar, content text, time_post timestamptz, lang varchar,
  metadata jsonb, collected_at timestamptz` — только то, что отдал парсер,
  поля атомарные и не склеиваются.

`raw_posts.url` — пермалинк конкретной новости (обязателен для дашборда);
`source.URL_source` — адрес издания; смешивать их нельзя.
`raw_posts.content` MUST храниться с компрессией LZ4
(`ALTER TABLE ... ALTER COLUMN content SET COMPRESSION lz4`).

#### Scenario: Поля не склеиваются
- **WHEN** парсер вернул документ с заголовком, ссылкой, текстом, датой и
  языком
- **THEN** каждое значение записывается в свою колонку (`title`, `url`,
  `content`, `time_post`, `lang`), а не в общий payload

#### Scenario: url — пермалинк новости
- **WHEN** дашборд строит ссылку на конкретную новость
- **THEN** используется `raw_posts.url`, а не `source.URL_source`

### Requirement: Неприкосновенность raw
Строки `raw_posts` MUST быть append-only для всего пайплайна: очистка,
дедупликация и любые downstream-процессы не удаляют и не мутируют raw.
`clean_posts` — пересобираемый слой: его можно сносить и перечищать, raw
остаётся источником правды.

#### Scenario: Перечистка корпуса
- **WHEN** слой `clean_posts` снесён и очистка запущена заново
- **THEN** все строки `raw_posts` остаются без изменений, и корпус
  пересобирается из них полностью

#### Scenario: Мусорный документ
- **WHEN** парсер вернул мусор, дубль или не-русский документ
- **THEN** строка в `raw_posts` всё равно создаётся; отбраковка отражается
  только в `clean_posts`

### Requirement: PK проектируется под будущее партиционирование
PK таблиц `raw_posts` и `clean_posts` MUST быть композитным с `time_post`
(`(ID, time_post)`), чтобы включение `PARTITION BY RANGE (time_post)` не
потребовало пересборки схемы. Само партиционирование в этом change не
включается.

#### Scenario: Создание таблиц
- **WHEN** выполняется DDL `raw_posts` / `clean_posts`
- **THEN** PK включает `time_post`, но таблицы создаются без
  `PARTITION BY`

### Requirement: Parsers360 ingestion в raw_posts
Ingestion-скрипт MUST забирать новости из вендорского API Parsers360 через
`POST` на `PARSERS360_API_URL` и записывать их в `raw_posts` (схема v5).
Каждый запрос MUST всегда включать `service=parser`, а по умолчанию —
`summary=true` и `company=true`. Вендорские особенности ответа
(JSON-строка вместо JSON, unix-таймстампы, `summary`/`companies`/
`is_duplicated`/`original_id`) сохраняются в `metadata jsonb`; источник
документа резолвится в `ID_source` по справочнику `source` (upsert по
`name_source`, кэшируется в рамках одного запуска).

#### Scenario: Обычный запуск ingestion
- **WHEN** скрипт запускается без переопределений через переменные окружения
- **THEN** он отправляет `POST` на `PARSERS360_API_URL` с
  `service=parser`, `summary=true`, `company=true`, параметром `start_at`
  равным вчерашней UTC-дате, и записывает результат в `raw_posts` с
  заполненными атомарными полями

#### Scenario: Источник ещё не известен справочнику
- **WHEN** документ пришёл от источника, отсутствующего в `source`
- **THEN** строка `source` создаётся автоматически (upsert по имени), но
  raw-строка не теряется

### Requirement: Пагинация и фиксация страниц
Ingestion MUST запрашивать страницы с фиксированным `limit=200` и
увеличивающимся `page`, коммитя в Postgres каждую успешно полученную
страницу немедленно (а не всё скачивание целиком).

#### Scenario: Ответ короче limit
- **WHEN** очередная страница от Parsers360 содержит меньше строк, чем
  `limit=200`, или пустая
- **THEN** ingestion прекращает пагинацию для текущего запуска

#### Scenario: Сбой между страницами
- **WHEN** запись страницы N в `raw_posts` завершилась успешно, а запрос
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
- исходный `id` элемента от парсера сохраняется в `metadata.external_id`;
- `summary`, `companies`, `source`, `is_duplicated` и `original_id`
  сохраняются в `raw_posts.metadata`.

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

### Requirement: Обязательные поля raw_posts
Каждая строка `raw_posts` MUST иметь `ID_source`, `parser`, `url`,
`content`, `time_post` и `collected_at`; `title`, `lang` и `metadata`
опциональны, но заполняются, когда парсер их отдаёт. Документ, которому
вендор не дал `url`, `content` или парсибельный `time_post`, MUST быть
пропущен (не вставлен), а не угадан.

#### Scenario: Строка без url
- **WHEN** документ не имеет пермалинка `url`
- **THEN** он не может быть вставлен как валидная строка `raw_posts`

### Requirement: Логирование ingestion
Ingestion MUST писать логи в stdout и, если задан
`PARSERS360_V5_LOG_FILE`, дополнительно в этот файл; по умолчанию — в
`logs/parsers360_v5.log` (отдельно от лога легаси-скрипта, чтобы не
перемежаться при параллельном существовании обоих).

#### Scenario: PARSERS360_V5_LOG_FILE не задан
- **WHEN** переменная окружения `PARSERS360_V5_LOG_FILE` не установлена
- **THEN** ingestion пишет логи в stdout и в `logs/parsers360_v5.log`