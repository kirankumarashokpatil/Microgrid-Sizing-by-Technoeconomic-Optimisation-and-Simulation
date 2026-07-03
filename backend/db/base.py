"""Declarative base, bound to the configured Postgres schema."""
from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

from db.config import get_settings

DB_SCHEMA = get_settings().db_schema


class Base(DeclarativeBase):
    metadata = MetaData(schema=DB_SCHEMA)
