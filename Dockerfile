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
RUN curl -fsSL \
      "https://github.com/smbl64/humble-cli/releases/download/${HUMBLE_CLI_VERSION}/humble-cli-linux-${TARGETARCH}.tar.gz" \
      -o /tmp/humble-cli.tar.gz \
    && tar -xzf /tmp/humble-cli.tar.gz -C /tmp \
    && mv "/tmp/humble-cli-linux-${TARGETARCH}" /usr/local/bin/humble-cli \
    && rm /tmp/humble-cli.tar.gz \
    && chmod +x /usr/local/bin/humble-cli \
    && /usr/local/bin/humble-cli --version

COPY pyproject.toml ./
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY app/ ./app/

RUN mkdir -p /data
VOLUME /data
EXPOSE 8000

# Single process only — app/downloads/worker.py's in-process job queue (phase 3) and
# the htmx-polling status reads share in-memory state that a second uvicorn worker
# would not see. Never add --workers here.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
