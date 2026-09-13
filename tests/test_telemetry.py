from opentelemetry import trace
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app import telemetry


def _processors(provider):
    return provider._active_span_processor._span_processors


def test_tracing_disabled_by_default_attaches_no_span_processor():
    assert not any(isinstance(p, BatchSpanProcessor) for p in _processors(telemetry._tracer_provider))


def test_tracer_provider_is_set_globally():
    assert trace.get_tracer_provider() is telemetry._tracer_provider


def test_build_tracer_provider_with_no_endpoint_attaches_no_span_processor():
    provider = telemetry._build_tracer_provider("")
    assert not any(isinstance(p, BatchSpanProcessor) for p in _processors(provider))


def test_build_tracer_provider_with_endpoint_attaches_a_batch_span_processor():
    provider = telemetry._build_tracer_provider("http://example.invalid:4318/v1/traces")
    assert any(isinstance(p, BatchSpanProcessor) for p in _processors(provider))
