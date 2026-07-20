# agent_1 Runtime

> **Note (2026-07-20):** `agent_1`'s scope is narrowing to collect → clean →
> dedup → embeddings (rework-agent-1-v5); KR labeling and semantic
> enrichment move to future Agents 2/4. This doc still describes the
> currently-running v1 workers (`agent_1` schema); the "KR labeling worker"
> section below is legacy reference — `preprocess_worker` no longer enqueues
> `label_kr` jobs. See `openspec/specs/agent_1*` and
> `openspec/changes/rework-agent-1-v5/` for current/target behavior.

## Parsers360 ingestion

The Parsers360 parser pulls raw news into `agent_1.raw_items` inside `mvp_db`.

### What it does

- sends `POST` to the URL configured in `PARSERS360_API_URL`
- always sends `service=parser`
- requests `summary=true` and `company=true` by default
- paginates with fixed `limit=200` and increasing `page`
- commits each successfully fetched page to Postgres immediately
- requests rows with `start_at=<yesterday UTC date>`
- inserts into `agent_1.raw_items`
- relies on a DB trigger to enqueue `preprocess` jobs automatically
- supports vendor `Basic Auth` via `PARSERS360_BASIC_USER` and
  `PARSERS360_BASIC_PASSWORD`
- retries failed HTTP/JSON requests up to 10 times with a 10 second pause
- uses a fixed HTTP timeout of 20 seconds
- writes logs to stdout and to `PARSERS360_LOG_FILE` when set, otherwise to
  `logs/parsers360.log`

### Current time-window behavior

The current script does not implement CLI interval selection. On every run it
computes:

```text
start_at = current UTC date - 1 day
```

and sends that date to the vendor API as the `start_at` query parameter. It
does not perform any additional timestamp filtering after the API response.

### API quirks handled in code

- the response may be a JSON value or a JSON-encoded string
- `created_at` is parsed as a Unix timestamp in seconds when possible
- the raw parser item `id` is stored as `external_id`
- `summary`, `companies`, `source`, `is_duplicated`, and `original_id` are
  preserved in `source_metadata`
- the client keeps requesting next pages until the API returns an empty page or
  a page shorter than `limit=200`
- TLS certificate verification is enabled by default and can be disabled with
  `PARSERS360_VERIFY_SSL=false`

### Install

```bash
cd /root/.openclaw/workspace/agents/agent_1
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

### Local secrets

The parser auto-loads `/root/.openclaw/workspace/agents/agent_1/.env` on startup.
Secrets and defaults can live there, for example:

- `PARSERS360_API_URL`
- `PARSERS360_TOKEN`
- `PARSERS360_BASIC_USER`
- `PARSERS360_BASIC_PASSWORD`
- `AGENT_1_DB_DSN`
- `PARSERS360_VERIFY_SSL`
- `PARSERS360_LOG_FILE`

### Run

Fetch rows using yesterday's UTC date as `start_at`:

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
PYTHONPATH=src python -m agent_1.parsers360_ingest
```

Override the endpoint through the environment:

```bash
PARSERS360_API_URL='https://parsers360.ru:10443/enablers-api/api/v2/parametrized' \
PYTHONPATH=src python -m agent_1.parsers360_ingest
```

Follow logs live:

```bash
tail -f /root/.openclaw/workspace/agents/agent_1/logs/parsers360.log
```

## Preprocess worker

`preprocess_worker` drains `agent_1.processing_jobs` rows with
`job_type='preprocess'`, cleans the text, performs exact duplicate detection,
applies a lightweight MinHash-style near-duplicate check for news documents,
and writes surviving documents into `agent_1.clean_items`. As of
rework-agent-1-v5 stage 2 it no longer enqueues `label_kr` jobs — `agent_1`'s
scope stops at clean/dedup (embeddings are a separate future stage).

The current preprocess stage is Russian-only by design: if the cleaned document
contains enough alphabetic text to classify and the script balance is not
Russian, the worker records `source_metadata.preprocess.status='filtered_out'`
with `reason='non_russian_text'`, marks the job `done`, and does not create a
`clean_items` row.

