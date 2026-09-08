# Humble Tracker

Self-hosted web UI for [humble-cli](https://github.com/smbl64/humble-cli): browse your
Humble Bundle library, download and track where every file ends up, cross-reference
Steam/GOG keys against your real connected accounts to flag unredeemed ones, and see
what's currently for sale without leaving your own library behind. Single-user,
Docker-based, no external services required beyond the ones you choose to connect.

## Features

- **Bundles** — every purchased bundle, downloadable subproducts kept structurally
  separate from third-party (Steam/GOG) keys, search/sort/filter, avg and total price
  per bundle, per-item and bulk download with live progress polling.
- **Catalog** — every distinct item across your whole library flattened into one
  searchable table, with duplicate-purchase detection (the same item unlocked by more
  than one bundle).
- **Finance** — spending over time, filterable by year/month/category, with an
  adaptive chart (yearly/monthly/daily granularity depending on the filter).
- **Home** — Humble's current storefront listings (games/books/software), a live
  countdown to each bundle's sale end, and a one-click compare against your own
  Catalog to see what in a for-sale bundle you don't already own.
- **Steam** — connect a Steam Web API key + SteamID, sync your real library, and see
  which Humble-granted Steam keys are unredeemed, with a direct link to redeem them
  on humblebundle.com.
- **GOG** — same idea for GOG, with an important caveat: GOG's OAuth requires a
  manual paste-the-redirect-URL flow (no public API/callback registration exists),
  and very few Humble bundles actually grant GOG keys with a matchable identifier —
  see the GOG card in Settings for specifics before expecting much here.
- **Auth** — a single shared password (`APP_PASSWORD`) by default, or OIDC/SSO
  (Authentik, Keycloak, etc.) configured from Settings, with password login
  optionally turned off once SSO is verified working. An emergency CLI recovery
  tool (see below) covers both "locked out of SSO" and "forgot the password."
- **Backups** — automatic daily backups on a schedule you set from Settings (how many
  to keep, what time UTC), a manual "Back up now," one-click download of any existing
  backup, and upload-to-restore for disaster recovery. See Backups below.

## Quick start

```bash
cp .env.example .env
# edit .env: set APP_SECRET_KEY and APP_PASSWORD to real random values
docker compose up --build
```

Then open http://localhost:8010, log in, go to **Settings**, and paste your Humble
Bundle session cookie (`_simpleauth_sess`) from a logged-in humblebundle.com browser tab —
see [Finding your session key](https://github.com/smbl64/humble-cli/blob/master/docs/session-key-chrome.md)
(same value humble-cli itself uses). This isn't a username/password login — it's the raw
browser cookie, and there's no automated refresh; if it's ever rejected, paste a fresh one.
Steam and GOG are both optional and connected the same way, from their own Settings cards.

## Security notes

This app defaults to safe behavior but will tell you loudly if you haven't finished
configuring it — check container logs on startup, and watch for a banner across the
top of every page:

- **Set a real `APP_SECRET_KEY`.** It encrypts every stored credential (Humble cookie,
  Steam key, GOG token, OIDC client secret) at rest via Fernet, keyed off this value
  through PBKDF2 (200k rounds) rather than a fast hash — but that only helps if the
  value itself isn't the placeholder default or something guessable. Leaving it
  unset/default logs a startup warning and is the one thing worth getting right before
  exposing this beyond your own machine.
- **Set `APP_PASSWORD` or configure OIDC.** An app with neither configured stays fully
  open to anyone who can reach it (unchanged from this project's original default —
  useful for a quick local trial, wrong for anything reachable beyond localhost). A
  startup warning and a site-wide banner both call this out if it's ever true.
- **Rotating `APP_SECRET_KEY` after a suspected leak** requires re-encrypting every
  stored credential first, or they become permanently undecryptable the moment the key
  changes:
  ```bash
  docker exec -it <container> python -m app.cli rotate-secret-key
  ```
  Follow the order it prints exactly (re-encrypt, *then* update the env var and
  restart) — doing it the other way around locks out every stored credential.
- **Locked out?** `docker exec -it <container> python -m app.cli disable-oidc` turns
  off SSO-only mode so password login works again; `python -m app.cli set-password`
  sets a new password directly in the database, no restart needed.
- Login attempts and the various `/*/refresh` endpoints are rate-limited per source IP
  (in-memory, resets on restart) — the former to blunt password brute-forcing, the
  latter so a stuck browser tab or script can't get this app's outbound IP rate-limited
  or blocked by Humble/Steam/GOG.
- Every mutating request requires a CSRF token (double-submit cookie) in addition to
  `SameSite=Lax` on the session cookie.
- This app talks to Humble's, Steam's, and GOG's real APIs using your real credentials.
  Humble's API is undocumented (`app/connectors/humble_connector.py` parses fields
  based on humble-cli's own source and widely-used reverse-engineered shapes); GOG's is
  fully unofficial and community-reverse-engineered (`app/connectors/gog_connector.py`).
  Both could change without notice.

## Backups

The Backups card in Settings handles this in-app: enable a daily backup at whatever
time (UTC) you choose, set how many to keep, and older ones are pruned automatically.
"Back up now" runs one immediately; each existing backup has a one-click download; and
uploading a `.db` file back through the same card restores it — the live database is
automatically backed up right before the swap, so a restore is itself undo-able from
the same list a moment later. Backups land in `./data/backups/` and use the same
atomic `sqlite3` backup API (safe against a live, in-use database) as the manual
approach below.

One thing worth getting right regardless of how a backup was made: **back up
`APP_SECRET_KEY` alongside the database, not just the database.** Every stored
credential is encrypted with a key derived from it (see Security notes above) — a
`.db` file restored without the matching secret key lists your bundles fine but can't
decrypt any saved Humble/Steam/GOG/OIDC credentials; you'd have to reconnect
everything from scratch.

`./data/humble.db` (and `./data/backups/`) are ordinary files on the host — a plain
bind mount, not a Docker-managed volume — so any external file-backup tool works too,
with no app-specific export needed, e.g. for an off-box copy the in-app feature
doesn't do on its own:
```bash
docker exec <container> python -c \
  "import sqlite3; sqlite3.connect('/data/humble.db').backup(sqlite3.connect('/data/humble.db.bak'))"
```

## Observability

`GET /metrics` exposes an OpenTelemetry-instrumented Prometheus scrape endpoint —
reachable without logging in (Prometheus can't do an interactive login), with an
optional `METRICS_TOKEN` bearer check if you want that one route restricted too. It
carries three kinds of data:

- HTTP request count/duration/response size per route, via automatic FastAPI
  instrumentation (`http_server_*`).
- App-specific gauges, computed fresh from the database on every scrape (no
  persisted duplicate state to drift out of sync): `humble_tracker_bundles_count`,
  `humble_tracker_downloads_files`/`humble_tracker_downloads_jobs` (by status),
  `humble_tracker_entitlements_unredeemed` (by platform — the app's core "did I
  actually redeem this key" purpose, now graphable over time),
  `humble_tracker_connector_status` (1/0 per source), and
  `humble_tracker_sync_last_success_timestamp`.
- `humble_tracker_rate_limit_rejections_total` (by which limiter) and Python's own
  process/GC metrics, both via `prometheus_client`'s standard collectors.

**Try it turnkey**: `docker compose --profile observability up` starts Prometheus (already
scraping the app — `examples/observability/prometheus.yml`) and Grafana (already pointed at
that Prometheus as a data source — `examples/observability/grafana-datasource.yml`) alongside
the app itself, all on the same Compose network. Neither starts with a plain `docker compose
up`. Prometheus: http://localhost:9090, Grafana: http://localhost:3000 (default login
`admin`/`admin`, changed on first login). If `METRICS_TOKEN` is set in `.env`, uncomment the
`authorization` block in `examples/observability/prometheus.yml` first.

Scraping from a Prometheus that lives *outside* this Compose network instead:

```yaml
scrape_configs:
  - job_name: humble-tracker
    static_configs:
      - targets: ["<host>:8010"]
    # Only needed if METRICS_TOKEN is set:
    # authorization:
    #   credentials: <the same value as METRICS_TOKEN>
```

From there, build panels/alerts against the metric names above — e.g. an alert on
`humble_tracker_connector_status{source="humble"} == 0` catches a broken Humble session
before you'd otherwise notice.

## Local development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/alembic upgrade head
.venv/Scripts/pytest
.venv/Scripts/uvicorn app.main:app --reload
```

Tests run against a throwaway temp SQLite DB and a test-only secret key — no real
credentials or network access needed.

**Do not point `HUMBLE_CLI_KEY_PATH` (or the `humble_cli_key_path` setting) at your real
home directory during local development** — it defaults to `~/.humble-cli-key`, the exact
path the real `humble-cli` binary itself hardcodes, and this app writes to it whenever a
session key is saved. In Docker this correctly resolves to `/root/.humble-cli-key` (the
container runs as root, with `HOME=/root` set explicitly in the Dockerfile); outside
Docker, override it to a scratch path so it never overwrites a real credential file
already on the machine.

## Architecture

`app/connectors/` — one module per external source behind a common `BaseConnector`
interface where applicable: `humble_connector.py` (authenticated, official-ish API),
`storefront.py` (unauthenticated, scrapes embedded JSON off Humble's public marketing
pages — separate from the connector interface since it describes for-sale bundles, not
owned ones), `steam_connector.py` (Steam Web API), `gog_connector.py` (unofficial OAuth,
fixed non-configurable redirect URI — see its docstring for why). `app/sync/` fetches via
a connector and upserts rows — all manual/on-demand (`POST .../refresh` buttons), no
background scheduler anywhere in this app. `app/models/` — `bundle` ↔ `download`
(downloadable subproducts, tracked with predicted-path verification against the real
filesystem, never by parsing humble-cli's stdout) and `bundle` ↔ `bundle_entitlement`
(third-party keys — Steam/GOG — matched against `steam_game`/`gog_game` rows synced
separately). `app/security.py` / `app/csrf.py` / `app/ratelimit.py` — session cookie,
Fernet-at-rest credential encryption, CSRF, and rate limiting, each a small standalone
module rather than folded into one another. `app/telemetry.py` — OpenTelemetry meter
setup and the custom gauges/counters behind `/metrics` (see Observability above).
`app/cli.py` — emergency recovery, run directly against the database with no
HTTP/session involved.

## Known limitations

- No session refresh for the Humble cookie, same as humble-cli itself — if calls start
  failing with an auth error, reconnect in Settings.
- GOG's real-world coverage is low: most bundles that include GOG keys don't expose a
  matchable identifier in Humble's API, so don't expect the GOG page to find much even
  once connected.
- Single-process only by design (`app/downloads/worker.py`'s in-process job queue and
  the rate limiters both hold in-memory state) — never run this with multiple uvicorn
  workers or replicas.
