"""Cross-dialect types for PostgreSQL + SQLite compatibility."""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID


class JSONBCompat(TypeDecorator):
    """Uses JSONB on PostgreSQL, falls back to JSON on other dialects (e.g. SQLite)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class UUIDCompat(TypeDecorator):
    """Uses native UUID on PostgreSQL, String(36) on other dialects."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return uuid.UUID(value)
