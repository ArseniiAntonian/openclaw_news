## ADDED Requirements

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

## RENAMED Requirements

- FROM: `### Requirement: Parsers360 ingestion в raw_items`
- TO: `### Requirement: Parsers360 ingestion в raw_posts`

- FROM: `### Requirement: Обязательные поля raw_items`
- TO: `### Requirement: Обязательные поля raw_posts`

## MODIFIED Requirements

### Requirement: Parsers360 ingestion в raw_posts
Ingestion-скрипт MUST забирать новости из вендорского API Parsers360 через
`POST` на `PARSERS360_API_URL` и записывать их в `raw_posts` (схема v5).
Каждый запрос MUST всегда включать `service=parser`, а по умолчанию —
`summary=true` и `company=true`. Вендорские особенности ответа
(JSON-строка вместо JSON, unix-таймстампы, `summary`/`companies`/
`is_duplicated`/`original_id`) сохраняются в `metadata jsonb`; источник
документа резолвится в `ID_source` по справочнику `source`.

#### Scenario: Обычный запуск ingestion
- **WHEN** скрипт запускается без переопределений через переменные окружения
- **THEN** он отправляет `POST` на `PARSERS360_API_URL` с
  `service=parser`, `summary=true`, `company=true` и записывает результат
  в `raw_posts` с заполненными атомарными полями

#### Scenario: Источник ещё не известен справочнику
- **WHEN** документ пришёл от источника, отсутствующего в `source`
- **THEN** строка `source` создаётся (или документ помечается для ручного
  резолва), но raw-строка не теряется

### Requirement: Обязательные поля raw_posts
Каждая строка `raw_posts` MUST иметь `ID_source`, `parser`, `url`,
`content`, `time_post` и `collected_at`; `title`, `lang` и `metadata`
опциональны, но заполняются, когда парсер их отдаёт.

#### Scenario: Строка без url
- **WHEN** документ не имеет пермалинка `url`
- **THEN** он не может быть вставлен как валидная строка `raw_posts`
