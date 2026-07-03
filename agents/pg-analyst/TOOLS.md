# TOOLS.md - Local Notes

## Database Aliases

- `dst_db` -> local Postgres db `dst_db`
- `graphrag_news` -> local Postgres db `graphrag_news`
- `postgres` -> local Postgres db `postgres`
- `randd_res_task_tracker` -> local Postgres db `randd_res_task_tracker`
- `research` -> local Postgres db `research`

## Rules

- Each alias should map to one Postgres database or cluster.
- Use read-only credentials only.
- Record schema quirks, important tables, and date columns here when known.

## Access Path

- Query path is MCP, not shell.
- One MCP server is configured per database alias.
- Available query tools:
  - `pg_dst_db.query`
  - `pg_graphrag_news.query`
  - `pg_postgres.query`
  - `pg_randd_res_task_tracker.query`
  - `pg_research.query`
- Current local access uses Unix socket connectivity to the local Postgres 16
  cluster.
