# Separate stage purely to capture the git commit this image was built from
# (app/version.py reads the resulting VERSION file) — kept out of the final
# stage below so it never needs git installed or .git present in the actual
# runtime image, just the one resulting file.
FROM python:3.12-slim AS version
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
COPY . .
# The exact tag when this build's HEAD is precisely one (a real release,
# published by .github/workflows/docker-publish.yml from a pushed vX.Y.Z
# tag) — falls back to the short commit SHA for anything else (a plain main
# build), same as app/version.py's own local-dev fallback. 2>/dev/null only
# on the tag attempt: its failure ("no tag exactly matches") is the expected,
# common case and not worth alarming the build log with.
RUN (git describe --tags --exact-match HEAD 2>/dev/null || git rev-parse --short HEAD) > VERSION || echo unknown > VERSION

FROM python:3.12-slim
WORKDIR /app

# Without this, Python fully block-buffers stdout/stderr whenever they aren't
# attached to a real terminal — true for every container process, always.
# print()/logging output then sits in an unflushed buffer until it either
# fills up or the process exits, neither of which happens for a long-running
# web server — so `docker logs`/Portainer's log viewer can show nothing at
# all for real output the app already produced. Confirmed live: a
# diagnostic print() added to debug an Audible login issue never appeared in
# Portainer's log view even though the code path that emits it was
# definitely running.
ENV PYTHONUNBUFFERED=1

# Explicit on purpose: Go's os.UserHomeDir() (what humble-cli uses to find
# ~/.humble-cli-key) requires $HOME to be set and does NOT fall back to
# /etc/passwd if it's missing — unverified whether python:3.12-slim's default
# shell environment reliably exports HOME to a non-interactive `sh -c` CMD,
# since Docker Desktop isn't installed on the dev machine this was written on
# to test directly. Setting it removes that ambiguity; this must stay in sync
# with app.config.Settings.humble_cli_key_path's default (Path.home()), which
# resolves via the same variable, and with the appuser home dir created below
# (gosu preserves the environment, including this, when it drops to appuser).
ENV HOME=/home/appuser

# The unprivileged user the app actually runs as (see docker-entrypoint.sh) —
# everything from `alembic upgrade head` through uvicorn and every humble-cli
# invocation runs as this user, never root. 1000:1000 matches the "first
# regular user" UID/GID convention most bind-mount and NFS/CIFS setups
# already assume (this repo's own README CIFS-mount example sets
# uid=1000,gid=1000 explicitly), minimizing the chance of a permission
# mismatch against a host directory that isn't world-writable.
RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser -m -d /home/appuser -s /usr/sbin/nologin appuser

ARG HUMBLE_CLI_VERSION=v0.23.2
ARG TARGETARCH

# smbl64/humble-cli publishes no checksums.txt or signature for its releases
# (confirmed 2026-09-07 against the real v0.23.2 release assets via the GitHub
# API — no checksum manifest, nothing in the release notes either), so these
# are self-computed against the exact bytes of the exact pinned version above.
# Bumping HUMBLE_CLI_VERSION means recomputing and updating both of these.
ARG HUMBLE_CLI_SHA256_AMD64=026f2b9a5c4594a51e66ef3d249e28dead50f2494e931738c7ea84e8ac44660a
ARG HUMBLE_CLI_SHA256_ARM64=870b5fad2376a4a58b69b1cd55776a159a9a66b559dea5c252cb29812b83f694

# gosu (not su/sudo — both need a PAM/TTY setup this minimal image doesn't
# have, and su in particular re-execs a full login shell) is what
# docker-entrypoint.sh uses to drop from root to appuser for the real process.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gosu \
    && rm -rf /var/lib/apt/lists/*

# curl is kept in the final image on purpose: build-time binary fetch AND a future
# runtime HEALTHCHECK. humble-cli is only ever shelled out to for the actual
# download/bulk-download step (see app/downloads/runner.py) — all browsing goes
# through app/connectors/humble_connector.py's direct API calls instead.
#
# Release assets are .tar.gz containing one arch-suffixed file (e.g.
# humble-cli-linux-amd64), not a bare "humble-cli" binary (confirmed 2026-09-06
# via `tar -tzvf` against the real release — an earlier version of this
# Dockerfile downloaded the untarred URL directly, which 404s; never verified
# until now since Docker Desktop isn't installed on the dev machine this was
# written on).
RUN case "${TARGETARCH}" in \
      amd64) expected="${HUMBLE_CLI_SHA256_AMD64}" ;; \
      arm64) expected="${HUMBLE_CLI_SHA256_ARM64}" ;; \
      *) echo "No pinned humble-cli checksum for TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL \
      "https://github.com/smbl64/humble-cli/releases/download/${HUMBLE_CLI_VERSION}/humble-cli-linux-${TARGETARCH}.tar.gz" \
      -o /tmp/humble-cli.tar.gz \
    && echo "${expected}  /tmp/humble-cli.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/humble-cli.tar.gz -C /tmp \
    && mv "/tmp/humble-cli-linux-${TARGETARCH}" /usr/local/bin/humble-cli \
    && rm /tmp/humble-cli.tar.gz \
    && chmod +x /usr/local/bin/humble-cli \
    && /usr/local/bin/humble-cli --version

COPY pyproject.toml ./
# [postgres] is always installed — one shared image supports either backend
# unconditionally, since switching is just a DATABASE_URL change (see
# docker-compose.postgres.yml), never a rebuild. psycopg[binary] adds no
# apt-get packages of its own (vendors libpq in the wheel).
RUN pip install --no-cache-dir ".[postgres]"
COPY alembic.ini ./
COPY app/ ./app/
# Only used by the "demo" compose profile's mock-api service (see
# docker-compose.yml) — always present in the image regardless, same as the
# rest of the app's code, so no separate build path is needed for it.
COPY mock_api/ ./mock_api/
COPY --from=version /app/VERSION ./VERSION

# app/config.py's own Settings defaults for these three are relative paths
# meant for local, non-Docker development (e.g. "sqlite:///./data/humble.db",
# resolving to /app/data/humble.db here — a directory nothing ever creates,
# only /data does). docker-compose.yml/docker-compose.image.yml already pass
# in Docker-correct values explicitly, but baking the same defaults in here
# too means a deployer's *own* hand-written compose file — one that mounts
# /data but never thought to set these — still gets a working, persistent
# database instead of silently writing to the container's throwaway layer
# every time it's recreated (confirmed live: a real deployment lost its
# database this way, one that mounted a named volume at /data correctly, but
# never set these). ENV here is only ever a default — any compose file or
# `docker run -e` that sets the same name explicitly still wins.
ENV DATABASE_URL=sqlite:////data/humble.db
ENV DATA_DIR=/data
ENV DOWNLOADS_DIR=/data/downloads

RUN mkdir -p /data
VOLUME /data
EXPOSE 8000

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY docker-start.sh /usr/local/bin/docker-start.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/docker-start.sh
# Still starts as root (the image's default with no USER instruction) — the
# entrypoint's whole job is fixing /data's ownership before dropping to
# appuser for everything after, see its own comment for why that first root
# moment is unavoidable rather than a missed hardening step.
ENTRYPOINT ["docker-entrypoint.sh"]

# Single process only — app/downloads/worker.py's in-process job queue (phase 3) and
# the htmx-polling status reads share in-memory state that a second uvicorn worker
# would not see. Never add --workers here.
CMD ["docker-start.sh"]
