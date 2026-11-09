"""Shared test configuration, including the test compose Postgres connection details."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import secrets
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# psycopg's async driver cannot run on Windows' default ProactorEventLoop. Production runs on
# Linux; this only affects tests on the Windows development host.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REQUIRED_ENV = ("TETHER_MIGRATOR_DB_PASSWORD", "TETHER_APP_DB_PASSWORD")


def _random_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


# Per-session random audit and call-binding keys (separate families, never shared material).
os.environ.setdefault("TETHER_AUDIT_ACTIVE_KEY_ID", "TESTA1")
os.environ.setdefault("TETHER_AUDIT_KEY_TESTA1", _random_key())
os.environ.setdefault("TETHER_BINDING_ACTIVE_KEY_ID", "TESTB1")
os.environ.setdefault("TETHER_BINDING_KEY_TESTB1", _random_key())

# Telemetry for the whole session (the global OTel providers can be set once per process): spans
# and metrics are kept in memory for assertions. The code under test uses the global API exactly
# as the api and worker processes do; only the exporters differ.
SPANS = InMemorySpanExporter()
TRACER_PROVIDER = TracerProvider()
TRACER_PROVIDER.add_span_processor(SimpleSpanProcessor(SPANS))
otel_trace.set_tracer_provider(TRACER_PROVIDER)
METRICS = InMemoryMetricReader()
otel_metrics.set_meter_provider(MeterProvider(metric_readers=[METRICS]))


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """The finished spans of this test only."""
    SPANS.clear()
    return SPANS


Points = dict[tuple[str, frozenset[tuple[str, Any]]], float]


def _metric_points() -> Points:
    """Counters and gauges by value, histograms by count, keyed by (name, attributes)."""
    points: Points = {}
    data = METRICS.get_metrics_data()
    for resource in data.resource_metrics if data else ():
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                for point in metric.data.data_points:
                    value = point.count if hasattr(point, "count") else point.value
                    key = (metric.name, frozenset(dict(point.attributes or {}).items()))
                    points[key] = points.get(key, 0) + value
    return points


@pytest.fixture
def metric_values() -> Callable[[], Points]:
    """``delta()`` returns every metric point's change since the test started."""
    baseline = _metric_points()

    def delta() -> Points:
        current = _metric_points()
        return {
            key: value - baseline.get(key, 0)
            for key, value in current.items()
            if value != baseline.get(key, 0)
        }

    return delta


@pytest.fixture
def log_lines() -> Iterator[Callable[[], list[dict[str, Any]]]]:
    """Route every log record through the production JSON pipeline into a buffer."""
    from tether.observability import logging as tether_logging

    buffer = io.StringIO()
    handler = tether_logging.configure_logging("DEBUG", stream=buffer)
    try:
        yield lambda: [json.loads(line) for line in buffer.getvalue().splitlines() if line]
    finally:
        tether_logging.remove_handler(handler)


@pytest.fixture
def raw_log() -> Iterator[io.StringIO]:
    """The raw text of every log line, for scanning (same pipeline as ``log_lines``)."""
    from tether.observability import logging as tether_logging

    buffer = io.StringIO()
    handler = tether_logging.configure_logging("DEBUG", stream=buffer)
    try:
        yield buffer
    finally:
        tether_logging.remove_handler(handler)


@dataclass(frozen=True)
class TestDatabase:
    """The Postgres from deploy/compose/docker-compose.test.yml."""

    __test__ = False  # not a pytest test class

    host: str
    port: int
    migrator_password: str
    app_password: str
    dbname: str = "tether"
    # The test-only Postgres superuser, used solely to create template-database clones.
    superuser_password: str | None = None

    def _dsn(self, role: str, password: str, dbname: str | None = None) -> str:
        database = dbname or self.dbname
        return f"postgresql://{role}:{quote(password, safe='')}@{self.host}:{self.port}/{database}"

    def superuser_dsn(self, dbname: str = "postgres") -> str:
        if not self.superuser_password:
            pytest.fail(
                "TETHER_POSTGRES_PASSWORD is required for template-database fixtures",
                pytrace=False,
            )
        return self._dsn("postgres", self.superuser_password, dbname)

    @property
    def migrator_dsn(self) -> str:
        return self._dsn("tether_migrator", self.migrator_password)

    @property
    def app_dsn(self) -> str:
        return self._dsn("tether_app", self.app_password)


@pytest.fixture(scope="session")
def test_db() -> TestDatabase:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        pytest.fail(
            f"missing {', '.join(missing)}: run `uv run poe test-integration` or "
            "`uv run poe test-contract`, which start deploy/compose/docker-compose.test.yml and "
            "load deploy/compose/.env (copy deploy/compose/.env.example)",
            pytrace=False,
        )
    return TestDatabase(
        host=os.environ.get("TETHER_TEST_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TETHER_TEST_PG_PORT", "55432")),
        migrator_password=os.environ["TETHER_MIGRATOR_DB_PASSWORD"],
        app_password=os.environ["TETHER_APP_DB_PASSWORD"],
        superuser_password=os.environ.get("TETHER_POSTGRES_PASSWORD") or None,
    )
