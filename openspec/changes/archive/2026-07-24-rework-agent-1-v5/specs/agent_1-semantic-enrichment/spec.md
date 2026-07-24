## REMOVED Requirements

### Requirement: Триггер semantic-enrichment и claim job'ов
**Reason**: Semantic-enrichment полностью уходит из Агента 1 (архитектура
v5) — извлечение сущностей и событий выполняет Агент 4.
**Migration**: Очереди — Агент 6; извлечение — спеки Агента 4.

### Requirement: Извлечение сущностей
**Reason**: В v5 сущности извлекает Агент 4: GLiNER даёт спаны
(`gliner_entities`), нормализация — через `entity_canonical`.
**Migration**: Спека Агента 4 при его change; groundedness-принцип
(значения дословно из текста) наследуется там by design (GLiNER работает
спанами).

### Requirement: Извлечение событий
**Reason**: В v5 события — часть драйверного слоя Агента 4
(`driver_instance`/`driver_mention`, сборка тройки `event|actor|object` —
отдельный шаг связывания, не Агент 1 и не GLiNER).
**Migration**: Спека Агента 4.

### Requirement: Контекст KR-меток в промптах extraction
**Reason**: KR-метки Агентом 1 больше не создаются; контекст КР в v5
приходит из `kr_cluster_match` (Агент 3).
**Migration**: Спеки Агентов 3/4.

### Requirement: Чекпоинты и логирование
**Reason**: У Агента 1 не остаётся LLM-вызовов.
**Migration**: Аналогично `agent_1-kr-labeling` — забота Агентов 2/4/5/6.

### Requirement: Сохранение результата и границы ответственности
**Reason**: **BREAKING** — таблица `document_enrichments` Агентом 1 больше
не наполняется.
**Migration**: Результаты анализа в v5 живут в `gliner_entities`,
`driver_instance`, `driver_mention` (Агент 4); исторические
`document_enrichments` остаются read-only.
