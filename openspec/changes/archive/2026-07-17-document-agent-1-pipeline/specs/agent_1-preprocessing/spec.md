## ADDED Requirements

### Requirement: Триггер preprocessing и claim job'ов
`preprocess_worker` MUST забирать строки `agent_1.processing_jobs` с
`job_type='preprocess'` через `FOR UPDATE SKIP LOCKED`, чтобы несколько
воркеров могли работать параллельно без двойной обработки одного job'а.

#### Scenario: Два воркера запущены одновременно
- **WHEN** два процесса `preprocess_worker` одновременно пытаются забрать
  job'ы из очереди `pending`
- **THEN** каждый `pending` job обрабатывается ровно одним воркером

### Requirement: Очистка и нормализация текста
`preprocess_worker` MUST выполнять извлечение текста из `raw_payload` (когда
`raw_text` отсутствует), html-очистку, unicode- и whitespace-нормализацию, а
также лёгкую очистку типового англоязычного boilerplate-мусора (маркеры вида
`Your browser does not support the video tag`, `Don't Miss`, `Most Read`,
`Read Next`, `Related Articles`, `Recommended`), включая разлепление
склеенных токенов вроде `TuesdayDon't` перед поиском этих маркеров.

#### Scenario: Текст содержит склеенный маркер boilerplate
- **WHEN** очищаемый текст содержит склеенные токены вида `MissNews` рядом с
  известным маркером мусорного блока
- **THEN** worker разлепляет токены перед поиском маркеров и вырезает
  найденный мусорный блок

### Requirement: Определение языка и ru-only фильтрация
`preprocess_worker` MUST классифицировать документ как русский или нет по
объёму алфавитного текста и доле кириллических букв: минимум
`min_alpha_chars=20` алфавитных символов и `min_russian_letter_ratio=0.55`
доли кириллицы. Если после очистки текста достаточно алфавитных символов
для классификации, но документ не проходит по доле кириллицы, worker
MUST пометить `raw_items.source_metadata.preprocess.status='filtered_out'`
с `reason='non_russian_text'`, закрыть job как `done` и не создавать строку
`clean_items`.

#### Scenario: Документ преимущественно на английском
- **WHEN** очищенный текст содержит достаточно алфавитных символов для
  классификации, но доля кириллических букв ниже `0.55`
- **THEN** `clean_items`-строка не создаётся, а `raw_items` помечается
  `status='filtered_out'`, `reason='non_russian_text'`

#### Scenario: Текста недостаточно для классификации языка
- **WHEN** очищенный текст содержит меньше `min_alpha_chars=20` алфавитных
  символов
- **THEN** worker не применяет фильтр `non_russian_text` по недостатку
  данных для классификации

### Requirement: Junk-topic regex-фильтрация с business-guard
`preprocess_worker` MUST применять консервативный regex-слой из именованных
категорий шумовых человеческих тем — `weather`, `winter_holiday_noise`,
`traffic_and_pdd`, `school_incidents`, `crime_and_fraud`, `animals`,
`family_and_newborns`, `health_and_sleep`, `beauty_and_fashion`,
`lifestyle_and_food`, `food_and_meals`, `religion_and_obituaries`, `sports`,
`celebrities_and_gossip`, `transport_and_airport`, `gardening_and_hobby`,
`missing_persons_and_searches` — и не применять более широкие рискованные
категории (война/геополитика, происшествия, эпидемии/медицина, ЖКХ, туризм,
развлечения, крипто/consumer tech, соцвопросы и образование), так как они
слишком часто скрывают бизнес-релевантные сигналы.

Если документ совпадает с одной из активных junk-категорий И не содержит
явного банковского/сберовского/бизнес-контекста
(`PROTECTED_BUSINESS_CONTEXT_RE`: упоминания Сбера, банков, платежей,
юрлиц, зарплатных проектов, GenAI/GigaChat, мобильного приложения,
кибербезопасности и т.п.), worker MUST пометить
`raw_items.source_metadata.preprocess.status='filtered_out'` с
`reason='junk_topic_regex'` (и категорией совпадения), закрыть job как
`done` и не создавать строку `clean_items`.

