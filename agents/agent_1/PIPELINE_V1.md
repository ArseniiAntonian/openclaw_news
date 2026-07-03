# agent_1 Pipeline v1

## Purpose

`agent_1` is not the whole pipeline.

The pipeline is worker-driven, and the agent is called only for semantic tasks
that genuinely need model judgment.

Overall system flow:

1. Python workers react to new rows in `raw_items`
2. Python workers preprocess and deduplicate documents
3. `agent_1` labels documents against key results with binary `0/1`
4. `agent_1` extracts entities and events
5. Python workers expand abbreviations and compute review trigram statistics

The pipeline is intentionally small. It avoids audit-heavy schemas, version
fields, and large numbers of mostly empty columns.

## Responsibility split

### Done by `agent_1`

- KR labeling against active key results
- entity extraction
- event extraction
- structured JSON output only

### Done by Python workers

- job creation and claiming
- text extraction from `raw_payload`
- html cleanup and normalization
- language detection
- exact dedup by normalized text equality
- MinHash reprint filtering
- all database reads and writes
- output validation
- abbreviation detection
- abbreviation expansion from a corporate dictionary
- trigram statistics for reviews
- reruns, backfills, and repair jobs

Rule of thumb:

- if the task is deterministic and can be implemented reliably in Python, it
  must stay out of the agent
- if the task requires semantic judgment over text, it may be delegated to the
  agent

## Pipeline

### Stage 1: Raw arrival

New documents are inserted into `raw_items`.

Each row must have:

- `source`
- `document_type`
- one of `external_id` or `url`
- one of `raw_text` or `raw_payload`

Optional but useful:

- `title`
- `published_at`
- `source_metadata`

### Stage 2: Preprocess trigger

Every new `raw_items.id` creates one `processing_jobs` row with:

- `job_type = 'preprocess'`
- `entity_type = 'raw_item'`
- `entity_id = raw_items.id`
- `status = 'pending'`

Workers claim jobs with `FOR UPDATE SKIP LOCKED`.

### Stage 3: Preprocessing

The preprocessing worker does:

- text extraction from payload when needed
- html cleanup
- unicode normalization
- whitespace normalization
- light boilerplate cleanup
- language detection
- conservative regex junk-topic filtering for obvious human-interest noise
- filtering of non-Russian documents before `clean_items`
- exact duplicate removal in Python by normalized text equality
- MinHash reprint filtering for news documents

If a document is an exact duplicate or a MinHash-LSH candidate whose exact
text-shingle Jaccard similarity is at least `0.7`, the current raw row is marked
in `source_metadata.preprocess`, no `clean_items` row is created, and the job is
marked `done`.

If a document contains enough alphabetic text to classify but is not Russian by
script balance, the current raw row is marked in `source_metadata.preprocess`
with `status='filtered_out'` and `reason='non_russian_text'`, no `clean_items`
row is created, and the job is marked `done`.

If a news document matches the conservative junk-topic regex layer and does not
contain protected banking / Sber / business-product context, the current raw row
is marked in `source_metadata.preprocess` with `status='filtered_out'` and
`reason='junk_topic_regex'`, no `clean_items` row is created, and the job is
marked `done`.

If the document survives preprocessing, one row is written to `clean_items`.

### Stage 4: KR labeling trigger

Every new `clean_items.id` creates one `processing_jobs` row with:

- `job_type = 'label_kr'`
- `entity_type = 'clean_item'`
- `entity_id = clean_items.id`
- `status = 'pending'`

### Stage 5: Agent KR labeling

The labeling worker first runs one document-level relevance gate over the
source-allowed active key results and keeps only the KR candidates that the
document actually relates to.

Only the matched KR candidates then go into the per-KR impact labeling flow.
Unrelated KR pairs are skipped and do not receive `neutral` labels.

Before invoking the agent for a concrete KR, the worker checks
`key_results.enrichment`. Agent 2 owns that enriched KR JSON and writes `тема`,
`ключевые_слова`, and ranked `типы_источников` together. Source types with
`важность = 3` or `2` are included for labeling; `1` is excluded. If a KR has no
enrichment yet, labeling is fail-open. If a KR has ranked source types but the
document does not expose a matching explicit `source_type`, that KR is skipped
without an LLM call.

The worker stores every KR impact label:

- `positive`
- `negative`
- `neutral`

