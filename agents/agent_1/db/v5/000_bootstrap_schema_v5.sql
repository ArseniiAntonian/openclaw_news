-- rework-agent-1-v5 / tasks.md 1.1
-- New schema for the v5 pipeline, isolated from the current `agent_1` schema.
-- `agent_1` (raw_items, clean_items, ...) is NOT touched by this migration.

BEGIN;

CREATE SCHEMA IF NOT EXISTS agent_1_v5 AUTHORIZATION postgres;

-- pgvector is database-wide; safe to call even if another schema already
-- enabled it. Needs superuser (or a role with CREATE privilege on the DB).
CREATE EXTENSION IF NOT EXISTS vector;

COMMIT;
