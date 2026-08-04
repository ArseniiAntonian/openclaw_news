"""Небольшая диагностическая выгрузка agent_1_v5 в xlsx. Только чтение."""
import os
import sys
from pathlib import Path

import psycopg
from openpyxl import Workbook

sys.path.insert(0, "src")
from agent_1 import preprocess_worker as pw  # тот же построчный парсер .env

pw.load_dotenv(Path(".env"))
DSN = os.environ["AGENT_1_DB_DSN"]
OUT = "samples/agent1_outputs_sample_2026-07-28.xlsx"

# Все timestamptz кастуем в text: openpyxl не умеет писать tz-aware datetime.
QUERIES = [
    ("verdicts", """
     SELECT coalesce(drop_reason,'(NULL = оставлен)') AS drop_reason,
     is_duplicate, count(*) AS n
     FROM agent_1_v5.clean_posts GROUP BY 1,2 ORDER BY 3 DESC"""),
    ("volumes", """
     SELECT (SELECT count(*) FROM agent_1_v5.raw_posts) AS raw_posts,
     (SELECT count(*) FROM agent_1_v5.clean_posts) AS clean_posts,
     (SELECT count(*) FROM agent_1_v5.clean_posts
     WHERE drop_reason IS NULL AND NOT is_duplicate) AS kept,
     (SELECT count(*) FROM agent_1_v5.clean_posts
     WHERE embedding IS NOT NULL) AS embedded,
     (SELECT count(*) FROM agent_1_v5.source) AS sources"""),
    ("date_range", """
     SELECT min(time_post)::text AS min_time_post,
     max(time_post)::text AS max_time_post,
     count(*) AS n
     FROM agent_1_v5.clean_posts"""),
    ("by_day", """
     SELECT date_trunc('day', time_post)::date::text AS day, count(*) AS n
     FROM agent_1_v5.clean_posts
     GROUP BY 1 ORDER BY 1"""),
    ("june_window", """
     SELECT date_trunc('day', time_post)::date::text AS day, count(*) AS n
     FROM agent_1_v5.clean_posts
     WHERE time_post >= '2026-06-08' AND time_post < '2026-06-16'
     GROUP BY 1 ORDER BY 1"""),
    ("sources_match", """
     SELECT s.name_source, s.type, count(*) AS posts
     FROM agent_1_v5.clean_posts c
     JOIN agent_1_v5.raw_posts r USING (id_raw_post)
     JOIN agent_1_v5.source s USING (id_source)
     WHERE s.name_source ~* 'dzen|3dnews|1prime|ria|news|habr|cnews|gazeta|spbit'
     GROUP BY 1,2 ORDER BY 3 DESC LIMIT 40"""),
    ("kept_sample", """
     SELECT c.id_clean_post, s.name_source, s.type, c.time_post::text, r.url,
     left(replace(c.clean_content, E'\\n', ' '), 300) AS preview,
     (c.embedding IS NOT NULL) AS has_vec
     FROM agent_1_v5.clean_posts c
     JOIN agent_1_v5.raw_posts r USING (id_raw_post)
     JOIN agent_1_v5.source s USING (id_source)
     WHERE c.drop_reason IS NULL AND NOT c.is_duplicate
     ORDER BY c.id_clean_post DESC LIMIT 200"""),
    ("verdict_samples", """
     SELECT * FROM (
     SELECT drop_reason, id_clean_post, dup_score, id_canonical_post,
     left(replace(coalesce(clean_content,'(нет текста)'), E'\\n',' '), 200) AS preview,
     row_number() OVER (PARTITION BY drop_reason ORDER BY id_clean_post) rn
     FROM agent_1_v5.clean_posts WHERE drop_reason IS NOT NULL
     ) t WHERE rn <= 5 ORDER BY drop_reason, id_clean_post"""),
    ("reprint_group", """
     WITH top AS (SELECT id_canonical_post, count(*) n
     FROM agent_1_v5.clean_posts WHERE is_duplicate
     GROUP BY 1 ORDER BY 2 DESC LIMIT 1)
     SELECT c.id_clean_post, c.is_duplicate, c.dup_score, c.id_canonical_post,
     s.name_source, c.time_post::text,
     left(replace(c.clean_content, E'\\n',' '), 200) AS preview
     FROM agent_1_v5.clean_posts c
     JOIN agent_1_v5.raw_posts r USING (id_raw_post)
     JOIN agent_1_v5.source s USING (id_source)
     JOIN top ON c.id_canonical_post = top.id_canonical_post
     OR c.id_clean_post = top.id_canonical_post
     ORDER BY c.is_duplicate, c.id_clean_post LIMIT 30"""),
    ("source_stats", """
     SELECT name_source, total_raw, processed, pct_junk, pct_non_russian,
     pct_duplicates, avg_content_len, last_seen_at::text
     FROM agent_1_v5.source_stats WHERE processed > 0
     ORDER BY processed DESC LIMIT 60"""),
    ("junk_categories", """
     SELECT category, is_business_guard, is_active,
     jsonb_array_length(patterns) AS n_patterns
     FROM agent_1_v5.junk_categories ORDER BY is_business_guard DESC, category"""),
]

wb = Workbook()
wb.remove(wb.active)
with psycopg.connect(DSN) as conn:
    for sheet, sql in QUERIES:
        with conn.cursor() as cur:
            cur.execute(sql)
            ws = wb.create_sheet(sheet)
            ws.append([d.name for d in cur.description])
            n = 0
            for row in cur:
                ws.append([
                    v if v is None or isinstance(v, (int, float, str, bool)) else str(v)
                    for v in row
                ])
                n += 1
            print(f"{sheet}: {n} строк")
wb.save(OUT)
print("saved", OUT)
