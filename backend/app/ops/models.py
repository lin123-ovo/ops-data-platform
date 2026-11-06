from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class OpsBase(DeclarativeBase):
    """Created by the opt-in migration, outside the default application startup."""


class OpsCheckpoint(OpsBase):
    __tablename__ = 'ops_checkpoint'

    id: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    service_revision: Mapped[int] = mapped_column(BigInteger, server_default='0')
    data_version: Mapped[int] = mapped_column(BigInteger, server_default='0')
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpsTicket(OpsBase):
    __tablename__ = 'ops_ticket'

    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sla_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(BigInteger)


    service_id: Mapped[str | None] = mapped_column(String(64))


class OpsService(OpsBase):
    __tablename__ = 'ops_service'

    service_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    team: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(BigInteger)


class OpsSyncRun(OpsBase):
    __tablename__ = 'ops_sync_run'
    __table_args__ = (
        Index(
            'uq_ops_one_active_run', text('(1)'), unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trigger: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(12))
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_read: Mapped[int] = mapped_column(default=0)
    rows_written: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
