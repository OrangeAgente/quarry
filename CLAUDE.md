# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**Quarry** — a single-file-per-module Flask app that turns a search query into a persisted library of cleaned web pages plus optional LLM extractions. Pipeline: **DuckDuckGo search → concurrent headless-Chromium crawl (crawl4ai) → optional per-document LiteLLM extraction → SQLite**.

## Commands

```bash
# Local dev
python -m venv .venv && . .venv/Scripts/activate   # bin/activate on macOS/Linux
pip install -r requirements.txt
crawl4ai-setup            # one-time: downloads Chromium for crawl4ai (~5 min)
cp .env.example .env      # set COHERE_API_KEY (or change LLM_PROVIDER)
python app.py             # serves on 0.0.0.0:5000

# Docker
docker compose up -d --build
```

There is **no test suite, linter, or formatter** configured. Verify changes by running the app and exercising the routes.

## Architecture

The flow crosses a **sync/async boundary** that shapes most of the code:

- **Flask routes (`app.py`) are synchronous** but all storage and crawling is `async`. Routes call `run_async(coro)` (`app.py:60`), a helper that runs a coroutine to completion, spawning a thread-pool executor when an event loop is already running. Use `run_async()` for any DB call from a route — never `await` directly in a route.
- **Background jobs run in daemon threads.** `POST /search` → `create_job()` + `run_job_in_background()` (`jobs.py:183`) spins a thread that calls `asyncio.run(_run_job(...))`. `_run_job` (`jobs.py:197`) is the orchestrator: search → crawl-with-progress → store → optional extract → mark done.
- **The job store is in-memory global module state** (`jobs.py:52`, `_store`/`_lock`/`_recent_job_ids`). It holds the live URL stream, log lines, and the sidebar's "recent crawls" list. **It is wiped on process restart** — only the SQLite DB persists. The History page's "Live crawl" links auto-hide for jobs no longer in `_store` (`get_in_memory_job_ids`). All access goes through the module-level lock.

### Progress streaming

The crawl page polls `GET /api/job/<id>` (~500ms) and there is also an SSE endpoint `GET /api/job/<id>/stream` (`app.py:130`, capped at ~10 min). `job_state()` (`jobs.py:159`) is the single serialization point that converts a `Job` dataclass into the JSON the frontend consumes — keep it and the templates in sync.

### Two crawler functions — use the progress one

`crawler.py` has both `crawl_urls` (batch, no progress) and `crawl_urls_with_progress` (`crawler.py:69`). **The app only uses the latter.** It crawls with an `asyncio.Semaphore(4)` and reports per-URL state back into the job store via `update_url`/`add_log`/`inc_counter`. Crawl4ai produces two markdown variants per page: `raw_markdown` (stored as `content_markdown`) and `fit_markdown` (stored as `content_fit`); the extractor prefers `content_fit`.

### Storage (`storage.py`)

- **No connection pool** — every function opens its own `aiosqlite.connect()`. `DB_PATH` is resolved once and cached at module level (`get_db_path`).
- **`init_db()` is idempotent and self-migrating**: `CREATE TABLE IF NOT EXISTS`, an `ALTER TABLE ... ADD COLUMN job_id` wrapped in try/except, and an FTS5 virtual table `documents_fts`. If the FTS row count diverges from `documents`, it **rebuilds the whole FTS index**. Called lazily via `@app.before_request` (`app.py:73`) guarded by `app._db_initialized`.
- Documents are keyed by UUID but **`UNIQUE(url, search_query)`** — re-crawling the same URL under the same query replaces the row (`INSERT OR REPLACE`). FTS rows are deleted+reinserted alongside every document write to stay consistent.
- Full-text search input is tokenized and quoted by `_build_fts_query` before hitting `MATCH` to avoid FTS5 syntax injection.

### LLM extraction (`extractor.py`)

