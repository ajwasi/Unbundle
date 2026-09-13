import importlib

from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app import telemetry
from app.config import settings


def _processors():
    return telemetry._tracer_provider._active_span_processor._span_processors


def test_tracing_disabled_by_default_attaches_no_span_processor():
    assert not any(isinstance(p, BatchSpanProcessor) for p in _processors())


def test_tracer_provider_is_set_globally():
    from opentelemetry import trace

    assert trace.get_tracer_provider() is telemetry._tracer_provider


def test_tracing_enabled_attaches_a_batch_span_processor():
    # Module-level setup only runs once at import — reload with the endpoint
    # set to exercise the enabled branch, then reload again to restore the
    # disabled state other tests in the suite rely on.
    original = settings.otel_exporter_otlp_endpoint
    settings.otel_exporter_otlp_endpoint = "http://example.invalid:4318/v1/traces"
    try:
        importlib.reload(telemetry)
        assert any(isinstance(p, BatchSpanProcessor) for p in _processors())
    finally:
        settings.otel_exporter_otlp_endpoint = original
        importlib.reload(telemetry)
