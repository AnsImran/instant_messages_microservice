# microservice-instant-messages

A small web service that delivers messages to a Microsoft Teams chat. Two ways to send:

- **Adaptive Card** (`POST /api/v1/teams/messages`) — describe "banner with red alert, title in bold, a row of ticket info, two buttons" and it builds the Adaptive Card JSON for you.
- **Plain text** (`POST /api/v1/teams/text`) — send `{"text": "..."}` and it posts it as a plain message (for a Power Automate "Post message" flow that maps `body.text`). Teams renders light Markdown; newlines preserved.

**Delivery is queued and fire-and-forget.** A request is validated, the webhook is resolved, the payload is built, and then **handed to a per-webhook queue and acknowledged with `202 queued` immediately**. A background worker drains each webhook's queue on a fixed cadence (`per_webhook_min_interval_seconds`, default 0.5s) so a burst to one webhook isn't throttled/dropped downstream by Power Automate / Teams. Each webhook is paced independently.

Built on FastAPI. Containerised. Deployed to EC2 through GitHub Actions. Everything is wired up for production: configuration, logging, retries, health checks, admin endpoints, tests.

---

## How a message travels through the service

This is what happens from the moment a caller decides to send a message, to the moment the card appears in Teams. Every shape in this picture is a real thing inside the service; the language is deliberately non-technical.

The request path is now **fire-and-forget**: the caller gets a `202 queued` the moment the message is accepted into the per-webhook queue. The actual POST to Teams happens later, in a background worker, paced per webhook.

```mermaid
flowchart TD
    start(["A caller decides to send a message to a Teams chat"])
    start --> post["HTTP POST<br/>(Adaptive Card -> /teams/messages,<br/>or plain text -> /teams/text)"]

    subgraph service ["Request path — returns 202 immediately"]
      direction TB
      post   --> stamp["Stamp the request with a unique ticket number"]
      stamp  --> check{"Does the request look valid?"}
      check  -- "No" --> rejectBad["Reject (422 / 4xx) with the ticket number"]
      check  -- "Yes" --> pickWebhook["Resolve which webhook to deliver to<br/>(webhook_url / webhook_target / default)"]
      pickWebhook --> build["Build the payload:<br/>Adaptive Card (/messages) or {text} (/text)"]
      build  --> enqueue{"Room in this webhook's queue?"}
      enqueue -- "No (full)" --> q503["Reject 503 WEBHOOK_QUEUE_FULL"]
      enqueue -- "Yes" --> accepted["Enqueue + return 202 'queued'"]
    end

    accepted --> caller(["Caller is done — has its 202"])

    subgraph worker ["Background worker — one per webhook URL, paced"]
      direction TB
      slot["Wait for the next paced slot<br/>(>= per_webhook_min_interval_seconds apart)"]
      slot --> deliver["POST the payload to Teams"]
      deliver --> teamsAnswer{"Accepted?"}
      teamsAnswer -- "Yes" --> logok["Log webhook_delivered"]
      teamsAnswer -- "Slow / 5xx / network" --> retry["Backoff + retry (up to N)"]
      retry --> deliver
      teamsAnswer -- "4xx or retries exhausted" --> logfail["Log webhook_delivery_failed (swallowed)"]
    end

    accepted -. "picked up later, paced" .-> slot
    logok --> land(["The message appears in the Teams chat"])
    rejectBad --> sawError(["Caller sees an error response"])
    q503 --> sawError
```

Note the consequence of fire-and-forget: a delivery that fails *after* the 202 (4xx, or retries exhausted) is **logged server-side, not returned to the caller**. The caller only learns of failures that happen *before* the 202 (validation 422, unknown target 400, queue full 503).

Two supporting cycles worth mentioning that don't fit in the flow above:

- **Startup**. When the service boots, it reads `.env` and `config/app.yaml`, opens a connection pool to Teams, and only *then* says "I'm ready". Liveness/readiness endpoints reflect this.
- **Shutdown**. On `Ctrl+C` or a container stop signal, the service finishes in-flight requests, closes the connection pool, and exits cleanly.

---

## The codebase at a glance

This map shows every important file in the repo and how they depend on each other. The entry point is the root `main.py`; every arrow means "the file at the tail of the arrow needs the file at the head". The italic text under each filename is a one-line layperson description of what the file is *for*.

