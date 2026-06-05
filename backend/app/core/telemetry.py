from __future__ import annotations

from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

try:
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
except ImportError:
    HTTPXClientInstrumentor = None  # type: ignore[assignment,misc]
try:
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
except ImportError:
    SQLAlchemyInstrumentor = None  # type: ignore[assignment,misc]
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBasedTraceIdRatio
from starlette.types import ASGIApp

from app.core.config import settings

logger = structlog.get_logger()

_tracer: trace.Tracer | None = None
_initialized = False


def setup_telemetry(app: ASGIApp) -> None:
    global _initialized, _tracer
    if _initialized or not settings.OTLP_ENABLED:
        return
    _initialized = True

    resource = Resource.create({
        "service.name": settings.OTLP_SERVICE_NAME,
        "service.version": settings.APP_VERSION,
        "deployment.environment": settings.APP_ENV,
    })

    provider = TracerProvider(
        resource=resource,
        sampler=ParentBasedTraceIdRatio(0.5) if settings.APP_ENV == "production" else None,
    )

    try:
        exporter = OTLPSpanExporter(endpoint=settings.OTLP_ENDPOINT, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("otlp_configured", endpoint=settings.OTLP_ENDPOINT)
    except Exception as exc:
        logger.warning("otlp_export_failed", error=str(exc))

    trace.set_tracer_provider(provider)
    _tracer = provider.get_tracer(__name__)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)  # type: ignore[arg-type]
    if HTTPXClientInstrumentor is not None:
        HTTPXClientInstrumentor().instrument()
    try:
        from app.core.database import engine
        if SQLAlchemyInstrumentor is not None:
            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
    except Exception:
        pass

    logger.info("telemetry_initialized", endpoint=settings.OTLP_ENDPOINT)


def get_tracer() -> trace.Tracer:
    return _tracer or trace.get_tracer(__name__)


def add_span_attribute(key: str, value: str | int | float | bool) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute(key, value)


def add_span_attributes(attrs: dict[str, str | int | float | bool]) -> None:
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attributes(attrs)


class TracingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        tracer = get_tracer()
        path = scope.get("path", "/unknown")
        method = scope.get("method", "WS")

        with tracer.start_as_current_span(f"{method} {path}") as span:
            span.set_attribute("http.method", method)
            span.set_attribute("http.url", path)
            span.set_attribute("service.name", settings.OTLP_SERVICE_NAME)
            try:
                await self.app(scope, receive, send)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
                raise
