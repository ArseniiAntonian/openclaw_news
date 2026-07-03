BEGIN;

SET search_path TO agent_1, public;

CREATE TABLE raw_items (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    document_type TEXT NOT NULL,
    external_id TEXT,
    url TEXT,
    title TEXT,
    raw_text TEXT,
    raw_payload JSONB,
    source_metadata JSONB,
    published_at TIMESTAMPTZ,
    CHECK (external_id IS NOT NULL OR url IS NOT NULL),
    CHECK (raw_text IS NOT NULL OR raw_payload IS NOT NULL)
);

CREATE UNIQUE INDEX raw_items_source_external_id_uq
ON raw_items (source, external_id)
WHERE external_id IS NOT NULL;

CREATE UNIQUE INDEX raw_items_source_url_uq
ON raw_items (source, url)
WHERE external_id IS NULL AND url IS NOT NULL;


CREATE TABLE processing_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'failed'))
);

CREATE INDEX processing_jobs_lookup_idx
ON processing_jobs (job_type, status, id);

CREATE UNIQUE INDEX processing_jobs_active_uq
ON processing_jobs (job_type, entity_type, entity_id)
WHERE status IN ('pending', 'processing');


CREATE TABLE clean_items (
    id BIGSERIAL PRIMARY KEY,
    raw_item_id BIGINT NOT NULL UNIQUE REFERENCES raw_items(id) ON DELETE CASCADE,
    clean_title TEXT,
    clean_text TEXT NOT NULL CHECK (btrim(clean_text) <> ''),
    language TEXT
);


CREATE TABLE key_results (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    enrichment JSONB CHECK (enrichment IS NULL OR jsonb_typeof(enrichment) = 'object'),
    enriched_at TIMESTAMPTZ,
    enriched_by TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE
);


CREATE TABLE document_kr_labels (
    clean_item_id BIGINT NOT NULL REFERENCES clean_items(id) ON DELETE CASCADE,
    kr_id BIGINT NOT NULL REFERENCES key_results(id) ON DELETE CASCADE,
    impact TEXT NOT NULL CHECK (impact IN ('positive', 'negative', 'neutral')),
    signal_strength TEXT NOT NULL CHECK (signal_strength IN ('direct', 'indirect')),
    theme TEXT NOT NULL CHECK (btrim(theme) <> ''),
    dashboard_description TEXT NOT NULL CHECK (btrim(dashboard_description) <> ''),
    why_for_goal TEXT NOT NULL CHECK (btrim(why_for_goal) <> ''),
    evidence TEXT[] NOT NULL CHECK (cardinality(evidence) > 0),
    reasoning_steps JSONB NOT NULL CHECK (jsonb_typeof(reasoning_steps) = 'array'),
    uncertainty TEXT NOT NULL CHECK (btrim(uncertainty) <> ''),
    confidence NUMERIC(2,1) NOT NULL CHECK (
        confidence IN (0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
    ),
    is_sber_paid_news SMALLINT CHECK (is_sber_paid_news IN (0, 1)),
    prompt1_payload JSONB NOT NULL CHECK (jsonb_typeof(prompt1_payload) = 'object'),
    prompt2_payload JSONB CHECK (
        prompt2_payload IS NULL OR jsonb_typeof(prompt2_payload) = 'object'
    ),
    prompt3_payload JSONB CHECK (
        prompt3_payload IS NULL OR jsonb_typeof(prompt3_payload) = 'object'
    ),
    PRIMARY KEY (clean_item_id, kr_id)
);

CREATE INDEX document_kr_labels_kr_id_idx
ON document_kr_labels (kr_id);


CREATE TABLE label_kr_step_checkpoints (
    clean_item_id BIGINT NOT NULL REFERENCES clean_items(id) ON DELETE CASCADE,
    kr_id BIGINT NOT NULL REFERENCES key_results(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL CHECK (
        step_name IN ('impact', 'sber_paid_news', 'entity_tonality')
    ),
    status TEXT NOT NULL DEFAULT 'done' CHECK (status = 'done'),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    raw_output TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clean_item_id, kr_id, step_name)
);

CREATE INDEX label_kr_step_checkpoints_lookup_idx
ON label_kr_step_checkpoints (clean_item_id, kr_id, step_name, status);


CREATE TABLE llm_call_logs (
    id BIGSERIAL PRIMARY KEY,
    worker TEXT NOT NULL,
    clean_item_id BIGINT REFERENCES clean_items(id) ON DELETE SET NULL,
    kr_id BIGINT REFERENCES key_results(id) ON DELETE SET NULL,
    step_name TEXT NOT NULL,
    job_id BIGINT,
    model TEXT,
    session_key TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    success BOOLEAN NOT NULL,
    prompt_chars INTEGER NOT NULL CHECK (prompt_chars >= 0),
    output_chars INTEGER CHECK (output_chars IS NULL OR output_chars >= 0),
    usage JSONB CHECK (usage IS NULL OR jsonb_typeof(usage) = 'object'),
    error TEXT
);

CREATE INDEX llm_call_logs_lookup_idx
ON llm_call_logs (worker, clean_item_id, kr_id, step_name, started_at);


CREATE TABLE corporate_abbreviations (
    short TEXT PRIMARY KEY,
    expanded TEXT NOT NULL
);


CREATE TABLE document_enrichments (
    clean_item_id BIGINT PRIMARY KEY REFERENCES clean_items(id) ON DELETE CASCADE,
    entities JSONB,
    events JSONB,
    abbreviations JSONB,
    CHECK (entities IS NULL OR jsonb_typeof(entities) = 'object'),
    CHECK (events IS NULL OR jsonb_typeof(events) = 'array'),
    CHECK (abbreviations IS NULL OR jsonb_typeof(abbreviations) = 'array')
);


CREATE TABLE review_trigrams (
    clean_item_id BIGINT PRIMARY KEY REFERENCES clean_items(id) ON DELETE CASCADE,
    trigrams JSONB NOT NULL,
    CHECK (jsonb_typeof(trigrams) = 'array')
);

COMMIT;
