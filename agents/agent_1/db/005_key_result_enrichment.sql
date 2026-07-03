BEGIN;

ALTER TABLE agent_1.key_results
ADD COLUMN IF NOT EXISTS enrichment JSONB CHECK (
    enrichment IS NULL OR jsonb_typeof(enrichment) = 'object'
),
ADD COLUMN IF NOT EXISTS enriched_at TIMESTAMPTZ,
ADD COLUMN IF NOT EXISTS enriched_by TEXT;

COMMIT;
