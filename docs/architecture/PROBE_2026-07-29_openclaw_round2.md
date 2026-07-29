# Промпт для OpenClaw: раунд 2 — качество текста, необработанный срез, сущности

**Дата:** 2026-07-29
**Предыдущий раунд:** `PROBE_2026-07-29_openclaw_blockers.md` (выполнен, отчёт получен)
**Режим:** только чтение. Ни одного `INSERT`/`UPDATE`/`DELETE`/`CREATE`/`ALTER`.

Всё ниже до конца файла — текст, который отдаётся OpenClaw как есть.

---

## ЗАДАЧА

Первый раунд закрыл вопрос про даты: окна 2026-06-09…13 в корпусе нет,
answer key банка по датам неприменим. Этот раунд отвечает на три новых
вопроса, возникших из тех же цифр.

**Вопрос 1.** `raw_posts` = 84686 строк до 23 июля, `clean_posts` = 57000
и обрывается 30 июня. Похоже, весь июльский срез (~27686 строк) не прошёл
препроцессинг и не имеет эмбеддингов. Подтвердить и измерить точно.

**Вопрос 2.** Заполнен ли `clean_posts.clean_content` у живых документов.
Если он NULL, то все подсчёты попаданий по регексам в раунде 1 шли только
по заголовкам и занижены. От этого зависит вывод «корпуса хватит на свою
разметку».

**Вопрос 3.** Что лежит в `metadata->'companies'` и `metadata->'summary'`.
Это потенциально готовые сущности от вендора и готовый короткий текст под
эмбеддинг — оба относятся к дизайну ретрива напрямую.

### Ограничения

- Только `SELECT`. Никаких DDL/DML. Ничего не запускать, не чинить,
  не досоздавать — даже если увидишь, что «надо».
- Не трогать `agent_1.processing_jobs`.
- Не запускать `parsers360_ingest`, `preprocess_v5`, `embed_v5` — ни в
  каком виде, ни с какими флагами. Этот раунд ничего не выполняет.
- DSN — `AGENT_1_DB_DSN` из `/root/.openclaw/workspace/agents/agent_1/.env`
  (БД `mvp_db`). `SET statement_timeout = '120s';`
- «Живой» документ = `drop_reason IS NULL AND is_duplicate = FALSE`.

---

## БЛОК E — необработанный срез

### E1. Помесячно: сырьё против обработанного

```sql
SELECT date_trunc('month', r.time_post)::date AS month,
       count(*) AS raw_rows,
       count(c.id_clean_post) AS has_clean_row,
       count(*) - count(c.id_clean_post) AS pending
FROM agent_1_v5.raw_posts r
LEFT JOIN agent_1_v5.clean_posts c ON c.id_raw_post = r.id_raw_post
GROUP BY 1
ORDER BY 1;
```

### E2. То же по дням, только для непрошедших препроцессинг

```sql
SELECT date_trunc('day', r.time_post)::date AS d, count(*) AS pending
FROM agent_1_v5.raw_posts r
LEFT JOIN agent_1_v5.clean_posts c ON c.id_raw_post = r.id_raw_post
WHERE c.id_clean_post IS NULL
GROUP BY 1
ORDER BY 1;
```

### E3. Состав необработанного среза по доменам (топ-25)

```sql
SELECT lower(substring(r.url from '^(?:https?://)?(?:www\.)?([^/?#]+)')) AS host,
       count(*) AS pending
FROM agent_1_v5.raw_posts r
LEFT JOIN agent_1_v5.clean_posts c ON c.id_raw_post = r.id_raw_post
WHERE c.id_clean_post IS NULL
GROUP BY 1
ORDER BY 2 DESC
LIMIT 25;
```

### E4. Доля Telegram в уже обработанном (для сравнения с E3)

```sql
SELECT lower(substring(r.url from '^(?:https?://)?(?:www\.)?([^/?#]+)')) AS host,
       count(*) AS kept
FROM agent_1_v5.clean_posts c
JOIN agent_1_v5.raw_posts r ON r.id_raw_post = c.id_raw_post
WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
GROUP BY 1
ORDER BY 2 DESC
LIMIT 25;
```

---

## БЛОК F — заполненность текста (проверка достоверности раунда 1)

### F1. NULL-rate и длина текста

```sql
SELECT count(*) AS kept,
       count(clean_content) AS content_not_null,
       count(*) FILTER (WHERE clean_content IS NULL OR length(btrim(clean_content)) = 0) AS content_empty,
       min(length(clean_content)) AS min_len,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY length(clean_content)) AS median_len,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY length(clean_content)) AS p95_len,
       max(length(clean_content)) AS max_len
FROM agent_1_v5.clean_posts
WHERE drop_reason IS NULL AND is_duplicate = FALSE;
```

### F2. Раздельный подсчёт: заголовок против тела

Проверяем прямо: даёт ли поиск по телу больше, чем по заголовку.

