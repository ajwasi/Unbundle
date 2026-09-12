# Unbundle

Self-hosted web UI for [humble-cli](https://github.com/smbl64/humble-cli): browse your
Humble Bundle library, download and track where every file ends up, cross-reference
Steam/GOG keys against your real connected accounts to flag unredeemed ones, and see
what's currently for sale without leaving your own library behind. Single-user,
Docker-based, no external services required beyond the ones you choose to connect.

## Features

- **Bundles** — every purchased bundle, downloadable subproducts kept structurally
  separate from third-party (Steam/GOG) keys, search/sort/filter, avg and total price
  per bundle, per-item and bulk download with live progress polling.
- **Downloads** — one page for every download across every bundle: what's currently
  running, full history with filters, and destination routing rules so completed files
  move automatically to a folder based on their format and (optionally) a tag — e.g.
  PDFs land in one place by default, but PDFs tagged "Comic" land somewhere else. A
  folder-scan tool marks files that already exist on disk (from before this feature, or
  downloaded outside the app) as tracked without re-downloading or moving them. See
  Downloads below.
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
  on humblebundle.com. Each unredeemed row also flags whether you might already own
  the same game under a different Steam listing (a re-release/edition with a
  different App ID) or on GOG instead, plus the key's expiration date if it has
  one — an expired row is highlighted so it's obvious at a glance.
- **GOG** — same idea for GOG, with an important caveat: GOG's OAuth requires a
  manual paste-the-redirect-URL flow (no public API/callback registration exists), and
  very few Humble bundles grant GOG keys at all — matched by title against your synced
  GOG library (Humble's API essentially never provides a GOG product ID to match on
  directly, unlike Steam) — see the GOG card in Settings for specifics before expecting
  much here. Its unredeemed table gets the same different-listing/owned-on-Steam and
  expiration treatment as the Steam page.
- **Auth** — a one-time setup screen on first launch requires setting a password
  before the app is usable at all, or OIDC/SSO (Authentik, Keycloak, etc.) configured
  from Settings afterward, with password login optionally turned off once SSO is
  verified working. An emergency CLI recovery tool (see below) covers both "locked
  out of SSO" and "forgot the password." The top-right user menu has your display
  identity, a link to change your password/email (Settings), the dark-mode toggle,
  and Log out.
- **Backups** — automatic daily backups on a schedule you set from Settings (how many
  to keep, what time UTC), a manual "Back up now," one-click download of any existing
  backup, and upload-to-restore for disaster recovery. See Backups below.
- **Version tracking** — the sidebar shows the exact release tag (`v1.2.3`) for a
  published image, or the short git commit SHA for anything else (a plain `main`
  build, local dev) — see "Deploying a published image" above for how a release is
  actually tagged. A periodic background check against the highest `vX.Y.Z` tag in
  this repo surfaces an "Update available" link straight to that tag on GitHub when
  a newer one exists, plus a matching `unbundle_update_available` metric for your
  own alerting.

## Quick start

```bash
cp .env.example .env
# edit .env: set APP_PASSWORD to a real value (APP_SECRET_KEY can be left
# blank — see Security notes below)
docker compose up --build
```

Then open http://localhost:8010, log in, go to **Settings**, and paste your Humble
Bundle session cookie (`_simpleauth_sess`) from a logged-in humblebundle.com browser tab —
see [Finding your session key](https://github.com/smbl64/humble-cli/blob/master/docs/session-key-chrome.md)
(same value humble-cli itself uses). This isn't a username/password login — it's the raw
browser cookie, and there's no automated refresh; if it's ever rejected, paste a fresh one.
Steam and GOG are both optional and connected the same way, from their own Settings cards.

### Try it without connecting accounts

```bash
docker compose --profile demo up --build
```

with `DEMO_MODE=true` set on the `app` service (`.env` or inline). This starts an extra
`mock-api` service — a fake Humble/Steam/GOG backend seeded with a curated,
privacy-scrubbed subset of a real library (`mock_api/`,
`scripts/generate_mock_data.py`) — and points the app's connectors at it instead of the
real APIs. Log in, then use the exact same buttons as normal: "Refresh from Humble" on
Bundles, "Save & test" on Steam with any placeholder key, and GOG's Settings card shows
a "Connect with demo data" button in place of the real login link (GOG's real flow needs
an actual external browser login, which can't be faked). Nothing here touches a real
account; `mock-api` isn't reachable from outside the compose network.

### Using PostgreSQL instead of SQLite

SQLite is the default and needs no setup. To use PostgreSQL instead, set
`POSTGRES_PASSWORD` in `.env`, then start both compose files together:

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up --build
```

`docker-compose.postgres.yml` is an override, not a separate stack — it adds
a `postgres` service and points `app` at it; the `demo`/`observability`
profiles above still work the same way layered on top. See
[Backups](#backups) for a caveat and [Local development](#local-development)
for running against Postgres outside Docker.

### Deploying a published image instead of building from source

`docker compose up --build` above builds the image locally, which needs this
repo checked out. For a host that shouldn't need the source at all (a
homelab server, a Portainer stack), pull a released image from GitHub
Container Registry instead:

```bash
# copy docker-compose.image.yml and .env (from .env.example) to the host, then:
docker compose -f docker-compose.image.yml pull
docker compose -f docker-compose.image.yml up -d
```

`ghcr.io/ajwasi/unbundle` is built for both `amd64` and `arm64` and published
by `.github/workflows/docker-publish.yml` whenever a version tag (`vX.Y.Z`)
is pushed to this repo — pushing to `main` alone doesn't publish anything.
The full test suite runs first and gates the build: a tag whose tests fail
never reaches ghcr.io. To cut a release:

```bash
git tag v1.0.0
git push origin v1.0.0
```

That tags both `:latest` and the exact version (`:v1.0.0` and `:1.0.0`), so a
deployment can pin a specific version instead of always tracking `:latest` if
it wants to. **One-time step after the very first tag/publish**: a new ghcr.io
package defaults to private, which would otherwise require `docker login
ghcr.io` on every host that pulls it — go to the package's page on GitHub
(under the repo, or your account's Packages tab) → Package settings → change
visibility to Public, so a plain `docker compose pull` works with no
authentication.

**Deploying as a Portainer stack (no `.env` file needed):** pasting
`docker-compose.image.yml` into Portainer's stack editor works without a
real `.env` file sitting next to it — this app's compose files don't rely
on `env_file` for anything. Set `APP_SECRET_KEY` and `APP_PASSWORD` (and
`DATABASE_URL` if you want Postgres instead of the SQLite default) directly
in Portainer's own **Environment variables** section on the stack instead;
everything else already has a container-correct default baked into the
compose file itself, so a stack with zero environment variables set still
starts correctly rather than crashing on `alembic upgrade head`.

**Set `DATA_DIR_HOST` to an absolute path in Portainer.** The volumes:
section's default (`./data`, relative to wherever compose runs) is fine for
a plain CLI deployment, but is a real data-loss trap in Portainer: a
stack's relative paths resolve against Portainer's *own* internal stack
directory, not a stable host path you control. Redeploying the stack (not
just bumping the image tag — recreating it, or Portainer regenerating its
internal directory) can silently point `./data` at a brand-new empty
folder, which looks exactly like a fresh install: the setup/login screen
comes back and every bundle, credential, and setting appears gone, even
though nothing was actually deleted — it's just no longer looking at the
same folder. Set `DATA_DIR_HOST` (and `SCAN_ROOT`/`SCAN_ROOT_COMICS`/etc.,
same risk) to an absolute host path instead — e.g.
`/volume1/docker/unbundle/data` on a Synology — in the stack's Environment
variables section, so every redeploy keeps pointing at the same real
folder regardless of what Portainer does internally.

## Security notes

This app defaults to safe behavior but will tell you loudly if you haven't finished
configuring it — check container logs on startup, and watch for a banner across the
top of every page:

- **`APP_SECRET_KEY` encrypts every stored credential** (Humble cookie, Steam key, GOG
  token, OIDC client secret) at rest via Fernet, keyed off this value through PBKDF2
  (200k rounds) rather than a fast hash. You don't need to set this yourself: leave it
  unset and the app generates a real random value the first time it boots, saving it to
  `<DATA_DIR>/.secret_key` (`./data/.secret_key` by default) so later restarts reuse the
  same one — the container logs this exactly once, the first time it happens. Set it
  explicitly only if you'd rather manage the value yourself (env var / `.env` / compose
  file); an explicit value always wins over the auto-generated one.
- **First launch always requires setting a password.** Every request redirects to a
  one-time `/setup` screen until either a password is set there (or via
  `set-password`/`APP_PASSWORD`, see below) or OIDC is configured — there's no way to
  run this app wide open.
- **Rotating `APP_SECRET_KEY` after a suspected leak** requires re-encrypting every
  stored credential first, or they become permanently undecryptable the moment the key
  changes:
  ```bash
  docker exec -it <container> python -m app.cli rotate-secret-key
  ```
  If you're using the auto-generated key (the default), this mints a fresh random one,
  re-encrypts everything under it, and updates `<DATA_DIR>/.secret_key` in place — just
  restart the app afterward, nothing else to configure. If you set `APP_SECRET_KEY`
  yourself, it instead prompts you for a new value and re-encrypts under that — follow
  the order it prints exactly (re-encrypt, *then* update the env var and restart), since
  doing it the other way around locks out every stored credential.
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

> **Running on PostgreSQL?** This section doesn't apply — the Backups card in
> Settings will show that in-app backups aren't available on this database
> backend. Use `pg_dump`/`pg_dumpall` or your own external PostgreSQL backup
> strategy instead; nothing here has a Postgres equivalent.

The Backups card in Settings handles this in-app: enable a daily backup at whatever
time (UTC) you choose, set how many to keep, and older ones are pruned automatically.
"Back up now" runs one immediately; each existing backup has a one-click download; and
uploading a `.db` file back through the same card restores it — the live database is
automatically backed up right before the swap, so a restore is itself undo-able from
the same list a moment later. Backups land in `./data/backups/` and use the same
atomic `sqlite3` backup API (safe against a live, in-use database) as the manual
approach below.

One thing worth getting right regardless of how a backup was made: **the secret key
needs to travel with the database, not just the database itself.** Every stored
credential is encrypted with a key derived from it (see Security notes above) — a
`.db` file restored without the matching secret key lists your bundles fine but can't
decrypt any saved Humble/Steam/GOG/OIDC credentials; you'd have to reconnect
everything from scratch.

If you're using the auto-generated key (no `APP_SECRET_KEY` set explicitly — see
Security notes), it already lives at `./data/.secret_key`, right next to
`./data/humble.db` — a manual backup of the whole `./data` directory (see below)
covers both automatically. The in-app Backups card above only backs up the database
file itself, not `.secret_key`, so if you rely on that card alone, also back up
`./data/.secret_key` separately (or set `APP_SECRET_KEY` explicitly and back up
*that* value instead, wherever you've configured it).

`./data/humble.db` (and `./data/backups/`) are ordinary files on the host — a plain
bind mount, not a Docker-managed volume — so any external file-backup tool works too,
with no app-specific export needed, e.g. for an off-box copy the in-app feature
doesn't do on its own:
```bash
docker exec <container> python -c \
  "import sqlite3; sqlite3.connect('/data/humble.db').backup(sqlite3.connect('/data/humble.db.bak'))"
```

## Downloads

The **Downloads** page (sidebar) covers everything about files after they're
requested: what's downloading right now, full history across every bundle, and where
completed files actually end up.

humble-cli has no per-item output-directory flag — every file always lands at
`./data/downloads/<bundle>/<item>/<filename>` first. Humble's own filenames tend to
concatenate words with underscores (often encoding volume/part numbering, e.g.
`some_book_vol_02.epub`) — once a download verifies successfully, this app renames it
in place to swap underscores for spaces (`some book vol 02.epub`), before any
relocation below. This is a pure character substitution (never merges two differently-
named files together) and only touches the file's own name on disk — nothing about how
it's matched against your library changes. Type/tag-based routing is
therefore a move-after-the-fact: a **Destination** rule (name, comma-separated
formats, an optional tag, and a target path) says where a completed file should be
relocated to once it's verified on disk. Precedence, most specific first:

1. A rule with both a tag and a format match — for a format that's ambiguous on its
   own (PDF is a common one: comics vs. regular books), tag the comic bundle/item and
   give it its own rule.
2. A rule with a format match and no tag.
3. A rule with no tag and no formats — the default location for anything not covered
   by a more specific rule.
4. No matching rule — the file stays right where humble-cli put it.

The target path is whatever the container can see, including an already-mounted
network drive (mount it into the container via `docker-compose.yml` and point a
destination's path at the mount, e.g. `/mnt/library/comics`) — this app doesn't manage
the mount itself, just moves files onto it.

**Scan a folder** reconciles files that predate this feature (or were placed there
some other way): point it at a folder and it matches filenames against every item in
your library, previews what it found (flagging anything ambiguous — the same filename
matching more than one item — for manual review rather than guessing), and only marks
files as downloaded once you commit — and, like a fresh download, renames an
underscore-heavy match to a readable one at that point (never during preview, which
never touches disk). On a large, long-lived library the preview can turn up thousands
of rows — only the first 200 are actually displayed for either list. Check specific
rows to commit just those, or leave everything unchecked and commit to act on the full
result, not just what's shown — the "select all" checkbox only ever selects the rows
currently on screen, never the rest of a truncated list. Committing removes those
entries from the matched list (they're now tracked, so there's nothing left to review),
which is what surfaces the next page automatically — no extra step needed to work
through a scan with thousands of matches a couple hundred at a time.

### Scanning multiple folders

By default there's one scan root (`SCAN_ROOT`/`SCAN_ROOT_DIR`). To scan more than one
location — separate NAS shares that can't share a common parent directory, for
instance — mount each as its own path in `docker-compose.yml`'s `volumes:` (see that
file's own comment on `SCAN_ROOT`) and list all of them, comma-separated, in `.env`'s
`SCAN_ROOTS`:

```bash
# docker-compose.yml
volumes:
  - ${SCAN_ROOT:-./scan-root}:/scan-root:ro
  - ${SCAN_ROOT_COMICS:-./scan-root-comics}:/scan-root-comics:ro
```
```bash
# .env
SCAN_ROOTS=/scan-root,/scan-root-comics
```

The scan form then shows a dropdown to pick which root to scan — its label is just
that mount's own last path segment (`/scan-root-comics` → "scan-root-comics"), so name
the container-side path meaningfully. Leaving `SCAN_ROOTS` blank keeps today's
single-root behavior exactly as before.

## Reverse proxy / HTTPS

By default this app assumes plain HTTP on a trusted LAN, same as `audiobook-tracker`.
To put a real domain and HTTPS in front of it:

1. **Stop publishing the app's port to the host** — remove or comment out
   `docker-compose.yml`'s `ports: ["8010:8000"]` under the `app` service once a
   reverse proxy is taking over that job, so the app is only reachable through the
   proxy, not directly.
2. **Set `BEHIND_HTTPS_PROXY=true`** in `.env` — marks the session/CSRF cookies
   `Secure`. Leave this `false` until step 1 is actually done and HTTPS is really in
   front of the app; a `Secure` cookie over plain HTTP means the browser silently
   refuses to send it back, locking out every login.
3. `docker-compose.yml` already sets `FORWARDED_ALLOW_IPS=*` for the app service —
   this tells uvicorn to trust `X-Forwarded-*` headers from anywhere on the Compose
   network (uvicorn's own default only trusts `127.0.0.1`, which a reverse proxy
   running as its own container never connects from). This is safe specifically
   *because* of step 1: nothing else can reach the app directly to spoof those
   headers once its port isn't published.
4. Point your reverse proxy at the app's Compose service name and port
   (`app:8000`, not `localhost:8010` — same internal-network addressing the
   observability stack below already uses). Example configs for Caddy (automatic
   HTTPS from just a domain name) and nginx (bring your own certs, or skip TLS here
   entirely if something like Nginx Proxy Manager/Synology's own reverse proxy/
   Traefik is already terminating it) are in `examples/reverse-proxy/`.

## Observability

`GET /metrics` exposes an OpenTelemetry-instrumented Prometheus scrape endpoint —
reachable without logging in (Prometheus can't do an interactive login), with an
optional `METRICS_TOKEN` bearer check if you want that one route restricted too. It
carries three kinds of data:

- HTTP request count/duration/response size per route, via automatic FastAPI
  instrumentation (`http_server_*`).
- App-specific gauges, computed fresh from the database on every scrape (no
  persisted duplicate state to drift out of sync): `unbundle_bundles_count`,
  `unbundle_downloads_files`/`unbundle_downloads_jobs` (by status),
  `unbundle_entitlements_unredeemed` (by platform — the app's core "did I
  actually redeem this key" purpose, now graphable over time),
  `unbundle_connector_status` (1/0 per source), and
  `unbundle_sync_last_success_timestamp`.
- `unbundle_rate_limit_rejections_total` (by which limiter) and Python's own
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
  - job_name: unbundle
    static_configs:
      - targets: ["<host>:8010"]
    # Only needed if METRICS_TOKEN is set:
    # authorization:
    #   credentials: <the same value as METRICS_TOKEN>
```

From there, build panels/alerts against the metric names above — e.g. an alert on
`unbundle_connector_status{source="humble"} == 0` catches a broken Humble session
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
credentials or network access needed. This is true regardless of which database
you run the app itself against; the automated suite always uses SQLite.

To develop against a real local PostgreSQL instead of SQLite (outside Docker —
see the Quick start section for the Docker Compose route): install Postgres
yourself, create a database, then:

```bash
.venv/Scripts/pip install -e ".[dev,postgres]"
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/humble .venv/Scripts/alembic upgrade head
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/humble .venv/Scripts/uvicorn app.main:app --reload
```

(set `DATABASE_URL` before each command that imports `app.*` — `app/config.py`
reads it once at import time, so setting it and launching a process must
happen together, not in separate steps).

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
- GOG's real-world coverage is low: very few Humble bundles grant a GOG key at all
  (confirmed against a real 550-bundle library: 2 of 1,277 entitlements), so don't
  expect the GOG page to find much regardless of connection — matching itself (by
  title, since Humble essentially never provides a GOG product ID) works fine once a
  bundle actually has one.
- Single-process only by design (`app/downloads/worker.py`'s in-process job queue and
  the rate limiters both hold in-memory state) — never run this with multiple uvicorn
  workers or replicas.
- The folder-scan reconciliation tool (Downloads page) matches by filename alone, not
  content — a renamed file won't be found, and a coincidental filename collision across
  two different library items is reported as ambiguous rather than guessed at. It can
  only see `SCAN_ROOT` (a read-only Docker volume — see `.env.example`) and its
  subdirectories, never an arbitrary path — point it at wherever your existing library
  actually lives.

## Acknowledgments

Unbundle is a web UI, not a reimplementation — it exists on top of tools and
research it didn't create:

- **[humble-cli](https://github.com/smbl64/humble-cli)** (by [smbl64](https://github.com/smbl64))
  is what actually downloads files from Humble Bundle; this app shells out to it for
  every download and mirrors its own session-cookie-based auth flow (see Quick start
  above). `app/connectors/humble_connector.py`'s direct API calls are also modeled on
  humble-cli's own source, since Humble's API itself is undocumented.
- **[GOG API Docs](https://gogapidocs.readthedocs.io/)** is the community
  reverse-engineering effort `app/connectors/gog_connector.py` is built against — GOG
  has no official public API or self-service key, so every request shape, endpoint,
  and the OAuth2 flow's quirks (see that file's module docstring) come from this
  project's documentation, not from GOG.

Both projects deal with undocumented, unofficial surfaces that can change without
notice — see Security notes above.
