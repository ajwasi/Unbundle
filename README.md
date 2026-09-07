# Humble Tracker

Self-hosted web UI for [humble-cli](https://github.com/smbl64/humble-cli): browse your
Humble Bundle library, track where every downloaded file currently lives, and (later)
flag Humble-granted Steam keys you never actually redeemed. Single-user, Docker-based.

**Status: phase 1 (auth + Humble connection) is implemented and testable now. Bundle
browsing, download execution, and location tracking are not built yet** — see Roadmap.

## Quick start

```bash
cp .env.example .env
# edit .env: set APP_SECRET_KEY and APP_PASSWORD
docker compose up --build
```

Then open http://localhost:8010, log in, go to **Settings**, and paste your Humble
Bundle session cookie (`_simpleauth_sess`) from a logged-in humblebundle.com browser tab —
see [Finding your session key](https://github.com/smbl64/humble-cli/blob/master/docs/session-key-chrome.md)
(same value humble-cli itself uses). This isn't a username/password login — it's the raw
browser cookie, and there's no automated refresh; if it's ever rejected, paste a fresh one.

## Known risks

- **Humble's API is undocumented.** `app/connectors/humble_connector.py` parses fields
  (`subproducts`, `download_struct`, `tpkd_dict`/`all_tpks`, `redeemed_key_val`) based on
  humble-cli's own source and widely-used reverse-engineered shapes, not an official
  spec — verify against a real account's response and adjust `_parse_bundle` if fields
  come back differently than expected.
- **No session refresh.** Same limitation as humble-cli itself — the session cookie has
  no fixed expiry but isn't automatically renewed; if calls start failing with an auth
  error, reconnect in Settings.

## Local development

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/alembic upgrade head
.venv/Scripts/pytest
.venv/Scripts/uvicorn app.main:app --reload
```

Tests run against the default dev secret key and don't require a real Humble account.

**Do not point `HUMBLE_CLI_KEY_PATH` (or the `humble_cli_key_path` setting) at your real
home directory during local development** — it defaults to `~/.humble-cli-key`, the exact
path the real `humble-cli` binary itself hardcodes, and this app writes to it whenever a
session key is saved. In Docker this correctly resolves to the container's own
`/home/appuser`; outside Docker, override it to a scratch path so it never overwrites a
real credential file already on the machine.

## Architecture

`app/connectors/` — one module per external source behind a common `BaseConnector`
interface (`humble_connector.py` today; a `steam_connector.py` is planned for the
Steam-key-redemption phase). `app/sync/refresh.py` fetches via the connector and upserts
`Bundle`/`BundleEntitlement` rows — manual, on-demand (`POST /bundles/refresh`), no
background scheduler. `app/models/` — schema: `bundle` ↔ `download` (downloadable
subproducts only) and `bundle` ↔ `bundle_entitlement` (third-party/Steam keys — never
downloadable, tracked separately by construction). `app/security.py` — single
`APP_PASSWORD`-gated session cookie for the web UI itself, and Fernet-at-rest encryption
for the stored Humble session cookie.

## Roadmap

- Bundle browsing (list/search/detail, separating downloadable items from Steam-key
  entitlements)
- Download execution via a subprocess call to the real `humble-cli` binary (reusing its
  resume/retry logic), with per-file location tracking
- Relocating a downloaded file's tracked location (local today; SMB/NAS later, via a
  pluggable `LocationBackend`)
- Phase 2: flagging Humble-granted Steam keys never redeemed on the real Steam account,
  via the Steam Web API