```mermaid
flowchart TD
    root(["<b>main.py</b> (repo root)<br/><i>the 'start' button —<br/>just launches the web server</i>"])
    root --> appmain["<b>src/main.py</b><br/><i>assembles every part of the service<br/>and turns it on</i>"]

    subgraph API ["🏪 API layer — the service's front counter"]
      direction TB
      router["<b>api/v1/router.py</b><br/><i>directory board: sends each incoming<br/>request to the right window</i>"]
      ep_teams["<b>endpoints/teams.py</b><br/><i>the 'please send this Teams<br/>message' window</i>"]
      ep_health["<b>endpoints/health.py</b><br/><i>the 'are you open?' bell —<br/>used by orchestrators to check<br/>the service is alive</i>"]
      ep_admin["<b>endpoints/admin.py</b><br/><i>manager-only switches —<br/>reload config, peek at settings.<br/>Requires a key.</i>"]
      ep_meta["<b>endpoints/meta.py</b><br/><i>the name + version<br/>plate on the door</i>"]
      deps["<b>api/deps.py</b><br/><i>shared toolbox — every window<br/>reaches into this for settings,<br/>the Teams craftsman, the key checker</i>"]

      router --> ep_teams
      router --> ep_health
      router --> ep_admin
      router --> ep_meta
      ep_teams --> deps
      ep_admin --> deps
      ep_meta  --> deps
    end

    subgraph SVC ["🛠️ Service layer — the worker who does the job"]
      direction TB
      svc["<b>services/teams.py</b><br/><i>the craftsman: paints the Adaptive Card<br/>(or renders {text}), delivers it to Teams,<br/>retries if Teams is slow</i>"]
      disp["<b>services/dispatcher.py</b><br/><i>the dispatcher: one paced queue + worker<br/>per webhook URL. Endpoints enqueue here<br/>and return 202; it drains each webhook<br/>~0.5s apart, fire-and-forget</i>"]
      disp --> svc
    end

    subgraph CORE ["🧱 Core — the building's infrastructure"]
      direction TB
      cfg["<b>core/config.py</b><br/><i>the settings binder — reads .env<br/>and config/app.yaml, hands out the<br/>current values, knows how to<br/>refresh without a restart</i>"]
      log["<b>core/logging.py</b><br/><i>the scribe — writes every event<br/>to the journal (pretty or JSON)</i>"]
      mw["<b>core/middleware.py</b><br/><i>the door stampers — every arriving<br/>request gets a ticket number; every<br/>departure is recorded in the journal</i>"]
      handlers["<b>core/handlers.py</b><br/><i>the complaints desk — turns any<br/>failure into a polite, consistent<br/>response (the error envelope)</i>"]
      excs["<b>core/exceptions.py</b><br/><i>the dictionary of problems —<br/>every possible failure has a<br/>clear name and HTTP status</i>"]
    end

    subgraph SCH ["📋 Schemas — the paperwork templates"]
      direction TB
      sch_teams["<b>schemas/teams.py</b><br/><i>the order form —<br/>what a valid Teams message<br/>request must look like</i>"]
      sch_admin["<b>schemas/admin.py</b><br/><i>shapes for manager-only<br/>responses (version, settings snapshot,<br/>reload result)</i>"]
      sch_common["<b>schemas/common.py</b><br/><i>shared pieces — the standard<br/>error response, the strict base model</i>"]
      sch_enums["<b>schemas/enums.py</b><br/><i>the allowed-words lists —<br/>e.g. 'bold' may only be<br/>lighter / default / bolder</i>"]
      sch_teams  --> sch_common
      sch_teams  --> sch_enums
      sch_admin  --> sch_common
    end

    appmain --> router
    appmain --> handlers
    appmain --> mw
    appmain --> log
    appmain --> cfg
    appmain --> disp

    ep_teams  --> disp

    deps      --> cfg
    deps      --> excs
    deps      --> svc

    svc       --> cfg
    svc       --> excs
    svc       --> sch_teams

    cfg       --> excs
    handlers  --> excs
    handlers  --> sch_common

    ep_teams  --> sch_teams
    ep_admin  --> sch_admin
    ep_health --> sch_admin
    ep_meta   --> sch_admin

    classDef entry   fill:#f7e9ff,stroke:#6b21a8,color:#2e1065
    classDef api     fill:#eaf3ff,stroke:#2b4a8f,color:#142445
    classDef svc     fill:#fff3e0,stroke:#a16207,color:#422c04
    classDef core    fill:#e8f7ee,stroke:#2a7a3d,color:#113a1b
    classDef sch     fill:#fdecef,stroke:#b42d4b,color:#4c0c1c
    class root,appmain entry
    class router,ep_teams,ep_health,ep_admin,ep_meta,deps api
    class svc,disp svc
    class cfg,log,mw,handlers,excs core
    class sch_teams,sch_admin,sch_common,sch_enums sch
```

