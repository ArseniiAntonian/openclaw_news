-- rework-agent-1-v5 / tasks.md 5.1
-- Per-source quality statistics (Требование 6 / capability agent_1-source-stats).
-- A plain VIEW: always fresh, no scheduling, cheap at 57k raws / ~2k sources.
-- Materialize later only if it gets slow.
--
-- Percentages are over PROCESSED docs (raws that already have a clean_posts
-- verdict), not over total_raw -- otherwise the large unprocessed backlog
-- (55767 raws with no clean_posts yet) would drag every rate toward 0 until the
-- backfill finishes. total_raw and processed are both exposed so the numbers
-- stay interpretable while the backfill is in progress.

BEGIN;

CREATE OR REPLACE VIEW agent_1_v5.source_stats AS
SELECT
    s.id_source,
    s.name_source,
    COUNT(r.id_raw_post)                                   AS total_raw,
    COUNT(c.id_clean_post)                                 AS processed,
    ROUND(100.0 * COUNT(c.id_clean_post)
          FILTER (WHERE c.drop_reason LIKE 'junk:%')
          / NULLIF(COUNT(c.id_clean_post), 0), 2)          AS pct_junk,
    ROUND(100.0 * COUNT(c.id_clean_post)
          FILTER (WHERE c.drop_reason = 'non_russian')
          / NULLIF(COUNT(c.id_clean_post), 0), 2)          AS pct_non_russian,
    ROUND(100.0 * COUNT(c.id_clean_post)
          FILTER (WHERE c.is_duplicate)
          / NULLIF(COUNT(c.id_clean_post), 0), 2)          AS pct_duplicates,
    ROUND(AVG(length(r.content))::numeric, 0)              AS avg_content_len,
    MAX(r.collected_at)                                    AS last_seen_at
FROM agent_1_v5.source s
LEFT JOIN agent_1_v5.raw_posts   r ON r.id_source   = s.id_source
LEFT JOIN agent_1_v5.clean_posts c ON c.id_raw_post = r.id_raw_post
GROUP BY s.id_source, s.name_source;

COMMENT ON VIEW agent_1_v5.source_stats IS
  'rework-agent-1-v5 5.1: per-source quality stats (total_raw, processed, '
  'pct_junk/non_russian/duplicates over processed, avg_content_len, '
  'last_seen_at). Seed for agent_memory source prioritization.';

COMMIT;
