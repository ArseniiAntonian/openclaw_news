## Demo Contour

This directory contains an isolated demo flow for Agent 1.

- `demo_ingest.py` creates/fills schema `demo` in `mvp_db`
- `demo_run.py` runs a synchronous traceable demo over frozen `demo.clean_items`
- `run_trace.log` is appended on each demo run

This is a demo stand only. It does not use `agent_1.processing_jobs` and does not
write into schema `agent_1`.