- Uses **LiteLLM** (`litellm.completion`) with `settings.llm_provider` as the model id. Switch vendors by changing `LLM_PROVIDER` (e.g. `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4-5`) and setting the vendor's API key env var.
- Note: the api key is always passed as `settings.cohere_api_key` (`extractor.py:54`) — for non-Cohere providers LiteLLM reads the vendor key from the environment instead, so the explicit arg is harmless but Cohere-specific.
- Content is **truncated to 20k chars** before the call. Output is parsed as JSON; on parse failure it's wrapped as `{"raw_response": ...}` rather than failing.

## Agentic collection (expert agents)

A second pipeline layered on top of the one-shot search. A saved **Agent**
(persona built from an area of expertise via `prompt_templates.build_persona`)
runs a **Mission** against a question using an intelligence-collection loop:
**plan → approve → collect/assess/re-task → synthesize brief.**

- **Two background stages split by the approval gate** (`agent_runner.py`):
  `start_planning` decomposes the question into `requirements` (EEIs) and stops
  at `status=awaiting_approval`; `POST /missions/<id>/approve` then launches
  `start_collection`, which loops searching → `crawl_urls_with_progress` →
  `assess_requirement` (gap analysis) → re-tasking the unmet gaps. Both run in
  daemon threads (`asyncio.run`), mirroring `jobs._run_thread`.
- **Completion is concrete, not vibes:** a mission is done when every requirement
  is `satisfied` or `unmet`. The circuit-breaker is `per_req_attempts` — a
  requirement still unmet after that many tries is marked `unmet` and the agent
  moves on. `max_passes`/`max_sources` are the global budget backstops (stored in
  `missions.budget_json`).
- **Source of truth is SQLite, not the job store.** Mission status, requirements,
  and the brief live in the new tables (`agents`, `missions`, `requirements`,
  `mission_documents`). The in-memory job store (`jobs.create_mission_job`) only
  carries the **live trace** (log lines), so the mission view degrades to a
  static DB-rendered page once the process restarts — same pattern as the History
  "Live crawl" auto-hide.
- **Use `storage.upsert_document`, not `insert_document`, for collected docs.**
  It refreshes a doc in place on `(url, search_query)` conflict and keeps the
  same id, so `mission_documents` never points at an orphaned id (plain
  `INSERT OR REPLACE` mints a new id on conflict).
