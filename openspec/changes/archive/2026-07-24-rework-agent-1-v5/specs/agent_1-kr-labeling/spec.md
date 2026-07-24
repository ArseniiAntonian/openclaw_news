## REMOVED Requirements

### Requirement: Триггер kr-labeling и claim job'ов
**Reason**: KR-labeling полностью уходит из Агента 1 (архитектура v5) —
оценка релевантности выполняется после кластеризации.
**Migration**: Механика очередей — зона Агента 6 (`processing_jobs`,
`post_kr_processing`); разметка — зона Агента 4.

### Requirement: Source-ranking фильтр по enrichment agent_2
**Reason**: Уходит из Агента 1 вместе с этапом kr-labeling.
**Migration**: Важность источников — колонка `source.importance` (Агент 2
её заполняет); применение фильтра — в спеках Агента 4.

### Requirement: Relevance gate одним multi-KR вызовом
**Reason**: Релевантность документ↔КР в v5 определяется через
кластеризацию (Агент 3: `kr_cluster_match`) и анализ (Агент 4), а не
per-document LLM-вызовом Агента 1.
**Migration**: Спеки Агентов 3/4 при их changes; существующая логика
`label_kr_worker.py` — референс.

### Requirement: Impact-разметка для релевантных KR
**Reason**: Уходит в Агент 4 (драйверный слой: `driver_mention` с
direction/magnitude/horizon/confidence).
**Migration**: Спека Агента 4; silver-датасет существующей разметки
сохраняется для будущей дистилляции.

### Requirement: Sber PR-like флаг и entity tonality для significant impact
**Reason**: Уходит в Агент 4. Известный баг (document-level шаги вызываются
per-KR с идентичным входом) чинится там кэшированием по документу.
**Migration**: Спека Агента 4.

### Requirement: Чекпоинты и логирование LLM-вызовов
**Reason**: У Агента 1 не остаётся LLM-вызовов — чекпоинтить и логировать
нечего.
**Migration**: Логирование LLM-вызовов — сквозная забота Агентов 2/4/5 и
оркестратора (Агент 6).

### Requirement: Итоговая запись document_kr_labels и переход дальше
**Reason**: **BREAKING** — таблица `document_kr_labels` Агентом 1 больше не
наполняется; чейнинг этапов делает оркестратор.
**Migration**: Связь пост×КР в v5 — `post_kr_processing` (Агент 6) и
`kr_cluster_match` (Агент 3); исторические данные `document_kr_labels`
остаются read-only как silver-датасет.