For source-allowed KRs, the worker first asks the agent which KR are actually
related to the document. This relevance gate is a single multi-KR call per
document so the pipeline does not add a separate binary LLM call for every KR.

For `positive` and `negative` impact labels, the worker then runs the Sber
PR-like flag prompt and the third prompt in sequence, storing both raw JSON
payloads.

Every LLM step is checkpointed independently so retries can resume from the last
successful step. Every LLM call is logged with timing, success/error,
prompt/output size, and any usage/cost metadata exposed by OpenClaw.

### Stage 6: Agent semantic enrichment trigger

Every labeled `clean_items.id` creates one `processing_jobs` row with:

- `job_type = 'extract_semantics'`
- `entity_type = 'clean_item'`
- `entity_id = clean_items.id`
- `status = 'pending'`

### Stage 7: Agent semantic enrichment

This stage has exactly two outputs:

1. entity extraction
2. event extraction

Execution rules:

- `entities` are extracted for every labeled clean document
- `events` are extracted for every labeled clean document

Recommended order inside the worker:

1. call the agent for entities
2. call the agent for events
3. validate groundedness
4. store the result in `document_enrichments`

The agent does not compute trigrams, does not resolve abbreviations from a
dictionary, and does not touch queue state or persistence directly.

#### 7.1 Entity extraction

Goal:

- produce compact document-level entities grounded in the text

Minimum output groups:

- `companies`
- `products`
- `people`
- `locations`
- `technologies`

Each extracted value must appear in the document text directly or as an obvious
surface form.

#### 7.2 Event extraction

Goal:

- identify explicit events or changes stated in the document

Store only document-grounded events. Do not infer strategy, impact, or hidden
causal chains that are not supported by the text.

Minimum event fields:

- `event_type`
- `summary`
- `evidence`

Optional but useful:

- `participants`
- `event_time`

### Stage 8: Python deterministic enrichment

This stage has exactly two outputs:

1. abbreviation detection and expansion
2. syntax statistics for reviews via trigrams

#### 8.1 Abbreviation expansion

Goal:

- detect abbreviations used in the document
- resolve them when the corporate dictionary contains a mapping

Operational rule:

- run by a Python worker, not by the agent
- if a mapping exists, store the expanded value with `status = 'resolved'`
- if a mapping does not exist yet, store the abbreviation with
  `status = 'unresolved'`
- unresolved entries must be rerunnable later after the corporate dictionary is
  added

Recommended implementation:

- detect candidate abbreviations with deterministic rules or regexes
- expand only from a maintained corporate dictionary
- allow a later backfill when the dictionary grows

Minimal dictionary table:

```sql
CREATE TABLE corporate_abbreviations (
    short TEXT PRIMARY KEY,
    expanded TEXT NOT NULL
);
```

#### 8.2 Syntax statistics for reviews

Goal:

- produce compact syntactic statistics for reviews using trigrams

Execution rule:

- run only for `document_type = 'review'`
- run by a separate Python script, not by the agent

Recommended stored shape:

- trigram text
- count
- relative frequency

## Minimal schema

Local deployment note:

- the tables are created inside schema `agent_1`
- workers should use `search_path = agent_1, public` or qualify table names

### raw_items

```sql
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
```

### processing_jobs

```sql
CREATE TABLE processing_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'failed'))
);
```

### clean_items

```sql
CREATE TABLE clean_items (
    id BIGSERIAL PRIMARY KEY,
    raw_item_id BIGINT NOT NULL UNIQUE REFERENCES raw_items(id) ON DELETE CASCADE,
    clean_title TEXT,
    clean_text TEXT NOT NULL,
    language TEXT
);
```

### key_results

```sql
CREATE TABLE key_results (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    enrichment JSONB,
    enriched_at TIMESTAMPTZ,
    enriched_by TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
```

### document_kr_labels

Store one impact label per active KR.

