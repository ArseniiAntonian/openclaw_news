BEGIN;

CREATE SCHEMA IF NOT EXISTS demo;

CREATE TABLE IF NOT EXISTS demo.raw_items (
    id BIGSERIAL PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_payload JSONB,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS demo_raw_items_url_created_at_uq
ON demo.raw_items (url, created_at);

CREATE INDEX IF NOT EXISTS demo_raw_items_created_at_idx
ON demo.raw_items (created_at);

CREATE INDEX IF NOT EXISTS demo_raw_items_source_idx
ON demo.raw_items (source);

CREATE TABLE IF NOT EXISTS demo.clean_items (
    id BIGSERIAL PRIMARY KEY,
    raw_id BIGINT NOT NULL UNIQUE REFERENCES demo.raw_items(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    title TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL,
    content TEXT NOT NULL,
    is_duplicate BOOLEAN NOT NULL DEFAULT FALSE,
    dup_of BIGINT REFERENCES demo.clean_items(id) ON DELETE SET NULL,
    duplicate_kind TEXT CHECK (duplicate_kind IN ('exact', 'near') OR duplicate_kind IS NULL),
    duplicate_similarity DOUBLE PRECISION,
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS demo_clean_items_live_idx
ON demo.clean_items (is_duplicate, created_at);

CREATE INDEX IF NOT EXISTS demo_clean_items_source_idx
ON demo.clean_items (source);

CREATE TABLE IF NOT EXISTS demo.kr (
    id BIGSERIAL PRIMARY KEY,
    text TEXT NOT NULL,
    enrichment JSONB NOT NULL CHECK (jsonb_typeof(enrichment) = 'object'),
    enriched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS demo.doc_labels (
    id BIGSERIAL PRIMARY KEY,
    clean_item_id BIGINT NOT NULL REFERENCES demo.clean_items(id) ON DELETE CASCADE,
    kr_id BIGINT NOT NULL REFERENCES demo.kr(id) ON DELETE CASCADE,
    impact TEXT NOT NULL CHECK (impact IN ('positive', 'negative', 'neutral')),
    sber_paid_news SMALLINT CHECK (sber_paid_news IN (0, 1) OR sber_paid_news IS NULL),
    entity_tonality JSONB CHECK (entity_tonality IS NULL OR jsonb_typeof(entity_tonality) = 'object'),
    raw_json JSONB NOT NULL CHECK (jsonb_typeof(raw_json) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (clean_item_id, kr_id)
);

COMMIT;
