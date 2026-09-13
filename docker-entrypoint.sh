#!/bin/sh
# Runs as root — the image's default, and the only way a fresh container can
# reliably fix this — just long enough to make /data writable by the
# unprivileged appuser this app actually runs as, then drops to that user via
# gosu for the real process. Never runs app code (or humble-cli, or anything
# touching a stored credential) as root.
#
# The chown is needed on every start, not just the first: Docker preserves
# whatever UID owns a volume/bind-mount's contents already, and every image
# before this change ran (and therefore created /data) as root — an existing
# deployment's volume is root-owned until this runs once. Safe to repeat: a
# no-op once ownership already matches appuser.
set -e
chown -R appuser:appuser /data
exec gosu appuser "$@"
