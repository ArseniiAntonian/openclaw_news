## ADDED Requirements

### Requirement: Агрегируемая статистика качества по источникам
По каждому `ID_source` MUST агрегироваться (SQL-view или таблица с
обновлением по расписанию): `total_raw`, `pct_junk`, `pct_non_russian`,
`pct_duplicates`, `avg_content_len`, `last_seen_at`. Статистика
вычисляется из `raw_posts` и вердиктов `clean_posts`
(`drop_reason`/`is_duplicate`) без отдельных лог-таблиц.

Это задел под будущую `agent_memory` (приоритизация источников); сама
таблица памяти агентов — вне скоупа.

#### Scenario: Источник с высокой долей мусора
- **WHEN** по источнику накоплены обработанные документы
- **THEN** запрос статистики возвращает его `pct_junk`,
  `pct_non_russian`, `pct_duplicates` и `avg_content_len` одним запросом

#### Scenario: Свежесть источника
- **WHEN** источник давно не отдавал документов
- **THEN** `last_seen_at` отражает время последнего полученного raw-поста
