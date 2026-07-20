## ADDED Requirements

### Requirement: Триггер semantic-enrichment и claim job'ов
Каждый `clean_items.id`, получивший хотя бы одну строку в `document_kr_labels`, MUST порождать одну строку `processing_jobs` с
`job_type='extract_semantics'`, `entity_type='clean_item'`,
`status='pending'`. `extract_semantics_worker` MUST забирать такие job'ы
через `FOR UPDATE SKIP LOCKED`, поддерживая `retry_failed` и границы
`clean_item_id`.

#### Scenario: У документа нет ни одной KR-метки
- **WHEN** `process_job` вызывается для `clean_item_id`, у которого нет ни
  одной строки в `document_kr_labels`
- **THEN** job помечается как `failed` с причиной
  `missing_document_kr_labels`, и extraction не выполняется

### Requirement: Извлечение сущностей
`extract_semantics_worker` MUST выполнить один LLM-шаг `entities`,
возвращающий объект с группами `companies`, `products`, `people`,
`locations`, `technologies` (каждая — список строк). Каждое значение в
каждой группе MUST быть grounded — дословно встречаться в заголовке или
`clean_text` документа (включая нормализацию по снятию обрамляющих кавычек
и схлопыванию пробелов), иначе ответ отклоняется как невалидный.

#### Scenario: Значение сущности отсутствует в тексте
- **WHEN** одно из значений в `entities.companies` (или любой другой
  группе) не находится дословно (с учётом нормализации) в заголовке или
  `clean_text`
- **THEN** весь ответ шага `entities` отклоняется как невалидный

#### Scenario: Группа сущностей отсутствует в ответе
- **WHEN** агент не вернул одну из пяти обязательных групп
  (`companies`/`products`/`people`/`locations`/`technologies`)
- **THEN** worker трактует отсутствующую группу как пустой список, не
  отклоняя весь ответ из-за этого

### Requirement: Извлечение событий
`extract_semantics_worker` MUST выполнить один LLM-шаг `events`,
возвращающий список событий, каждое из которых обязано иметь
`event_type`, `summary` и `evidence` (список строк, каждый — grounded в
тексте документа); `participants` и `event_time` опциональны.
Извлечённые события MUST описывать только явно указанные в документе
факты, а не выводимую стратегию, влияние или скрытые причинно-следственные
связи.

#### Scenario: evidence события не встречается в тексте
- **WHEN** хотя бы один фрагмент `events[i].evidence` не находится дословно
  (с учётом нормализации) в заголовке или `clean_text`
- **THEN** весь ответ шага `events` отклоняется как невалидный

#### Scenario: event_time не указан
- **WHEN** агент не указал `event_time` для события (значение отсутствует
  или `null`)
- **THEN** worker сохраняет `event_time=null`, не отклоняя событие целиком

### Requirement: Контекст KR-меток в промптах extraction
Оба шага (`entities` и `events`) MUST получать в промпте краткий контекст
уже сохранённых `document_kr_labels` документа (`kr_id`, `impact`, `theme`,
`dashboard_description`, заголовок KR), чтобы extraction была согласована с
уже установленной релевантностью документа.

#### Scenario: У документа несколько KR-меток
- **WHEN** у документа сохранено несколько строк `document_kr_labels`
- **THEN** промпты `entities` и `events` включают краткое описание каждой
  из них в `label_context`

### Requirement: Чекпоинты и логирование
Каждый шаг (`entities`, `events`) MUST быть независимо чекпоинтирован в
`agent_1.extract_semantics_step_checkpoints` (ключ `clean_item_id,
step_name`), а каждый вызов `agent_1` — залогирован в
`agent_1.llm_call_logs`, аналогично `agent_1-kr-labeling`.

#### Scenario: Повторный запуск после успешного шага entities
- **WHEN** для `clean_item_id` уже есть чекпоинт для шага `entities`, но не
  для `events`
- **THEN** повторный прогон переиспользует сохранённый результат `entities`
  без повторного LLM-вызова и выполняет только `events`

### Requirement: Сохранение результата и границы ответственности
После успешного выполнения обоих шагов worker MUST записать результат в
`agent_1.document_enrichments` (`entities`, `events`) методом upsert по
`clean_item_id`. Агент MUST NOT вычислять триграммы, MUST NOT разрешать
аббревиатуры по корпоративному словарю и MUST NOT напрямую трогать
состояние очереди или персистентность — это ответственность
`agent_1-deterministic-enrichment` и самого воркера соответственно.

#### Scenario: Успешное завершение обоих шагов
- **WHEN** и `entities`, и `events` успешно провалидированы
- **THEN** `document_enrichments` для `clean_item_id` содержит оба
  результата, а `processing_jobs`-строка помечается `done`
