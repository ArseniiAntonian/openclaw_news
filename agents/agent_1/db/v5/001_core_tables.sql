-- rework-agent-1-v5 / tasks.md 1.1
-- Core v5 tables owned by Agent 1: source, raw_posts, clean_posts.
-- See openspec/changes/rework-agent-1-v5/specs/agent_1-ingestion/spec.md
-- and .../agent_1-preprocessing/spec.md for the requirements this implements.

BEGIN;

SET search_path TO agent_1_v5, public;

-- ---------------------------------------------------------------------------
-- source
-- ---------------------------------------------------------------------------

CREATE TABLE source (
    id_source BIGSERIAL PRIMARY KEY,
    name_source TEXT NOT NULL,
    url_source TEXT,
    type VARCHAR NOT NULL,
    importance DOUBLE PRECISION NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- ---------------------------------------------------------------------------
-- raw_posts — exactly what the parser returned, atomic fields, append-only.
-- Composite PK includes time_post as a partitioning seed (PARTITION BY RANGE
-- (time_post) is a future, separate change; not enabled here).
-- ---------------------------------------------------------------------------

CREATE TABLE raw_posts (
    id_raw_post BIGSERIAL NOT NULL,
    id_source BIGINT NOT NULL REFERENCES source (id_source),
    parser VARCHAR NOT NULL,
    title TEXT,
    url TEXT NOT NULL,
    content TEXT NOT NULL,
    time_post TIMESTAMPTZ NOT NULL,
    lang VARCHAR,
    metadata JSONB,
    collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_raw_post, time_post),
    -- lets clean_posts FK to id_raw_post alone before partitioning lands
    UNIQUE (id_raw_post)
);

CREATE INDEX raw_posts_source_idx ON raw_posts (id_source, time_post);

-- LZ4 TOAST compression for the large text column (Postgres >= 14).
ALTER TABLE raw_posts ALTER COLUMN content SET COMPRESSION lz4;

-- ---------------------------------------------------------------------------
-- clean_posts — cleanup verdict for every processed raw_posts row.
-- Invariant: clean_posts = вердикт очистки по каждому обработанному raw.
-- drop_reason IS NULL AND is_duplicate = false  =>  kept, eligible for embedding.
-- time_post is denormalized from raw_posts so this table can be partitioned
-- the same way once partitioning is enabled.
-- ---------------------------------------------------------------------------

CREATE TABLE clean_posts (
    id_clean_post BIGSERIAL NOT NULL,
    id_raw_post BIGINT NOT NULL UNIQUE REFERENCES raw_posts (id_raw_post),
    id_canonical_post BIGINT,
    id_cluster BIGINT,  -- nullable FK, written by Agent 3 (agent_1_v5 does not own the cluster table)
    time_post TIMESTAMPTZ NOT NULL,
    clean_content TEXT,
    content_hash TEXT,
    drop_reason TEXT,
    is_duplicate BOOLEAN NOT NULL DEFAULT FALSE,
    dup_score DOUBLE PRECISION,
    embedding VECTOR(1024),
    cleaned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (id_clean_post, time_post),
    UNIQUE (id_clean_post),
    CONSTRAINT clean_posts_canonical_fk
        FOREIGN KEY (id_canonical_post) REFERENCES clean_posts (id_clean_post)
);

ALTER TABLE clean_posts ALTER COLUMN clean_content SET COMPRESSION lz4;

-- Exact dedup: unique among "kept, non-duplicate" rows only. Rows that are
-- filtered out or marked as duplicates are excluded from the predicate, so
-- their content_hash may repeat without violating uniqueness.
CREATE UNIQUE INDEX clean_posts_content_hash_uq
ON clean_posts (content_hash)
WHERE drop_reason IS NULL AND is_duplicate = FALSE;

CREATE INDEX clean_posts_cluster_idx ON clean_posts (id_cluster);
CREATE INDEX clean_posts_canonical_idx ON clean_posts (id_canonical_post);

-- HNSW is deliberately NOT created here (tasks.md 4.2): build it after the
-- initial bulk embedding load, not before.

COMMIT;
