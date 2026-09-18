#!/bin/sh
# Runs migrations, then launches uvicorn — as appuser, via docker-entrypoint.sh.
#
# Exists as a script rather than a one-line CMD because the --ssl-* flags are
# conditional: uvicorn rejects an empty --ssl-certfile, so they have to be
# absent entirely (not blank) when no certificate is configured. Building the
# argument list with `set --` keeps paths intact regardless of what's in them,
# which `${VAR:+--flag $VAR}` inside a CMD string would not.
set -e

alembic upgrade head

set -- --host 0.0.0.0 --port 8000

if [ -n "$SSL_CERTFILE" ] && [ -n "$SSL_KEYFILE" ]; then
  set -- "$@" --ssl-certfile "$SSL_CERTFILE" --ssl-keyfile "$SSL_KEYFILE"
  if [ -n "$SSL_KEYFILE_PASSWORD" ]; then
    set -- "$@" --ssl-keyfile-password "$SSL_KEYFILE_PASSWORD"
  fi
elif [ -n "$SSL_CERTFILE" ] || [ -n "$SSL_KEYFILE" ]; then
  # One without the other can't serve TLS, and silently falling back to plain
  # HTTP would leave someone believing they had HTTPS.
  echo "ERROR: SSL_CERTFILE and SSL_KEYFILE must be set together (got only one)." >&2
  exit 1
fi

exec uvicorn app.main:app "$@"