#### Scenario: Новость про погоду без бизнес-контекста
- **WHEN** новость подходит под категорию `weather` и не содержит
  защищённых бизнес-терминов
- **THEN** `clean_items`-строка не создаётся, `raw_items` помечается
  `status='filtered_out'`, `reason='junk_topic_regex'`,
  `junk_category='weather'`

#### Scenario: Новость про спорт с явным банковским контекстом
- **WHEN** новость подходит под категорию `sports`, но также содержит
  защищённый бизнес-термин (например, "банковская карта" или "GigaChat")
- **THEN** business-guard предотвращает фильтрацию, и документ проходит
  дальше в pipeline как обычно

### Requirement: Exact dedup по summary-independent ключу
`preprocess_worker` MUST выполнять exact-дедуп по ключу, построенному из
нормализованного `title + FULL clean_text` — этот ключ намеренно не зависит
от `source_metadata.summary` и не обрезается лимитом near-dup контента, так
что два побайтово идентичных документа с разными или отсутствующими
summary всегда совпадают по этому ключу.

#### Scenario: Два документа с одинаковым текстом, но разным summary
- **WHEN** два `raw_items` имеют идентичные `title` и `clean_text`, но
  разные значения `source_metadata.summary`
- **THEN** второй документ распознаётся как точный дубликат первого

### Requirement: MinHash near-dup фильтрация
Near-dup текст MUST строиться отдельно от exact-ключа: из нормализованного
`title + summary + content[:4000]`, токен-нормализованного (`lower()`,
удаление пунктуации, `ё -> е`), хэшированного смешанными словными шинглами
длиной `5` и символьными шинглами длиной `17`, пропущенного через `128`
MinHash-перестановок (`MINHASH_SIZE=128`), с LSH-бэндингом `32` бэнда по `4`
строки (`MINHASH_BANDS=32`, `MINHASH_ROWS_PER_BAND=4`, кандидатный порог
similarity ≈`0.42`), и принятого как near-duplicate при точном
Jaccard-сходстве шинглов не ниже `0.7` (`near_duplicate_threshold=0.7`).
Near-dup фильтрация MUST применяться только к новостным (news-like)
документам, а не ко всем `document_type`.

Ранее (до 2026-07-03) эта же конфигурация использовала `64` перестановки и
`16` бэндов по `4` строки; текущее значение — `128`/`32×4`.

#### Scenario: Near-duplicate news-документ выше порога
- **WHEN** документ является LSH-кандидатом и точное сходство шинглов с уже
  сохранённым документом ≥ `0.7`
- **THEN** текущая raw-строка помечается в `source_metadata.preprocess` как
  near-duplicate, `clean_items`-строка не создаётся, job закрывается как
  `done`

#### Scenario: Near-dup фильтр не применяется к не-новостному документу
- **WHEN** `document_type` документа не является новостным (news-like)
- **THEN** MinHash near-dup фильтрация к нему не применяется, даже если
  текст похож на уже сохранённый документ

### Requirement: Успешное создание clean_items
Если документ проходит определение языка, junk-topic фильтр и оба уровня дедупа, worker MUST записать одну строку в `agent_1.clean_items` и
поставить в очередь одну job-строку `job_type='label_kr'`,
`entity_type='clean_item'`, `entity_id=clean_items.id`, `status='pending'`.

#### Scenario: Документ проходит preprocessing без отклонений
- **WHEN** документ — русскоязычный, не подпадает под junk-topic фильтр, не
  является ни exact-, ни near-duplicate
- **THEN** создаётся строка `clean_items`, и для неё ставится в очередь
  ровно один `label_kr` job

### Requirement: Сохранение raw-строки при любом исходе
Preprocessing MUST всегда сохранять исходную `raw_items`-строку независимо
от исхода (создан `clean_items` или нет); решение дедупа/фильтрации
записывается в `raw_items.source_metadata.preprocess`, а не приводит к
удалению или изменению самой raw-строки.

#### Scenario: Документ отфильтрован любой причиной
- **WHEN** документ отфильтрован как дубликат, non_russian_text или
  junk_topic_regex
- **THEN** соответствующая строка `raw_items` остаётся в БД без изменений
  содержимого, только с добавленным `source_metadata.preprocess`