### One-line purpose of every file (plain English)

| File | What it's for |
|---|---|
| `main.py` (repo root) | The one-button launcher. Starts the web server. Nothing else. |
| `src/main.py` | Assembles all the pieces — middleware, error handlers, routes — and hands you a ready-to-serve app. |
| `src/api/v1/router.py` | A directory that says "health calls go here, Teams calls go there, admin calls go in that corner." |
| `src/api/v1/endpoints/teams.py` | The windows that accept send requests: `/messages` (Adaptive Card) and `/text` (plain text). Each resolves the webhook, builds the payload, hands it to the dispatcher, and returns **202 queued**. |
| `src/api/v1/endpoints/health.py` | The bell other systems ring to ask "are you up? are you ready?" |
| `src/api/v1/endpoints/admin.py` | Manager-only. Reload config from disk, show the current settings (with secrets hidden). Needs the admin key. |
| `src/api/v1/endpoints/meta.py` | Like the little plate next to your doorbell: name of the service, version. |
| `src/api/deps.py` | The shared toolbox every endpoint reaches into — settings, the Teams worker, the admin-key check, request id. |
| `src/services/teams.py` | Renders the Adaptive Card from your description, resolves the webhook (`resolve_webhook_url`), and performs the actual POST with retry/backoff (`post_rendered`, called by the dispatcher's worker). Retries on flaky networks / 5xx, never on 4xx. |
| `src/services/dispatcher.py` | The per-webhook outbound **queue**. One `asyncio.Queue` + one background worker per webhook URL; the worker fires sends on a fixed cadence (≥ `per_webhook_min_interval_seconds`) so a burst to one webhook isn't throttled downstream. Created at startup (lifespan), drained in the background, fire-and-forget. Raises `WebhookQueueFull` when a queue is at capacity. |
| `src/core/config.py` | Reads `.env` and `config/app.yaml`. Remembers the values. Can reload from disk on demand without a restart. Masks secrets when you ask for a settings snapshot. |
| `src/core/logging.py` | Decides how log lines look. JSON in production (for log shippers), readable for local development. |
| `src/core/middleware.py` | Stamps every request with a unique ticket number, then writes one line in the journal per request with how long it took and how it ended. |
| `src/core/exceptions.py` | A neatly-organised family tree of every possible failure the service can raise. Each branch has a stable name and an HTTP status. |
| `src/core/handlers.py` | The complaints desk. Catches any failure — yours, the framework's, or a completely unexpected crash — and turns it into the same consistent response envelope. Never leaks internals to the caller; full stack traces still go to the logs. |
| `src/schemas/teams.py` | The form that describes a valid Teams message: what a banner looks like, what a row looks like, what a button looks like. |
| `src/schemas/admin.py` | The forms for admin and version endpoints. |
| `src/schemas/common.py` | Shared form pieces used by every other form, including the uniform error shape. |
| `src/schemas/enums.py` | The allowed-word lists: "weight may only be lighter / default / bolder", "banner style may only be attention / warning / good / accent / emphasis / default", etc. |
| `config/app.yaml` (+ `.example`) | Non-secret runtime config. Lists named webhooks, timeouts, CORS, defaults. Mountable into the container. |
| `.env` (+ `.example`) | Secrets and deployment values. Default webhook URL, admin key, log level. Gitignored. |
| `Dockerfile` | Recipe for building the container image. |
| `docker-compose.yml` | How to run the container on the server (port mapping, volume mounts, restart policy, healthcheck). |
| `.github/workflows/ci.yml` | The automatic pipeline: on every push to `main`, run tests, and if *code* changed, build the image, push it to the registry, and deploy to EC2. |
| `tests/` | 58 tests — card rendering, both send endpoints (card + text), the 202 enqueue contract, per-webhook queue **pacing + independence + queue-full**, retry/exception mapping (service level), admin-key rules, config reload. |
| `artifacts/` | Historical reference only. Contains the original one-file script this whole project grew out of, plus a ready-made smoke-test payload. |

---

## Features

- **High-level message DSL** — describe banners, rows (left / right / both), buttons, inline markdown links; the service builds the Adaptive Card JSON for you.
- **Plain-text path** — `POST /api/v1/teams/text` sends `{"text": "..."}` as a plain Teams message (for a Power Automate "Post message" flow). Same webhook selection + same queue as cards.
- **Per-webhook outbound queue (fire-and-forget)** — both endpoints **enqueue and return `202 queued` instantly**; a background worker per webhook URL paces sends (≥ `per_webhook_min_interval_seconds`, default 0.5s) so a burst to one webhook isn't throttled/dropped by Power Automate / Teams. Different webhooks run in parallel; a full queue returns `503 WEBHOOK_QUEUE_FULL`.
- **API versioning** — everything lives under `/api/v1/...`.
- **Config from `.env` + YAML** — both files are mountable as Docker volumes and reloadable without a restart.
- **Typed exception hierarchy** — every failure gets a stable `code` and a uniform `ErrorResponse` envelope.
- **Retry with exponential backoff** on timeouts, network errors, and downstream 5xx (never on 4xx).
- **Structured JSON logging** with `X-Request-ID` correlation on every request and log line.
- **OpenAPI / Swagger UI** out of the box with descriptions on every field.
- **Health / readiness probes** for orchestrators.
- **Admin endpoints** (X-Admin-Key gated) to reload config and inspect the current settings (with secrets masked).
- **Containerised + automated deploy** — `docker compose` on the host, pushes to `main` trigger a GitHub Actions pipeline that builds and deploys only when code actually changed.

---

## Quickstart (local)

```bash
# 1. Install deps
uv sync

# 2. Configure
cp .env.example .env
#   set DEFAULT_TEAMS_WEBHOOK_URL and ADMIN_API_KEY
cp config/app.yaml.example config/app.yaml
#   optional: add entries under teams.named_webhooks

# 3. Run
uv run python main.py
#   or: uv run uvicorn src.main:app --reload

# 4. Open docs
# http://localhost:8000/docs
```

---

## Production deployment

Deployment is automated by [.github/workflows/ci.yml](.github/workflows/ci.yml):

1. Every push to `main` runs the full test suite.
2. A paths filter checks whether **code** changed (anything under `src/`, `main.py`, `pyproject.toml`, `uv.lock`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/**`, or `config/app.yaml.example`). README-only and other docs-only pushes skip the build+deploy jobs.
3. When code did change, the image is built and pushed to GHCR.
4. The deploy job SSHes into the EC2 host, does `git reset --hard origin/main`, `docker compose pull`, `docker compose up -d`, and then probes `/api/v1/health` until the container reports healthy.

The container binds to `127.0.0.1:8014` on the host — not directly exposed to the internet. Put it behind nginx (or your preferred reverse proxy) when you need public access.

Required repo secrets (set once via `gh secret set`):

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | EC2 public IP / DNS |
| `DEPLOY_USER` | SSH user (e.g. `ubuntu`) |
| `DEPLOY_PORT` | SSH port (optional; default `22`) |
| `DEPLOY_SSH_KEY` | contents of the private key |
| `DEPLOY_GIT_PATH` | absolute path on the server where the repo is cloned |
| `GHCR_USER` | GitHub username (lowercase) |
| `GHCR_TOKEN` | PAT with `read:packages` (pull) + `write:packages` (push) |

---

## Repository layout

```
.
├── artifacts/                 # historical — original CLI + smoke-test payload
├── config/
│   ├── app.yaml               # non-secret runtime config (gitignored, volume-mountable)
│   └── app.yaml.example
├── src/
│   ├── main.py                # create_app() FastAPI factory + lifespan
│   ├── api/
│   │   ├── deps.py            # DI: settings, TeamsService, admin auth
│   │   └── v1/
│   │       ├── router.py      # composes everything under /api/v1
│   │       └── endpoints/     # teams, health, admin, meta
│   ├── core/
│   │   ├── config.py          # Settings + YAML source + reload
│   │   ├── logging.py         # JSON / pretty formatters
│   │   ├── middleware.py      # RequestID + access log
│   │   ├── exceptions.py      # typed AppError hierarchy
│   │   └── handlers.py        # global exception handlers
│   ├── schemas/               # Pydantic models with Field descriptions
│   └── services/
│       ├── teams.py           # render_card + resolve_webhook_url + post_rendered (retry/exception mapping)
│       └── dispatcher.py      # per-webhook queue + paced background workers (fire-and-forget)
├── tests/                     # 58 tests
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/ci.yml
├── main.py                    # thin root launcher -> uvicorn
└── pyproject.toml
```

---

## API surface

All endpoints are under `/api/v1`.

| Method | Path                   | Purpose                                             |
|-------:|------------------------|-----------------------------------------------------|
|   GET  | `/health`              | Liveness probe (always 200 while the process is up) |
|   GET  | `/health/ready`        | Readiness probe (200 once the lifespan has run)     |
|   GET  | `/version`             | Returns `{name, version}` from settings             |
|  POST  | `/teams/messages`      | Enqueue an **Adaptive Card** for a Teams webhook → `202 queued` |
|  POST  | `/teams/text`          | Enqueue a **plain-text** message (`{"text": ...}`) → `202 queued` |
|  POST  | `/admin/reload-config` | Reload `.env` + YAML from disk (needs `X-Admin-Key`) |
|   GET  | `/admin/config`        | Current settings, secrets masked (needs `X-Admin-Key`) |

> **Both send endpoints are fire-and-forget:** they return `202 {"status":"queued"}` once the message is accepted into the per-webhook queue. The POST to Teams happens later in the background, paced. Failures *after* the 202 are logged server-side, not returned.

### POST `/api/v1/teams/messages`

Minimal payload:

```json
{
  "title": {"text": "Hello from the microservice"}
}
```

Rich payload with every feature exercised:

```json
{
  "banner": {"text": "SYSTEM DEGRADED", "style": "attention", "bold": true},
  "title":  {"text": "Stroke workflow alert", "weight": "bolder", "size": "medium"},
  "rows": [
    {"left": {"text": "Ticket"}, "right": {"text": "#5432"}},
    {"left": {"text": "Age"},    "right": {"text": "67 minutes"}, "separator": true},
    {"left": {"text": "See [the ticket](https://desk.zoho.com/ticket/5432)."}}
  ],
  "buttons": [
    {"title": "Open Ticket", "url": "https://desk.zoho.com/ticket/5432"}
  ],
  "webhook_target": "superstat"
}
```

Webhook selection priority:

1. `webhook_url` — one-off override on the request.
2. `webhook_target` — look up in `config/app.yaml` -> `teams.named_webhooks`.
3. `DEFAULT_TEAMS_WEBHOOK_URL` from `.env`.

Response (both endpoints): `202` with `{"message_id", "sent_at", "webhook_host", "status": "queued"}`. `sent_at` is the **enqueue** time, not the actual send.

### POST `/api/v1/teams/text`

Plain text (no card). Posts `{"text": <text>}` to the webhook — for a Power Automate **"Post message in a chat or channel"** flow that maps the body's `text` field (e.g. `trigger().outputs.body.text`) into the message:

```json
{
  "text": "Stroke alert: accession COCSNV0001 unassigned for 2 min.\nPlease pick it up.",
  "webhook_target": "superstat"
}
```

Same webhook-selection priority as `/messages`. Teams renders light Markdown (bold, italics, links); `\n` newlines are preserved. Max ~28 KB (the Teams message size limit).

### Per-webhook outbound queue & pacing

Both endpoints enqueue onto a **per-webhook queue** (`src/services/dispatcher.py`) and return `202` immediately. A background worker per webhook URL drains its queue on a **slot clock** — one send every `per_webhook_min_interval_seconds` (default 0.5s) — and **spawns** each POST so the cadence holds even when a POST is slow. Different webhooks are fully independent (own queue, worker, clock → parallel).

Why: Power Automate / Teams throttles bursts to a single webhook (the Flow-bot "Post message / card" action is capped at **25 per 5 minutes**; concurrent posts get dropped). Pacing keeps us from flooding it. On shutdown the dispatcher cancels workers and logs any undrained items; if a queue hits `per_webhook_queue_maxsize` the enqueue returns `503 WEBHOOK_QUEUE_FULL`.

> Note: 0.5s pacing prevents *burst-concurrency* drops but does not raise the connector's 25-msg/5-min cap — that's a Microsoft-side limit on the Flow-bot post action. At real load (~1 message per event per webhook) you never approach it. For genuine high volume, aggregate into one message or use a Graph/bot integration.

---

## What the card DSL supports

| DSL feature              | Adaptive Card primitive used                     |
|--------------------------|--------------------------------------------------|
| Row with left + right    | `ColumnSet` with `stretch` + `auto` columns      |
| Bold / size / color      | `TextBlock.weight`, `.size`, `.color`            |
| Banner (themed colors)   | `Container{style: attention/warning/good/accent/emphasis}` |
| Button opening a URL     | `Action.OpenUrl`                                 |
| Inline clickable link    | Markdown inside TextBlock: `[label](https://...)`|
| Separator line above row | `separator: true`                                |

---

## Error contract

Every non-2xx response uses the same envelope:

```json
{
  "error": {
    "code":    "WEBHOOK_REJECTED",
    "message": "Teams rejected the request.",
    "details": {"status": 400, "body_excerpt": "..."}
  },
  "request_id": "c5b1f...-..."
}
```

| Code                    | HTTP | Meaning                                         |
|-------------------------|-----:|-------------------------------------------------|
| `VALIDATION_ERROR`      |  422 | Request body failed schema/validator            |
| `UNKNOWN_WEBHOOK_TARGET`|  400 | Named webhook not configured                    |
| `WEBHOOK_QUEUE_FULL`    |  503 | This webhook's outbound queue is at capacity     |
| `ADMIN_KEY_MISSING`     |  503 | Server has no ADMIN_API_KEY set                 |
| `ADMIN_KEY_INVALID`     |  401 | Wrong or missing X-Admin-Key                    |
| `CONFIG_INVALID`        |  500 | Malformed YAML at reload time                   |
| `INTERNAL_ERROR`        |  500 | Anything unexpected (full trace logged, generic message returned) |

> **Delivery errors are no longer returned to the caller.** Because delivery is queued/fire-and-forget, the webhook failures `WEBHOOK_TIMEOUT` / `WEBHOOK_NETWORK_ERROR` / `WEBHOOK_REJECTED` (4xx) / `WEBHOOK_SERVER_ERROR` (5xx) now happen in the background worker and are **logged server-side** (`webhook_delivery_failed`), not surfaced in the HTTP response. The response only carries errors that occur *before* the 202 (above).

---

## Configuration reference

### `.env`

| Variable                      | Default             | Purpose                                         |
|-------------------------------|---------------------|-------------------------------------------------|
| `DEFAULT_TEAMS_WEBHOOK_URL`   | —                   | Fallback webhook when request omits a target    |
| `ADMIN_API_KEY`               | —                   | Required on `X-Admin-Key` for `/admin/*`        |
| `LOG_LEVEL`                   | `INFO`              | `DEBUG` / `INFO` / `WARNING` / `ERROR`          |
| `LOG_FORMAT`                  | `json`              | `json` (prod) or `pretty` (dev)                 |
| `HTTPX_TIMEOUT_SECONDS`       | `15`                | Per-request outbound timeout                    |
| `WEBHOOK_MAX_RETRIES`         | `2`                 | Retries for timeouts / network / 5xx only       |
| `PER_WEBHOOK_MIN_INTERVAL_SECONDS` | `0.5`         | Min spacing between consecutive POSTs to the **same** webhook (per-webhook queue pacing) |
| `PER_WEBHOOK_QUEUE_MAXSIZE`   | `1000`              | Max pending items per per-webhook queue; over this, enqueue → `503` |
| `CORS_ALLOW_ORIGINS`          | `["*"]`             | JSON list (via pydantic-settings)               |
| `ENV_FILE`                    | `./.env`            | Override path (for Docker volume mounts)        |
| `CONFIG_FILE`                 | `./config/app.yaml` | Override YAML path                              |

### `config/app.yaml`

```yaml
teams:
  named_webhooks:
    superstat: "https://..."
  defaults: { banner_style: attention, title_weight: bolder, title_size: medium }
http:
  timeout_seconds: 15
  max_retries: 2
  per_webhook_min_interval_seconds: 0.5   # pace sends to the same webhook
  per_webhook_queue_maxsize: 1000         # backpressure -> 503 when full
api:
  cors:
    allow_origins: ["*"]
```

### Reload without restart

Edit either file, then:

```bash
curl -X POST http://localhost:8000/api/v1/admin/reload-config \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```

The response lists which sources contributed (`env`, `dotenv`, `yaml`).

---

## Testing

```bash
uv run pytest
```

The suite covers:
- card rendering (every DSL permutation -> expected JSON)
- both send endpoints — card (`/messages`) + plain text (`/text`) — the **202 enqueue contract**, webhook resolution, queue-full `503`
- the **dispatcher**: exact pacing to one webhook, per-webhook independence (parallel), queue-full → `WebhookQueueFull`, shutdown-rejects
- webhook failure/retry mapping (timeout / network / 4xx / 5xx) at the **service level** (`TeamsService.send`), where that logic now runs for the queue worker
- admin-key enforcement
- config reload from a mutated YAML on disk
- uniform error envelope + internal-error leak prevention

---

## The preserved CLI

The original minimal CLI script is at [artifacts/main.py](artifacts/main.py) and still works as a one-off smoke test against a webhook URL. A ready-made payload for a rich smoke-test is at [artifacts/smoke_rich.json](artifacts/smoke_rich.json).

---

## Deployment & observability (EC2)

Production runs as a Docker container on a single EC2 host, deployed by GitHub Actions, observed by a shared Prometheus + Grafana + Tempo + Loki stack. Local dev is unaffected.

### Containerization
- **`Dockerfile`** — `python:3.12-slim`; deps via `uv sync --frozen --no-dev --no-install-project` from `pyproject.toml` + `uv.lock` (no dep drift across local/CI/prod; replaced an earlier hand-curated `pip install` list that silently went stale). `CMD` is wrapped with `opentelemetry-instrument` (inert unless `OTEL_*` env vars are set).
- **`docker-compose.yml`** references the CI-built GHCR image **`ghcr.io/ansimran/instant_messages_microservice:latest`** with a `build:` fallback.
- **`.dockerignore`** excludes secrets, tests, docs, `.github/`.

### CI/CD — `.github/workflows/ci.yml`
On push to `main`: **test** → **build-and-push** (GHCR, registry-cached) → **deploy** (SSH to EC2, `git reset --hard origin/main`, `docker login ghcr.io`, `docker compose pull && up -d`, health-check). Secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `GHCR_USER`, `GHCR_TOKEN` (+ `DEPLOY_GIT_PATH` where used). Docs-only pushes skip via `paths-ignore`.

### EC2 topology
Container joins external Docker network **`observability-net`** (services resolve each other by name; Prometheus scrapes them). An EC2-side `docker-compose.override.yml` (gitignored, not in this repo) injects `OTEL_*` env vars + `WLS_LOG_FILE` and joins that network; committed compose stays environment-agnostic. Container name on EC2: **`instant-messages`**, internal port **`8000`**.

### Observability
- **Phase 1 — metrics:** `/metrics` via `prometheus-fastapi-instrumentator`; Prometheus scrape job / `OTEL_SERVICE_NAME`: **`instant-messages`**.
- **Phase 2 — traces + logs:** `opentelemetry-instrument` auto-instruments FastAPI + httpx → OTLP → OTel Collector → **Tempo**. JSON logs → `WLS_LOG_FILE` → **Promtail** → **Loki**; `OTEL_PYTHON_LOG_CORRELATION=true` adds `otelTraceID` for trace⇄log jumps. Explicit `opentelemetry-instrumentation-fastapi/-httpx/-logging` are pinned in `pyproject.toml` (uv venvs ship without `pip`, so `opentelemetry-bootstrap -a install` silently no-ops).
