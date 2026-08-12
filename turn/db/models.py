"""SQLAlchemy ORM models mirroring the domain schemas."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, JSON
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import Uuid  # generic UUID (PG + sqlite via 2.0)

from turn.domain.schemas import _utcnow


class Base(DeclarativeBase):
    pass


def _utcnow_default() -> datetime:  # pragma: no cover - used by Column defaults
    return datetime.now(timezone.utc)


class NodeModel(Base):
    __tablename__ = "nodes"

    id = Column(Uuid(as_uuid=True), primary_key=True)
    project_id = Column(Uuid(as_uuid=True), index=True, nullable=False)
    parent_id = Column(Uuid(as_uuid=True), index=True, nullable=True)

    objective = Column(Text, nullable=False)
    generated_prompt = Column(Text, nullable=True)
    executor = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="PENDING")
    paused = Column(Boolean, nullable=False, default=False)
    auto_run = Column(Boolean, nullable=False, default=True)

    required_inputs = Column(JSON, nullable=False, default=list)
    resource_refs = Column(JSON, nullable=False, default=list)
    artifact_refs = Column(JSON, nullable=False, default=list)

    revision = Column(Integer, nullable=False, default=1)
    superseded_by = Column(Uuid(as_uuid=True), nullable=True)
    forked_from = Column(Uuid(as_uuid=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class EdgeModel(Base):
    __tablename__ = "edges"

    id = Column(Uuid(as_uuid=True), primary_key=True)
    src = Column(Uuid(as_uuid=True), index=True, nullable=False)
    dst = Column(Uuid(as_uuid=True), index=True, nullable=False)
    type = Column(String(16), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class RunModel(Base):
    __tablename__ = "runs"

    id = Column(Uuid(as_uuid=True), primary_key=True)
    node_id = Column(Uuid(as_uuid=True), index=True, nullable=False)
    worker = Column(String(64), nullable=False)

    started_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    ended_at = Column(DateTime(timezone=True), nullable=True)

    status = Column(String(16), nullable=False, default="RUNNING")
    outcome = Column(String(16), nullable=True)
    summary = Column(Text, nullable=True)
    logs = Column(Text, nullable=False, default="")
    error = Column(Text, nullable=True)
    retry_recommended = Column(Boolean, nullable=False, default=False)
    node_revision = Column(Integer, nullable=False, default=1)


class ArtifactModel(Base):
    __tablename__ = "artifacts"

    id = Column(Uuid(as_uuid=True), primary_key=True)
    node_id = Column(Uuid(as_uuid=True), index=True, nullable=True)
    kind = Column(String(24), nullable=False, default="text")
    name = Column(String(255), nullable=False)
    content = Column(JSON, nullable=True)
    ref = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class SettingModel(Base):
    """Key/value store for cross-project preferences (e.g. default auto-run)."""

    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)


# Re-export for convenience.
__all__ = [
    "Base",
    "NodeModel",
    "EdgeModel",
    "RunModel",
    "ArtifactModel",
    "SettingModel",
]