```sql
CREATE TABLE document_kr_labels (
    clean_item_id BIGINT NOT NULL REFERENCES clean_items(id) ON DELETE CASCADE,
    kr_id BIGINT NOT NULL REFERENCES key_results(id) ON DELETE CASCADE,
    impact TEXT NOT NULL,
    signal_strength TEXT NOT NULL,
    theme TEXT NOT NULL,
    dashboard_description TEXT NOT NULL,
    why_for_goal TEXT NOT NULL,
    evidence TEXT[] NOT NULL,
    reasoning_steps JSONB NOT NULL,
    uncertainty TEXT NOT NULL,
    confidence NUMERIC(2,1) NOT NULL,
    is_sber_paid_news SMALLINT,
    prompt1_payload JSONB NOT NULL,
    prompt2_payload JSONB,
    prompt3_payload JSONB,
    PRIMARY KEY (clean_item_id, kr_id)
);
```

### label_kr_step_checkpoints

```sql
CREATE TABLE label_kr_step_checkpoints (
    clean_item_id BIGINT NOT NULL REFERENCES clean_items(id) ON DELETE CASCADE,
    kr_id BIGINT NOT NULL REFERENCES key_results(id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    status TEXT NOT NULL,
    payload JSONB NOT NULL,
    raw_output TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (clean_item_id, kr_id, step_name)
);
```

### llm_call_logs

```sql
CREATE TABLE llm_call_logs (
    id BIGSERIAL PRIMARY KEY,
    worker TEXT NOT NULL,
    clean_item_id BIGINT,
    kr_id BIGINT,
    step_name TEXT NOT NULL,
    job_id BIGINT,
    model TEXT,
    session_key TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL,
    success BOOLEAN NOT NULL,
    prompt_chars INTEGER NOT NULL,
    output_chars INTEGER,
    usage JSONB,
    error TEXT
);
```

### document_enrichments

```sql
CREATE TABLE document_enrichments (
    clean_item_id BIGINT PRIMARY KEY REFERENCES clean_items(id) ON DELETE CASCADE,
    entities JSONB,
    events JSONB,
    abbreviations JSONB
);
```

### corporate_abbreviations

```sql
CREATE TABLE corporate_abbreviations (
    short TEXT PRIMARY KEY,
    expanded TEXT NOT NULL
);
```

### review_trigrams

```sql
CREATE TABLE review_trigrams (
    clean_item_id BIGINT PRIMARY KEY REFERENCES clean_items(id) ON DELETE CASCADE,
    trigrams JSONB NOT NULL
);
```

## Contracts

### Agent KR impact labeling output

```json
{
  "impact": "positive",
  "signal_strength": "direct",
  "theme": "short theme",
  "dashboard_description": "short description",
  "why_for_goal": "causal link",
  "evidence": ["quoted fragment 1", "quoted fragment 2"],
  "reasoning_steps": ["fact", "impact", "goal link"],
  "uncertainty": "what is not proven",
  "confidence": 0.8
}
```

### Agent Sber PR-like flag output

```json
{
  "is_sber_paid_news": 0
}
```

### Agent semantic enrichment output

```json
{
  "entities": {
    "companies": [],
    "products": [],
    "people": [],
    "locations": [],
    "technologies": []
  },
  "events": [
    {
      "event_type": "string",
      "summary": "string",
      "participants": [],
      "event_time": null,
      "evidence": ["string"]
    }
  ]
}
```

### Abbreviation worker output

```json
[
  {
    "short": "ABC",
    "expanded": "Example Business Center",
    "status": "resolved"
  },
  {
    "short": "XYZ",
    "expanded": null,
    "status": "unresolved"
  }
]
```

### Review trigram output

```json
[
  {
    "trigram": "очень удобный интерфейс",
    "count": 4,
    "relative_frequency": 0.031
  }
]
```

## Validation rules

### KR labels

- every `kr_id` must exist and be active
- each evidence fragment must appear in the clean title or `clean_text`
- duplicate `kr_id` values are forbidden

### Agent entities and events

- event evidence must appear in `clean_text`
- extracted entities must be text-grounded
- event summaries must describe only what is present in the document

### Abbreviations

- unresolved abbreviations are allowed
- resolved values must come only from the corporate dictionary
- abbreviation detection must be rerunnable without redoing agent stages

### Review trigrams

- computed only for `document_type = 'review'`
- produced by a separate Python script
- `count` must be a positive integer
- `relative_frequency` must be numeric

## Notes

- Exact dedup stays in preprocessing.
- MinHash reprint removal is applied only to news-like documents.
- Abbreviation expansion is deterministic worker logic, not agent logic.
- Review trigrams are computed by a separate Python script.
- Abbreviation expansion depends on a later corporate dictionary and should be
  rerunnable.
