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

# Explicit on purpose: Go's os.UserHomeDir() (what humble-cli uses to find
# ~/.humble-cli-key) requires $HOME to be set and does NOT fall back to
# /etc/passwd if it's missing — unverified whether python:3.12-slim's default
# root shell environment actually exports HOME to a non-interactive `sh -c`
# CMD, since Docker Desktop isn't installed on the dev machine this was
# written on to test directly. Setting it removes that ambiguity; this must
# stay in sync with app.config.Settings.humble_cli_key_path's default
# (Path.home()), which resolves via the same variable.
ENV HOME=/root

ARG HUMBLE_CLI_VERSION=v0.23.2
ARG TARGETARCH

# smbl64/humble-cli publishes no checksums.txt or signature for its releases
# (confirmed 2026-09-07 against the real v0.23.2 release assets via the GitHub
# API — no checksum manifest, nothing in the release notes either), so these
# are self-computed against the exact bytes of the exact pinned version above.
# Bumping HUMBLE_CLI_VERSION means recomputing and updating both of these.
ARG HUMBLE_CLI_SHA256_AMD64=026f2b9a5c4594a51e66ef3d249e28dead50f2494e931738c7ea84e8ac44660a
ARG HUMBLE_CLI_SHA256_ARM64=870b5fad2376a4a58b69b1cd55776a159a9a66b559dea5c252cb29812b83f694

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
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

RUN mkdir -p /data
VOLUME /data
EXPOSE 8000

# Single process only — app/downloads/worker.py's in-process job queue (phase 3) and
# the htmx-polling status reads share in-memory state that a second uvicorn worker
# would not see. Never add --workers here.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
