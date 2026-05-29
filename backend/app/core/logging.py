"""
Structured logging configuration using structlog.

Structlog produces JSON-formatted log entries that are easy to parse
in production (e.g., by Datadog, ELK, CloudWatch).

Usage:
    import structlog
    logger = structlog.get_logger()
    logger.info("user_login", user_id=123)
"""

import logging
import sys

import structlog


def setup_logging(log_level: str = "INFO") -> None:
    """Configure structlog for structured JSON logging."""

    # Shared processors for all log entries
    shared_processors: list[structlog.types.Processor] = [
        # Add timestamp in ISO format
        structlog.processors.TimeStamper(fmt="iso"),
        # Add log level
        structlog.processors.add_log_level,
        # If the log entry is from stdlib logging, extract useful info
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        # Format stack traces nicely
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            # Filter by log level
            structlog.stdlib.filter_by_level,
            *shared_processors,
            # Wrap the final output for stdlib compatibility
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Set up the root logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, log_level.upper()))
