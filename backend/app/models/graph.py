"""Durable graph projection state. No foreign keys: deletion leaves cleanup tombstones."""
from __future__ import annotations

from sqlalchemy import Float, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class GraphState(Base, TimestampMixin):
    __tablename__ = 'graph_state'
    document_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base: Mapped[str] = mapped_column(String(100), index=True)
    desired_version: Mapped[str] = mapped_column(String(64))
    published_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default='pending')


class GraphJob(Base, TimestampMixin):
    __tablename__ = 'graph_job'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[str] = mapped_column(String(64))
    knowledge_base: Mapped[str] = mapped_column(String(100), index=True)
    operation: Mapped[str] = mapped_column(String(16), default='build')
    status: Mapped[str] = mapped_column(String(20), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(100))
    schema_version: Mapped[str] = mapped_column(String(32))
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)


class GraphChatMeta(Base):
    __tablename__ = 'graph_chat_meta'
    query_log_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    diagnostics: Mapped[dict] = mapped_column(JSON, default=dict)
    thread_id: Mapped[str] = mapped_column(String(200))
