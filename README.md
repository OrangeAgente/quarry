# Quarry

A small Flask app that turns a question into a searchable library of cleaned web pages and structured LLM extractions.

The agent runs three stages end-to-end:

1. **Search** — DuckDuckGo text search via [`ddgs`](https://pypi.org/project/ddgs/).
2. **Crawl** — concurrent headless Chromium fetches via [`crawl4ai`](https://github.com/unclecode/crawl4ai), producing both raw and fit-markdown.
3. **Extract** *(optional)* — per-document LLM extraction via [`litellm`](https://github.com/BerriAI/litellm) (default provider: Cohere). Output is JSON: summary, key facts, entities, topics, sentiment — or whatever your custom prompt asks for.

Everything is persisted to SQLite so you can re-open documents, re-run extractions, and revisit the search trail later.

## Quick start (Docker)

```bash
cp .env.example .env
# edit .env — set COHERE_API_KEY (or change LLM_PROVIDER for a different vendor)

docker compose up -d --build
```

Then open [http://localhost:5000](http://localhost:5000).

The first build is slow (~5 min) because `crawl4ai-setup` downloads Chromium. Subsequent rebuilds reuse cached layers.

The `data/` directory is bind-mounted, so `data/research.db` survives container rebuilds.

## Local dev (no Docker)

```bash
python -m venv .venv && . .venv/Scripts/activate   # or .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
crawl4ai-setup            # one-time: installs Chromium for crawl4ai
cp .env.example .env      # then edit
python app.py
```

## Configuration

All settings come from `.env` (see `.env.example`):

| Variable | Purpose | Default |
| --- | --- | --- |
| `COHERE_API_KEY` | API key for the LLM extractor | *(empty — extraction will fail)* |
| `LLM_PROVIDER` | LiteLLM model identifier | `cohere/command-r-plus` |
| `SEARCH_MAX_RESULTS` | Default result count for the search form | `5` |
| `DB_PATH` | SQLite file path | `data/research.db` |
| `CRAWL_TIMEOUT` | Per-page crawl timeout (ms) | `30000` |
| `FLASK_HOST` / `FLASK_PORT` | Bind address | `0.0.0.0:5000` |
| `FLASK_DEBUG` | Flask debug + auto-reload | `false` |
| `FLASK_SECRET_KEY` | Override Flask session signing key. Empty → fresh random key per process start | *(empty)* |

Switching LLM provider: anything LiteLLM supports works (e.g. `openai/gpt-4o-mini`, `anthropic/claude-sonnet-4-5`). Set the matching `*_API_KEY` env var that LiteLLM expects for that vendor.

## Features

- **End-to-end agent run** with a live progress page showing search results, in-flight crawls, and an agent log stream.
- **Library** — every crawled page, deduplicated by URL, with client-side filtering by domain/age/word-count and full-text search across titles + snippets.
- **History** — every search the agent ran, grouped by day. Each entry has:
  - **Live crawl** — opens the trace (URL stream + agent log) for that run, while the job is still in memory.
  - **View** — opens Library filtered to just the documents from that search (`/documents?search=...`).
  - **Re-run** — re-submits the same query.
- **Persistent sidebar** — "Live crawl" links to the most recent run (pulse dot when active); "Previous live crawls" lists earlier runs from the current process.
- **Per-document deep view** — markdown content, metadata, links, related documents from the same search/domain, and any extractions, plus an inline button to run a new extraction with a custom prompt.

## Architecture

```
app.py            Flask routes + context processor; spawns background jobs.
config.py         Pydantic Settings → reads .env.
models.py         Pydantic models: SearchResult, Document, ExtractedData, SearchRecord.
search.py         DuckDuckGo wrapper.
crawler.py        crawl4ai async wrapper; emits per-URL progress to the job store.
extractor.py      LiteLLM call; tries to parse JSON, falls back to {"raw_response": ...}.
jobs.py           In-memory job store + threaded async runner.
                  Tracks recent job IDs for the sidebar.
storage.py        aiosqlite — documents / extractions / searches tables.
templates/        Jinja2 — base.html (shell + sidebar) extended by per-page templates.
static/style.css  Hand-rolled CSS, supports light/dark themes + accent swatches.
```

### Job lifecycle

1. `POST /search` calls `create_job(...)` → returns a UUID and starts a daemon thread.
2. The thread runs `_run_job(job_id)` → search → crawl → optional extract → done.
3. The crawl page polls `GET /api/job/<id>` every 500ms, rendering URL rows and log lines incrementally.
4. On completion, the page stops polling and shows a "Jump to results" button (no auto-redirect).
5. The job record (URL stream + log + document IDs) lives in memory until the process restarts.

### Data model

```
documents       crawled pages, keyed by UUID, unique on (url, search_query)
extractions     LLM output per document, with the prompt that produced it
searches        one row per agent run, with job_id back-reference for trace lookup
```

## Security posture

Designed for **single-user local use** behind a firewall. Some specifics:

- **No authentication.** Anyone who can reach the port can run searches and burn your LLM API quota. Don't expose the port directly to the internet — put it behind Tailscale, a reverse proxy with auth, or rebind `FLASK_HOST=127.0.0.1` for localhost-only.
- **`FLASK_DEBUG` defaults to `false`** in `.env.example`. Never set it to `true` on a host reachable from untrusted networks — Werkzeug's debugger console is an RCE primitive.
- **Crawled HTML is treated as untrusted.** Page titles, URLs, and error strings are HTML-escaped before being inserted into the live agent log. Server-side templates use Jinja autoescape throughout.
- **Inputs are bounded:** query ≤ 500 chars, extraction prompt ≤ 5000 chars, `max_results` clamped to 1–20.
- **`flask_secret_key` is randomized per process start** unless you set `FLASK_SECRET_KEY` in `.env`. Sessions/flash messages reset on restart (they're not persistent here, so that's fine).
- **Container runs as a non-root `app` user** (UID 1000). The bind-mounted `data/` directory must be writable by that UID on the host.
- **Prompt injection is possible** — the LLM extractor sees raw page content. Treat extraction output as suggestion, not ground truth. Don't pipe it into anything that auto-executes.
- **No CSRF tokens.** Acceptable for a localhost-only app where the SameSite=Lax default on session cookies blocks the relevant cross-site POST scenarios. If you front this with a real domain and add auth, add CSRF tokens too.
- **DDG returns external URLs only.** No allowlist on what the crawler will fetch — a crafted query could in theory point the crawler at a private network address. Out of scope today; consider an SSRF guard if you ever expose this.

## Notes & caveats

- **In-memory job store.** URL streams, log entries, and the sidebar's "Previous live crawls" list reset when the Flask process restarts. The DB persists; the live trace does not. The History → Live Crawl button auto-hides for jobs no longer in memory.
- **DuckDuckGo rate limiting.** Heavy use can return zero results temporarily; the agent surfaces this as `no search results`.
- **Extraction context window.** Documents are truncated to ~20k chars before being sent to the LLM (see `extractor.py`).
- **Single-user design.** The job store and "recent crawls" tracker are global module state — fine for local use, not safe for multi-user deployments.
- **Production WSGI.** `app.py` uses Flask's dev server. For real deployment, swap the `CMD` for `gunicorn` or similar.

## License

Not licensed for redistribution by default — add a `LICENSE` file if you intend to publish.