```sql
WITH d AS (
  SELECT coalesce(r.title, '') AS ttl, coalesce(c.clean_content, '') AS body
  FROM agent_1_v5.clean_posts c
  JOIN agent_1_v5.raw_posts r ON r.id_raw_post = c.id_raw_post
  WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
)
SELECT count(*) FILTER (WHERE ttl ~* 'openai|chatgpt|anthropic|claude')  AS title_only_hits,
       count(*) FILTER (WHERE body ~* 'openai|chatgpt|anthropic|claude') AS body_hits,
       count(*) FILTER (WHERE (ttl || ' ' || body) ~* 'openai|chatgpt|anthropic|claude') AS combined_hits
FROM d;
```

### F3. Декомпозиция подозрительного результата раунда 1

В раунде 1 объект 4 дал 98 попаданий, а более узкий паттерн
`anthropic|claude` — 96. Надмножество почти равно подмножеству. Проверяем
каждый терм отдельно.

```sql
WITH d AS (
  SELECT coalesce(r.title,'') || ' ' || coalesce(c.clean_content,'') AS txt
  FROM agent_1_v5.clean_posts c
  JOIN agent_1_v5.raw_posts r ON r.id_raw_post = c.id_raw_post
  WHERE c.drop_reason IS NULL AND c.is_duplicate = FALSE
),
term(label, rx) AS (VALUES
  ('anthropic',        'anthropic'),
  ('claude',           'claude'),
  ('openai',           'openai'),
  ('chatgpt',          'chatgpt'),
  ('чатгпт/чат-гпт',   'чат-?гпт|чат-?джипити'),
  ('gpt-4/5',          '\mgpt-?[45]\M'),
  ('sora',             '\msora\M'),
  ('нейросет*',        'нейросет\w*'),
  ('искусственн* инт', 'искусственн\w+ интеллект\w*'),
  ('\mии\M',           '\mии\M')
)
SELECT term.label, count(*) FILTER (WHERE d.txt ~* term.rx) AS hits
FROM term CROSS JOIN d
GROUP BY term.label
ORDER BY 2 DESC;
```

---

## БЛОК G — вендорские сущности и summary

### G1. Форма `companies`

```sql
SELECT jsonb_typeof(metadata->'companies') AS typ, count(*) AS n
FROM agent_1_v5.raw_posts
GROUP BY 1
ORDER BY 2 DESC;
```

```sql
SELECT id_raw_post, left((metadata->'companies')::text, 400) AS companies
FROM agent_1_v5.raw_posts
WHERE metadata->'companies' IS NOT NULL
  AND (metadata->'companies')::text NOT IN ('null', '[]', '""')
ORDER BY id_raw_post DESC
LIMIT 10;
```

### G2. Насколько `companies` вообще заполнен

```sql
SELECT count(*) AS n,
       count(*) FILTER (WHERE (metadata->'companies')::text IN ('null','[]','""')
                           OR metadata->'companies' IS NULL) AS empty_companies,
       count(*) FILTER (WHERE (metadata->'summary')::text IN ('null','""')
                           OR metadata->'summary' IS NULL) AS empty_summary
FROM agent_1_v5.raw_posts;
```

### G3. Топ сущностей — если `companies` это массив

Если G1 показал `array`, выполни. Если другой тип — пропусти и напиши,
какой тип, не подгоняя запрос.

```sql
SELECT lower(x::text) AS company, count(*) AS n
FROM agent_1_v5.raw_posts, LATERAL jsonb_array_elements(metadata->'companies') AS x
WHERE jsonb_typeof(metadata->'companies') = 'array'
GROUP BY 1
ORDER BY 2 DESC
LIMIT 40;
```

### G4. Длина `summary` против длины тела

```sql
SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY length(metadata->>'summary')) AS median_summary_len,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY length(metadata->>'summary')) AS p95_summary_len,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY length(content)) AS median_content_len
FROM agent_1_v5.raw_posts
WHERE metadata->>'summary' IS NOT NULL;
```

---

## БЛОК H — форма URL у целевых доменов

Нужно понять, матчились бы ссылки answer key по точному URL, если бы
документы за нужные даты в корпусе были. В раунде 1 B4 дал 0 строк, но
это объясняется отсутствием документов за 9–13 июня, а не формой ссылок —
вопрос остался открытым.

```sql
SELECT lower(substring(url from '^(?:https?://)?(?:www\.)?([^/?#]+)')) AS host,
       url, time_post
FROM agent_1_v5.raw_posts
WHERE url ~* '(^|//|\.)(3dnews\.ru|1prime\.ru|ria\.ru|news\.ru|cnews\.ru|habr\.com)/'
ORDER BY host, time_post DESC
LIMIT 30;
```

---

## ЧТО ПРИСЛАТЬ В ОТВЕТ

Markdown-отчёт: идентификатор запроса (E1, E2, …) и вывод как есть.

В конце — четыре вердикта одним предложением каждый:

1. **Необработанный срез:** сколько строк, за какие даты, какие домены.
2. **`clean_content`:** заполнен / пуст; были ли подсчёты раунда 1
   фактически поиском по заголовкам (сравнение F2).
3. **`companies` / `summary`:** пригодны как сигнал / пусты / нужна
   отдельная проверка.
4. **Форма URL:** матч по точному URL был бы возможен / нет.

Если запрос упал — приложи текст ошибки, не переписывай его на глаз более
одного раза. Ничего не запускать и не менять.