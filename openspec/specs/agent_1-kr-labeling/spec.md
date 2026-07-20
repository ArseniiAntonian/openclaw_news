# agent_1-kr-labeling Specification

## Purpose

Source-ranking фильтр, relevance gate, impact-разметка, Sber PR-like флаг,
entity tonality, чекпоинты, логирование LLM-вызовов (`label_kr_worker.py`).
Снимок «как есть» на 2026-07-17 (change `document-agent-1-pipeline`).

> В архитектуре v5 эта капабилити уходит из Агента 1 (переезд в Агент 4 /
> этап после кластеризации) — см. change `rework-agent-1-v5`.

## Requirements

### Requirement: Триггер kr-labeling и claim job'ов
Каждая новая строка `clean_items.id` MUST порождать одну строку
`agent_1.processing_jobs` с `job_type='label_kr'`, `entity_type='clean_item'`,
`status='pending'`. `label_kr_worker` MUST забирать такие job'ы через
`FOR UPDATE SKIP LOCKED`, поддерживая опциональные границы `job_id`/
`clean_item_id` и режим `retry_failed`, который также забирает job'ы со
статусом `failed`, переиспользуя сохранённые чекпоинты по шагам.

#### Scenario: Нет активных key_results
- **WHEN** в `agent_1.key_results` нет ни одной строки с `active IS TRUE`
- **THEN** `label_kr_worker` оставляет `label_kr` job'ы в состоянии
  `pending` и логирует предупреждение, не продвигая документы к
  semantic-enrichment

### Requirement: Source-ranking фильтр по enrichment agent_2
Перед relevance gate `label_kr_worker` MUST для каждого активного KR
проверить `key_results.enrichment` (владение `agent_2`: поля `тема`,
`ключевые_слова`, `типы_источников`) и определить, разрешена ли разметка
этого KR для документа, сравнивая `source`/`domain`/`source_type` документа
с ранжированными источниками из enrichment. Источники с `важность=3` или
`важность=2` разрешены; `важность=1` — исключён.

Если у KR ранжированные `типы_источников` есть, но документ не выставляет
ни одного явного `source_type`, фильтр по умолчанию MUST быть fail-open
(`AGENT_1_LABEL_IGNORE_SOURCE_TYPE_RANKINGS`, по умолчанию `true`) — иначе
воркер рискует пропускать все документы без единого вызова агента, так как
большинство новостных документов не несут явный `source_type`.

#### Scenario: KR ещё не обогащён agent_2
- **WHEN** у активного KR `enrichment IS NULL`
- **THEN** KR становится кандидатом для relevance gate без ограничений по
  source-ranking (fail-open)

#### Scenario: Источник исключён явным ранжированием
- **WHEN** enrichment KR ранжирует конкретный `source`/`domain`/`source_type`
  документа с `важность=1` (или явным `include=false`)
- **THEN** KR исключается из кандидатов для этого документа ещё до
  relevance gate, без вызова агента

#### Scenario: У KR только source_type-ранжирование, а у документа нет source_type
- **WHEN** все ранжирования KR имеют тип `source_type`, и документ не
  предоставляет ни одного явного значения `source_type`
- **THEN** при включённом fail-open (по умолчанию) KR остаётся кандидатом,
  а не исключается

### Requirement: Relevance gate одним multi-KR вызовом
`label_kr_worker` MUST сделать один общий LLM-вызов (`agent_1`, шаг
`relevance`) для документов, у которых остался хотя бы один source-allowed
KR-кандидат, в котором сразу все KR-кандидаты передаются
как каталог целей, а не отдельный бинарный вызов на каждый KR. Ответ
MUST содержать список `matches` с `kr_id` из множества кандидатов,
`why_related` и `evidence`, где каждый фрагмент `evidence` обязан
дословно встречаться в заголовке или `clean_text` документа.

KR, не вошедшие в `matches`, считаются нерелевантными и MUST NOT получать
label `neutral` просто по факту прохождения через impact-промпт.

#### Scenario: Ни один кандидат не прошёл source-ranking фильтр
- **WHEN** для документа не осталось ни одного source-allowed KR-кандидата
- **THEN** relevance gate не вызывается вообще, job закрывается как `done`
  со статусом `skipped`/`source_rankings_filtered_all_krs`, и
  `document_kr_labels` не создаются

#### Scenario: KR не найден среди matches
- **WHEN** relevance gate вернул `matches`, не включающие конкретный
  KR-кандидат
- **THEN** этот KR попадает в `irrelevant_kr_ids`, не получает impact-вызов
  и не получает строку в `document_kr_labels`

#### Scenario: evidence не встречается в тексте документа
- **WHEN** фрагмент `evidence` в ответе relevance gate не встречается
  дословно (с учётом снятия обрамляющих кавычек и схлопывания пробелов) в
  заголовке или `clean_text`
- **THEN** ответ агента отклоняется как невалидный (`LabelValidationError`),
  а job обрабатывается как ошибочный шаг

