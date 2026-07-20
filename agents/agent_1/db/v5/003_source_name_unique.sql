-- rework-agent-1-v5 / tasks.md 1.3
-- A source identified by name should be unique by name — needed so the data
-- migration script (and, later, real ingestion) can upsert agent_1_v5.source
-- by name_source idempotently instead of guessing via a SELECT-then-INSERT
-- race.

BEGIN;

SET search_path TO agent_1_v5, public;

ALTER TABLE source
ADD CONSTRAINT source_name_source_uq UNIQUE (name_source);

COMMIT;
