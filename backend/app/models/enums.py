"""Domain enums used across persistence models."""

from __future__ import annotations

import enum
import sys

# ruff: noqa: UP036, UP042 — StrEnum compat needed for Python 3.10

if sys.version_info >= (3, 11):
    StrEnum = enum.StrEnum
else:
    class StrEnum(str, enum.Enum):
        """Compatibility shim for Python 3.10."""

        def __str__(self) -> str:
            return self.value


class Dialect(StrEnum):
    """Supported SQL dialects."""

    POSTGRESQL = "postgresql"
    ORACLE = "oracle"


class SessionStatus(StrEnum):
    """Lifecycle states for a session."""

    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


class FeedbackAction(StrEnum):
    """User feedback actions on an iteration."""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class IterationStatus(StrEnum):
    """Lifecycle of a single SQL generation attempt."""

    PENDING = "pending"
    VALIDATED = "validated"
    EXECUTED = "executed"
    FAILED = "failed"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class RequestStatus(StrEnum):
    """Lifecycle states for a request."""

    OPEN = "open"
    APPROVED = "approved"
    NEEDS_INTERVENTION = "needs_human_intervention"