### Requirement: Impact-разметка для релевантных KR
Для каждого KR, вошедшего в `relevant_kr_ids`, `label_kr_worker` MUST
выполнить отдельный LLM-вызов (шаг `impact-kr-<id>`), возвращающий `impact`
(`positive`/`negative`/`neutral`), `signal_strength`
(`direct`/`indirect`), `theme`, `dashboard_description`, `why_for_goal`,
`evidence` (каждый фрагмент — grounded в тексте), `reasoning_steps`,
`uncertainty` и `confidence` (число с шагом `0.1` в диапазоне
`[0.5, 1.0]`).

#### Scenario: Impact-ответ с недопустимым confidence
- **WHEN** агент возвращает `confidence`, не кратный `0.1`, или вне
  диапазона `[0.5, 1.0]`
- **THEN** ответ отклоняется как невалидный, и шаг считается ошибочным

#### Scenario: Impact-ответ с невалидным значением impact
- **WHEN** поле `impact` не равно `positive`, `negative` или `neutral`
- **THEN** ответ отклоняется как невалидный

### Requirement: Sber PR-like флаг и entity tonality для significant impact
Если `impact` равен `positive` или `negative`, `label_kr_worker` MUST
последовательно выполнить ещё два шага для того же KR: Sber PR-like флаг
(`sber-paid-kr-<id>`, возвращающий `is_sber_paid_news` строго `0` или `1`) и
entity tonality (`entity-tonality-kr-<id>`, возвращающий список `mentions` с
`text` (grounded в тексте), `sentiment` (`positive`/`negative`/`neutral`),
`justification` и `confidence` в диапазоне `[0.0, 1.0]`). Оба сырых payload'а
сохраняются как `prompt2_payload` и `prompt3_payload` соответственно.

Для KR с `impact='neutral'` эти два шага MUST NOT выполняться.

#### Scenario: Impact neutral не порождает третий и четвёртый шаг
- **WHEN** impact-разметка для KR вернула `impact='neutral'`
- **THEN** Sber PR-like и entity tonality шаги для этого KR не вызываются,
  а `prompt2_payload`/`prompt3_payload` остаются пустыми

#### Scenario: is_sber_paid_news вне допустимых значений
- **WHEN** ответ Sber PR-like флага содержит значение, отличное от `0` или
  `1` (включая булевы значения)
- **THEN** ответ отклоняется как невалидный

### Requirement: Чекпоинты и логирование LLM-вызовов
Каждый LLM-шаг MUST быть независимо чекпоинтирован
(`relevance`, `impact`, `sber_paid_news`, `entity_tonality`) в
`agent_1.label_kr_step_checkpoints` (ключ `clean_item_id, kr_id, step_name`),
так что повторный прогон возобновляется с последнего успешного шага, если не
передан флаг отключения резюме. Каждый вызов `agent_1` MUST логироваться в
`agent_1.llm_call_logs` с длительностью, успехом/ошибкой, размером
prompt/output и любыми полями usage/cost, которые вернул OpenClaw.

#### Scenario: Повторный запуск после сбоя на позднем шаге
- **WHEN** для пары `(clean_item_id, kr_id)` уже есть чекпоинт для шага
  `impact`, но не для `sber_paid_news`
- **THEN** повторный прогон переиспользует сохранённый `impact`-чекпоинт без
  повторного LLM-вызова и выполняет только оставшиеся шаги

#### Scenario: Агент временно недоступен по квоте/rate limit
- **WHEN** вызов `agent_1` завершается ошибкой, распознанной как
  capacity-ошибка (маркеры вида "usage limit", "rate limit", "quota")
- **THEN** job возвращается в статус `pending` для повторной попытки позже,
  а не помечается как окончательно `failed`

### Requirement: Итоговая запись document_kr_labels и переход дальше
После обработки всех релевантных KR `label_kr_worker` MUST полностью
заменить строки `agent_1.document_kr_labels` для данного `clean_item_id`
(удалить старые, вставить новые) и, если получилась хотя бы одна метка,
поставить в очередь ровно один `extract_semantics` job для того же
`clean_item_id`. Если после source-ranking фильтра и relevance gate меток
не осталось вовсе, `extract_semantics` job MUST NOT ставиться, и это
явно фиксируется как `skipped` с причиной
(`source_rankings_filtered_all_krs` или `relevance_filtered_all_krs`) в
`raw_items.source_metadata.label_kr`.

#### Scenario: Хотя бы один KR получил метку
- **WHEN** после relevance gate и impact-разметки получена хотя бы одна
  строка `document_kr_labels`
- **THEN** ставится ровно один `extract_semantics` job для этого
  `clean_item_id`

#### Scenario: Ни один KR не получил метку
- **WHEN** ни source-ranking фильтр, ни relevance gate не оставили ни
  одного KR для impact-разметки
- **THEN** `document_kr_labels` для документа не создаются,
  `extract_semantics` job не ставится, job `label_kr` закрывается как
  `done` со статусом `skipped`
