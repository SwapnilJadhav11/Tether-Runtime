"""Process telemetry setup for the api and worker (S1.9; ADR-0015: best-effort).

JSON logging first, so any later failure is reported; then secrets are registered with the log
scrubber; then the OTLP tracer and meter providers. Export runs on background threads with short
timeouts, so an unreachable collector costs dropped telemetry, never a blocked request, job or
shutdown. If setup itself fails, the process runs without telemetry.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from tether.observability.logging import configure_logging, get_logger, register_secret
from tether.settings import Settings

_log = get_logger(__name__)
# Third-party agent tracing would ship prompts and tool payloads to a hosted service.
_THIRD_PARTY_TRACING = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")


@dataclass
class Telemetry:
    tracer_provider: TracerProvider | None = None
    meter_provider: MeterProvider | None = None

    def shutdown(self, timeout_seconds: float = 5.0) -> None:
        """Flush what can be flushed within the export timeouts; never raises."""
        try:
            if self.tracer_provider is not None:
                self.tracer_provider.shutdown()
        except Exception:
            _log.warning("trace export shutdown failed")
        try:
            if self.meter_provider is not None:
                self.meter_provider.shutdown(timeout_millis=timeout_seconds * 1000)
        except Exception:
            _log.warning("metric export shutdown failed")


def disable_third_party_tracing() -> None:
    for name in _THIRD_PARTY_TRACING:
        os.environ[name] = "false"


def register_settings_secrets(settings: Settings) -> None:
    """Scrub the database credentials from logs (the key ring registers its own keys)."""
    url = settings.database_url.get_secret_value()
    register_secret(url)
    try:
        password = urlsplit(url).password
    except ValueError:
        password = None
    register_secret(password)


def build_providers(
    *,
    service: str,
    env: str,
    endpoint: str | None,
    export_timeout_seconds: float,
    metric_interval_seconds: float,
) -> tuple[TracerProvider, MeterProvider]:
    """Tracer and meter providers; they export over OTLP/HTTP when ``endpoint`` is set."""
    resource = Resource.create(
        {
            "service.name": service,
            "service.namespace": "tether",
            "deployment.environment.name": env,
        }
    )
    tracer_provider = TracerProvider(resource=resource)
    readers: list[MetricReader] = []
    if endpoint:
        base = endpoint.rstrip("/")
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{base}/v1/traces", timeout=export_timeout_seconds),
                export_timeout_millis=export_timeout_seconds * 1000,
            )
        )
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{base}/v1/metrics", timeout=export_timeout_seconds),
                export_interval_millis=metric_interval_seconds * 1000,
                export_timeout_millis=export_timeout_seconds * 1000,
            )
        )
    return tracer_provider, MeterProvider(resource=resource, metric_readers=readers)


def setup_telemetry(settings: Settings, *, service: str) -> Telemetry:
    configure_logging(settings.log_level)
    register_settings_secrets(settings)
    disable_third_party_tracing()
    try:
        tracer_provider, meter_provider = build_providers(
            service=service,
            env=settings.env,
            endpoint=settings.otel_endpoint,
            export_timeout_seconds=settings.otel_export_timeout_seconds,
            metric_interval_seconds=settings.otel_metric_interval_seconds,
        )
        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
    except Exception:
        _log.warning("telemetry unavailable; continuing without traces and metrics", exc_info=True)
        return Telemetry()
    _log.info("telemetry ready", service=service, exporting=bool(settings.otel_endpoint))
    return Telemetry(tracer_provider, meter_provider)
