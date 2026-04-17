# microservice-instant-messages

Production-grade FastAPI microservice for sending instant messages to Microsoft Teams (via incoming webhooks) using rich Adaptive Cards. Extensible to other channels.

## Features

- **High-level message DSL** — describe banners, rows (left / right / both), buttons, inline markdown links; the service builds the Adaptive Card JSON for you.
- **API versioning** — everything lives under `/api/v1/...`.
- **Config from `.env` + YAML** — both files are mountable as Docker volumes and reloadable without a restart.
- **Typed exception hierarchy** — every failure gets a stable `code` and a uniform `ErrorResponse` envelope.
- **Retry with exponential backoff** on timeouts, network errors, and downstream 5xx (never on 4xx).
- **Structured JSON logging** with `X-Request-ID` correlation on every request and log line.
- **OpenAPI / Swagger UI** out of the box with descriptions on every field.
- **Health / readiness probes** for orchestrators.
- **Admin endpoints** (X-Admin-Key gated) to reload config and inspect the current settings (with secrets masked).

Docker/compose and CI/CD are intentionally out of scope for this iteration.

## Quickstart

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

## Repository layout

```
.
├── artifacts/                 # preserved original single-script version
├── config/
│   ├── app.yaml               # non-secret runtime config (volume-mountable)
│   └── app.yaml.example
├── src/
│   ├── main.py                # create_app() FastAPI factory + lifespan
│   ├── api/
│   │   ├── deps.py            # DI: settings, TeamsService, admin auth
│   │   └── v1/
│   │       ├── router.py      # composes everything under /api/v1
│   │       └── endpoints/     # teams, health, admin, meta
│   ├── core/
│   │   ├── config.py          # Settings + YAML source + reload_settings()
│   │   ├── logging.py         # JSON / pretty formatters
│   │   ├── middleware.py      # RequestID + access log
│   │   ├── exceptions.py      # typed AppError hierarchy
│   │   └── handlers.py        # global exception handlers
│   ├── schemas/               # Pydantic models with Field descriptions
│   └── services/teams.py      # render_card + send + retry + exception mapping
├── tests/                     # 31 tests (card builder, endpoints, errors, reload)
├── main.py                    # thin root launcher -> uvicorn
└── pyproject.toml
```

## API surface

All endpoints are under `/api/v1`.

| Method | Path                   | Purpose                                             |
|-------:|------------------------|-----------------------------------------------------|
|   GET  | `/health`              | Liveness probe (always 200 while the process is up) |
|   GET  | `/health/ready`        | Readiness probe (200 once the lifespan has run)     |
|   GET  | `/version`             | Returns `{name, version}` from settings             |
|  POST  | `/teams/messages`      | Send an Adaptive Card to a Teams webhook            |
|  POST  | `/admin/reload-config` | Reload `.env` + YAML from disk (needs `X-Admin-Key`) |
|   GET  | `/admin/config`        | Current settings, secrets masked (needs `X-Admin-Key`) |

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

## What the card DSL supports

| DSL feature              | Adaptive Card primitive used                     |
|--------------------------|--------------------------------------------------|
| Row with left + right    | `ColumnSet` with `stretch` + `auto` columns      |
| Bold / size / color      | `TextBlock.weight`, `.size`, `.color`            |
| Banner (themed colors)   | `Container{style: attention/warning/good/accent/emphasis}` |
| Button opening a URL     | `Action.OpenUrl`                                 |
| Inline clickable link    | Markdown inside TextBlock: `[label](https://...)`|
| Separator line above row | `separator: true`                                |

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
| `WEBHOOK_TIMEOUT`       |  504 | httpx timed out                                 |
| `WEBHOOK_NETWORK_ERROR` |  502 | DNS / connect / TLS / read error                |
| `WEBHOOK_REJECTED`      |  502 | Teams returned 4xx (not retried)                |
| `WEBHOOK_SERVER_ERROR`  |  502 | Teams returned 5xx (after retries)              |
| `ADMIN_KEY_MISSING`     |  503 | Server has no ADMIN_API_KEY set                 |
| `ADMIN_KEY_INVALID`     |  401 | Wrong or missing X-Admin-Key                    |
| `CONFIG_INVALID`        |  500 | Malformed YAML at reload time                   |
| `INTERNAL_ERROR`        |  500 | Anything unexpected (full trace logged, generic message returned) |

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

## Testing

```bash
uv run pytest
```

The suite covers:
- card rendering (every DSL permutation -> expected JSON)
- HTTP endpoints (happy path + every error code)
- webhook failure mapping (timeout / network / 4xx / 5xx)
- admin-key enforcement
- config reload from a mutated YAML on disk
- uniform error envelope + internal-error leak prevention

## The preserved CLI

The original minimal CLI script is at [artifacts/main.py](artifacts/main.py) and still works as a one-off smoke test against a webhook URL.