By default the near-duplicate check follows the Agent 1 deduplication spec:
dedup text is built from normalized `title + summary + content[:4000]`, then
token-normalized (`lower()`, punctuation stripped, `ё -> е`), hashed with mixed
word shingles of length `5` plus character shingles of length `17`, passed
through `64` MinHash permutations, LSH banding with `b = 16` bands and
`r = 4` rows per band, and finally accepted with MinHash signature similarity
threshold `0.7`.

Duplicate decisions are recorded inside `raw_items.source_metadata.preprocess`
so the raw record is preserved even when no `clean_items` row is created.

Run one batch and exit:

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
PYTHONPATH=src python -m agent_1.preprocess_worker --once --batch-size 50
```

Run continuously:

```bash
PYTHONPATH=src python -m agent_1.preprocess_worker
```

Follow the worker log:

```bash
tail -f /root/.openclaw/workspace/agents/agent_1/logs/preprocess_worker.log
```

## KR labeling worker (legacy, disconnected from the queue)

> No longer part of `agent_1`'s active pipeline: `preprocess_worker` stopped
> enqueueing `label_kr` jobs (rework-agent-1-v5 stage 2). The worker code and
> instructions below still work standalone (e.g. for manual runs or as
> reference while building Agent 4), but nothing feeds it automatically
> anymore. `document_kr_labels` and related tables are read-only history —
> see `agents/agent_1/db/007_mark_kr_labeling_readonly.sql`.

`label_kr_worker` drains `agent_1.processing_jobs` rows with
`job_type='label_kr'`, loads the matching `clean_items` row and all active
`key_results`, calls OpenClaw `agent_1`, validates the returned JSON, and stores
multi-level labels in `agent_1.document_kr_labels`.

For every active KR, the worker runs the configured prompts in order:
impact-on-KR first, then the Sber PR-like flag and the third prompt when impact
is `positive` or `negative`. Every evidence fragment returned by the first prompt must
appear verbatim in the clean title or `clean_items.clean_text`.

Before each KR-specific LLM call, the worker checks `key_results.enrichment`,
which is the JSON produced by agent 2 for the KR. The expected agent 2 fields are
`тема`, `ключевые_слова`, and `типы_источников`. Source types with `важность`
`3` or `2` are treated as allowed for that KR; `1` is skipped. If a KR has no
enrichment yet, the worker labels it normally. If enrichment has ranked source
types but the document has no matching explicit `source_type`, that KR is skipped
without an LLM call.

Each LLM step is checkpointed in `agent_1.label_kr_step_checkpoints`, so reruns
can resume after the last successful step. Every OpenClaw call is logged in
`agent_1.llm_call_logs` with timing, prompt/output sizes, success/error, and any
usage/cost fields exposed by the OpenClaw JSON response.

If there are no active key results, the worker leaves `label_kr` jobs pending
and logs a warning. It does not advance documents to semantic extraction without
KR context.

After a clean item is labeled, the worker enqueues one `extract_semantics` job
for the same `clean_item_id`.

Run one batch and exit:

```bash
cd /root/.openclaw/workspace/agents/agent_1
. .venv/bin/activate
PYTHONPATH=src python -m agent_1.label_kr_worker --once --batch-size 5
```

Run continuously:

```bash
PYTHONPATH=src python -m agent_1.label_kr_worker
```

Useful configuration:

- `AGENT_1_OPENCLAW_CMD` overrides the OpenClaw command path
- `AGENT_1_LABEL_AGENT_ID` defaults to `agent_1`
- `AGENT_1_LABEL_AGENT_TIMEOUT_SECONDS` defaults to `600`
- `AGENT_1_LABEL_MODEL` optionally overrides the model
- `AGENT_1_LABEL_THINKING` optionally overrides thinking level
- `AGENT_1_LABEL_BATCH_SIZE` defaults to `1`
- `AGENT_1_LABEL_RATE_LIMIT_PER_MINUTE` defaults to `20`
- `AGENT_1_LABEL_RETRY_FAILED=true` also claims failed jobs and resumes from checkpoints
- `AGENT_1_LABEL_LOG_FILE` defaults to `logs/label_kr_worker.log`

Follow the worker log:

```bash
tail -f /root/.openclaw/workspace/agents/agent_1/logs/label_kr_worker.log
```
