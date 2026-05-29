"""Domain enums used across persistence models."""

from __future__ import annotations

import enum


class Dialect(str, enum.Enum):
    """Supported SQL dialects."""

    POSTGRESQL = "postgresql"
    ORACLE = "oracle"


class SessionStatus(str, enum.Enum):
    """Lifecycle states for a session."""

    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"


class FeedbackAction(str, enum.Enum):
    """User feedback actions on an iteration."""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class IterationStatus(str, enum.Enum):
    """Lifecycle of a single SQL generation attempt."""

    PENDING = "pending"
    VALIDATED = "validated"
    EXECUTED = "executed"
    FAILED = "failed"
