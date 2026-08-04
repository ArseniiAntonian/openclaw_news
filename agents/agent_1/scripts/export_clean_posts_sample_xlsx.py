"""Read-only XLSX sample of agent_1_v5.clean_posts."""
import os
import sys
from pathlib import Path

import psycopg
from openpyxl import Workbook

sys.path.insert(0, "src")
from agent_1 import preprocess_worker as pw

pw.load_dotenv(Path(".env"))
OUT = Path("samples/agent1_v5_clean_posts_sample_1000.xlsx")

SQL = """
SELECT id_clean_post, id_raw_post, id_canonical_post, id_cluster,
       time_post::text AS time_post,
       left(clean_content, 30000) AS clean_content,
       content_hash, drop_reason, is_duplicate, dup_score,
       (embedding IS NOT NULL) AS has_embedding,
       cleaned_at::text AS cleaned_at
FROM agent_1_v5.clean_posts
ORDER BY random()
LIMIT 1000
"""

wb = Workbook()
ws = wb.active
ws.title = "clean_posts_sample_1000"
with psycopg.connect(os.environ["AGENT_1_DB_DSN"]) as conn:
    with conn.cursor() as cur:
        cur.execute(SQL)
        ws.append([d.name for d in cur.description])
        for row in cur:
            ws.append(list(row))

wb.save(OUT)
print(f"saved {OUT}: {ws.max_row - 1} rows")