- **LLM calls go through `llm.py`** (`chat` / `chat_json`), a thin wrapper over
  LiteLLM with robust JSON extraction (`_extract_json` handles ```json fences and
  prose). It supports a **two-tier model setup** via a `tier` arg:
  `"reasoning"` → `settings.llm_provider` (Cohere) for **planning** and
  **assessment**; `"fast"` → `settings.llm_provider_fast` for summarization-style
  work (**brief synthesis** and the **document extractor**). The fast tier is
  optional — empty `LLM_PROVIDER_FAST` falls back to the reasoning model.
  `_provider_kwargs` branches on the model id: Ollama models
  (`ollama/`, `ollama_chat/`) get `ollama_api_base` and no key; hosted providers
  get `cohere_api_key`. A local Ollama is reachable from the container at
  `http://host.docker.internal:11434` on Docker Desktop. `model_for(tier)`
  resolves the id. **Fast-tier calls auto-fall back to the reasoning model** on
  provider failure (e.g. Ollama down); `chat_ex` returns
  `(text, model_that_actually_answered)` and `extractor` records that true
  producer on each extraction row.
- **The mission view** (`templates/mission.html`) renders three states from
  `mission.status` — editable **approval gate**, **requirements matrix**, and
  **brief + citation-linked source rail**. It polls `GET /api/mission/<id>`
  (~1.2s), updates the matrix/telemetry in place, and **reloads on status
  change** so the server re-renders the gate / brief rather than duplicating
  that rendering in JS.
- **The approval gate is editable.** The DOM *is* the plan; on submit JS
  serializes titles, per-requirement queries, and dropped ids into a
  `plan_json` field. `POST /missions/<id>/approve` applies those edits
  (update/insert/`delete_requirement`) before `start_collection`. With JS off
  the field is empty and the plan is approved as drafted.
- **The assessor's reasoning is persisted, not discarded**: `assessment_missing`
  and `assessment_confidence` columns on `requirements` feed the matrix's gap /
  satisfied callouts. This was the single biggest gap in the old UI.
- **Citation numbering must match the brief.** `brief.ordered_sources()` is the
  one ordering both the LLM prompt and the source rail use;
  `brief.linkify_citations()` turns `[n]` into `.cite` controls **after**
  `render_markdown` sanitization (it only ever injects markup built from an
  integer it re-serializes, so the sanitizer is never weakened or bypassed).
- **Telemetry + cooperative stop** live in the job store (`pass_num`,
  `sources_used`, `cancel_requested`); `agent_runner` checks `jobs.is_cancelled`
  at pass boundaries so a stop still produces a brief from what was collected.
- **Static assets are cache-busted** by mtime (`@app.url_defaults`), because a
  deploy otherwise leaves users on a stale `style.css`.
- **Scheduling ("morning brief")** lives in `scheduler.py`. An agent with a
  `schedule_cron` **and** a `schedule_question` gets an APScheduler job
  (`coalesce`, `max_instances=1`, so a missed window fires once and runs never
  overlap). A scheduled mission carries `auto_approve` in `budget_json`:
  `_run_planning` sees it, skips the human gate, and goes straight to
  collecting — nobody is at the keyboard at 07:00. Each run links to the
  previous one via `parent_mission_id`, and the brief leads with what is new
  since that specific mission (not "any mission ever", or another agent's crawl
  of the same URL would wrongly count as seen). `POST /agents/<id>/run-scheduled`
  fires it now, so a brief can be tested without waiting for its cron window.
  One scheduler exists because the image runs exactly one gunicorn worker.

### Collection quality (why these exist)

- **Search must not depend on one engine.** `search.py` walks
  `settings.search_backends` (default `auto,brave,bing,duckduckgo`) until one
  answers. Measured on this box: the `duckduckgo` backend returned **0** results
  for a query where brave/bing returned 4 in under a second. A single-engine
  search silently starves missions.
- **A "successful" crawl is often a captcha wall.** `content_quality.py` is the
  one place that judges real content vs. block/interstitial/near-empty pages,
  shared by the crawler (before storing — junk must not consume the source
  budget) and the assessor/brief (before prompting). `crawler.py` also enables
  crawl4ai's stealth options, since plain headless Chromium is trivially
  fingerprinted.
- **One flaky LLM call must not destroy a paid-for mission.** Three layers:
  `llm._complete_retrying` retries transient provider failures (Cohere's
  `NO_VALID_RESPONSE_GENERATED` is common), `assess_requirement` converts an
  exception into a "not assessed" verdict, and `agent_runner._collect_one` is
  wrapped so a requirement's failure costs it one attempt rather than the run.
- Extraction runs `EXTRACT_CONCURRENCY` documents at once; it was the slow tail
  of every mission.

### Tests

There is now a `tests/` suite (the repo's first). It runs without the heavy
crawl stack by stubbing `litellm`/`crawl4ai`/`ddgs` (`tests/stubs/`). Run it with
those stubs on the path:

```bash
PYTHONPATH=.:tests/stubs python -m pytest -q tests
```

`test_agent_planner` / `test_agent_assessor` / `test_brief` monkeypatch the LLM
calls; `test_storage_smoke` exercises the real schema, `upsert_document`
id-reuse, the join-table queries, and FTS consistency against a temp SQLite file.
`test_templates_parse` parses every Jinja template, and `test_style_css` guards
the stylesheet against corruption — a stray shell line once landed in it, and
browsers silently drop **every rule after** a malformed one, so the file still
"contains" styles that never load.

`tests/qa_harness.py`, `qa_mission_ui.py`, and `qa_ollama.py` are end-to-end
drivers (not pytest): copy one into the running container and execute it there,
e.g. `docker compose cp ./tests/qa_mission_ui.py web-researcher:/tmp/x.py &&
docker compose exec -T web-researcher python /tmp/x.py`.

## Security-relevant invariants (preserve these)

- **All crawled markdown is rendered through `markdown_render.render_markdown`** (`markdown_render.py:35`), which runs `markdown` → `bleach.clean` (tag/attr/protocol allowlist) → `bleach.linkify` with `rel="noopener nofollow" target="_blank"` hardening. Never bypass this when displaying crawled content.
- Live-log messages built in `crawler.py`/`jobs.py` HTML-escape interpolated page data with `html.escape` (`_esc`). Keep doing this — log strings are injected into the DOM.
- Inputs are bounded at the route layer: query ≤500, extract prompt ≤5000, `max_results` clamped 1–20, full-text `q` ≤200. Jinja autoescape is on everywhere.
- **Auth is optional and env-only** (`auth.py`): setting `QUARRY_PASSWORD`
  (plaintext or Werkzeug hash) gates every route except `login`/`static` via a
  `before_request` guard — HTML gets a redirect, `/api/*` gets 401 JSON. The
  login page is **standalone on purpose** (base.html's sidebar leaks counts,
  queries, and model config pre-auth). Login is rate-limited per IP in memory.
  `QUARRY_PASSWORD` must never become a Settings-page override — an
  unauthenticated visitor could set or clear it.
- The session signing key persists in `data/secret_key` (0600) via
  `config.persistent_secret_key`, so logins survive restarts; `FLASK_SECRET_KEY`
  overrides. Cookies are HttpOnly + SameSite=Lax; security headers
  (frame-deny, nosniff, referrer-policy) go out on every response.

## Config

All settings come from `.env` via Pydantic Settings (`config.py`). The singleton `settings` is imported across modules. Key vars: `COHERE_API_KEY`, `LLM_PROVIDER`, `LLM_PROVIDER_FAST`, `OLLAMA_API_BASE`, `DB_PATH` (default `data/research.db`), `CRAWL_TIMEOUT` (ms), `FLASK_HOST/PORT/DEBUG`, `FLASK_SECRET_KEY`, `QUARRY_BIND` (compose-level publish interface). Never set `FLASK_DEBUG=true` on a network-reachable host (Werkzeug console is an RCE primitive).

**UI-editable overrides:** the Settings page (`/settings`) persists `llm_provider`, `llm_provider_fast`, `ollama_api_base`, `cohere_api_key`, and `search_max_results` to `data/settings.json` (`config.save_overrides`), which is layered over `.env` at import (`load_overrides`) and mutated live on save — **`settings.json` wins over `.env`** for those keys. `known_models()` accumulates every model id ever saved so the Settings dropdowns never lose a previously used value.

## Deployment notes

- Docker runs **gunicorn with exactly 1 worker** (× 16 gthread threads). Keep `--workers 1`: the live job/mission trace is module-global memory (`jobs._store`), so >1 worker splits state. gthread is required so long-lived SSE streams aren't killed by the worker timeout. `python app.py` remains the Flask dev server for local dev.
- Compose publishes on **`127.0.0.1` by default** (`QUARRY_BIND`) because the app has no auth; a healthcheck hits `/` every 30s.
- This is a **single-user design**: the global job store and recent-crawls tracker are not safe for concurrent users.
- Docker: `entrypoint.sh` runs as root only to `chown` the bind-mounted `data/`, then drops to the non-root `app` user (UID 1000) via `gosu`. The Dockerfile installs Chromium OS deps as root (`playwright install-deps`) *before* downloading the browser binary as `app`, because `crawl4ai-setup`'s own dep step needs root and fails silently otherwise.
