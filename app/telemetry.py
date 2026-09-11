"""OpenTelemetry metrics, exposed via the OTel Prometheus exporter rather than
OTLP push — this app has no collector to push to, and "a /metrics endpoint
Prometheus scrapes" is the actual ask. The exporter registers into
prometheus_client's process-wide default REGISTRY, which is also where
prometheus_client's own auto-registered Python process/GC collectors live —
so app/routers/metrics.py's generate_latest(REGISTRY) picks up process health
metrics for free, not just what's defined here.

Deliberately metrics-only, not traces/logs — this is a single-process app
with no distributed calls worth tracing, and the ask was specifically a
Prometheus/Grafana-consumable endpoint.

Custom instruments are observable (callback-based), not push-updated counters
scattered across the codebase, for everything that's really "current state
already sitting in the DB" (bundle count, download/job counts by status,
unredeemed-key counts, connector status, last sync time) — each callback opens
its own short-lived SessionLocal() and is invoked lazily whenever /metrics is
actually scraped, mirroring this app's existing "compute fresh from the DB, no
persisted duplicate state" convention (see catalog.py, finance.py) rather than
needing every write path instrumented and kept in sync. Two exceptions:
rate_limit_rejections_total (a rate-limit rejection isn't state sitting in the
DB anywhere, so that one really is a push counter, incremented directly in
app/ratelimit.py at the moment a request is rejected) and update_available
(reads app/version.py's own periodically-refreshed in-memory cache instead —
nothing to query, and re-hitting GitHub's API on every scrape would be both
pointless and a good way to get rate-limited).
"""

from opentelemetry import metrics
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from sqlalchemy import func

from app.db import SessionLocal
from app.models.bundle import Bundle
from app.models.bundle_entitlement import BundleEntitlement
from app.models.credential import SOURCE_GOG, SOURCE_HUMBLE, SOURCE_STEAM, STATUS_OK, Credential
from app.models.download import Download
from app.models.download_job import DownloadJob
from app.models.sync_run import STATUS_SUCCESS, SyncRun
from app.version import is_update_available

METER_NAME = "unbundle"

_resource = Resource.create({"service.name": "unbundle"})
_reader = PrometheusMetricReader()
_provider = MeterProvider(metric_readers=[_reader], resource=_resource)
metrics.set_meter_provider(_provider)

meter = metrics.get_meter(METER_NAME)

# Real push counter — see module docstring for why this one isn't observable.
rate_limit_rejections_total = meter.create_counter(
    "unbundle.rate_limit.rejections",
    description="Requests rejected by a rate limiter, by which one",
)


def _bundle_count(options):
    db = SessionLocal()
    try:
        return [metrics.Observation(db.query(Bundle).count())]
    finally:
        db.close()


def _download_file_counts(options):
    db = SessionLocal()
    try:
        rows = db.query(Download.status, func.count(Download.id)).group_by(Download.status).all()
        return [metrics.Observation(count, {"status": status}) for status, count in rows]
    finally:
        db.close()


def _download_job_counts(options):
    db = SessionLocal()
    try:
        rows = db.query(DownloadJob.status, func.count(DownloadJob.id)).group_by(DownloadJob.status).all()
        return [metrics.Observation(count, {"status": status}) for status, count in rows]
    finally:
        db.close()


def _unredeemed_key_counts(options):
    db = SessionLocal()
    try:
        steam = db.query(BundleEntitlement).filter(
            BundleEntitlement.steam_app_id.isnot(None), BundleEntitlement.steam_owned.is_(False)
        ).count()
        # No gog_id.isnot(None) requirement — sync/gog_sync.py falls back to
        # name-matching when gog_id is absent (virtually always), so gog_owned
        # alone is the correct "was this checked" signal (same fix already
        # applied to routers/gog.py's own unredeemed-list query).
        gog = db.query(BundleEntitlement).filter(BundleEntitlement.gog_owned.is_(False)).count()
        return [
            metrics.Observation(steam, {"platform": "steam"}),
            metrics.Observation(gog, {"platform": "gog"}),
        ]
    finally:
        db.close()


def _connector_status(options):
    db = SessionLocal()
    try:
        creds = {c.source: c.status for c in db.query(Credential.source, Credential.status).all()}
        return [
            metrics.Observation(1 if creds.get(source) == STATUS_OK else 0, {"source": source})
            for source in (SOURCE_HUMBLE, SOURCE_STEAM, SOURCE_GOG)
        ]
    finally:
        db.close()


def _update_available(options):
    # Not DB-backed like the others (see module docstring) — reads app/version.py's
    # own periodically-refreshed cache, cheap and non-blocking at scrape time.
    return [metrics.Observation(1 if is_update_available() else 0)]


def _last_sync_timestamp(options):
    db = SessionLocal()
    try:
        run = (
            db.query(SyncRun)
            .filter(SyncRun.status == STATUS_SUCCESS)
            .order_by(SyncRun.finished_at.desc())
            .first()
        )
        if run is None or run.finished_at is None:
            return []
        return [metrics.Observation(run.finished_at.timestamp())]
    finally:
        db.close()


meter.create_observable_gauge(
    "unbundle.bundles.count",
    callbacks=[_bundle_count],
    description="Number of bundles currently in the library",
)
meter.create_observable_gauge(
    "unbundle.downloads.files",
    callbacks=[_download_file_counts],
    description="Downloadable files tracked, by status",
)
meter.create_observable_gauge(
    "unbundle.downloads.jobs",
    callbacks=[_download_job_counts],
    description="Download jobs tracked, by status",
)
meter.create_observable_gauge(
    "unbundle.entitlements.unredeemed",
    callbacks=[_unredeemed_key_counts],
    description="Third-party keys confirmed not redeemed on the matching platform",
)
meter.create_observable_gauge(
    "unbundle.connector.status",
    callbacks=[_connector_status],
    description="Whether each connector's stored credential is currently valid (1) or not (0)",
)
meter.create_observable_gauge(
    "unbundle.sync.last_success_timestamp",
    unit="s",
    callbacks=[_last_sync_timestamp],
    description="Unix timestamp of the last successful library sync",
)
meter.create_observable_gauge(
    "unbundle.update_available",
    callbacks=[_update_available],
    description="Whether a newer commit than the running version exists on GitHub's main branch (1) or not (0)",
)


def instrument_app(app) -> None:
    FastAPIInstrumentor.instrument_app(app, meter_provider=_provider)
