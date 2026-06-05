"""Cross-dialect types for PostgreSQL + SQLite compatibility."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.type_api import TypeEngine


class JSONBCompat(TypeDecorator[Any]):
    """Uses JSONB on PostgreSQL, falls back to JSON on other dialects (e.g. SQLite)."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class UUIDCompat(TypeDecorator[Any]):
    """Uses native UUID on PostgreSQL, String(36) on other dialects."""

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return uuid.UUID(value)
