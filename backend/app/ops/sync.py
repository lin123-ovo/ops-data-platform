import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal, TypeVar
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.postgres import _normalize_sqlalchemy_dsn
from app.ops.metrics import ServiceId
from app.ops.models import OpsCheckpoint, OpsService, OpsSyncRun, OpsTicket

logger = logging.getLogger(__name__)
POLL_SECONDS = 60
LEASE_SECONDS = 120
MAX_BATCH = 10_000
QUEUE_LOCK = 743580101
ACTIVE_STATUSES = ('queued', 'running')


class SourceTicket(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    ticket_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    title: str = Field(min_length=1, max_length=250)
    created_at: AwareDatetime
    sla_due_at: AwareDatetime
    resolved_at: AwareDatetime | None
    service_id: ServiceId | None = None
    revision: int = Field(strict=True, gt=0)

    @model_validator(mode='after')
    def check_times(self) -> 'SourceTicket':
        if self.sla_due_at < self.created_at:
            raise ValueError('SLA 截止时间早于工单创建时间')
        if self.resolved_at is not None and self.resolved_at < self.created_at:
            raise ValueError('解决时间早于工单创建时间')
        return self


class SourceService(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    service_id: ServiceId
    name: str = Field(min_length=1, max_length=120)
    team: str = Field(min_length=1, max_length=120)
    revision: int = Field(strict=True, gt=0)


@dataclass(frozen=True)
class SourceBatch:
    tickets: list[Mapping[str, object]]
    services: list[Mapping[str, object]]


@dataclass(frozen=True)
class Claim:
    run_id: str
    started_at: datetime
    revision: int
    service_revision: int


@lru_cache(maxsize=2)
def source_engine(dsn: str) -> Engine:
    return create_engine(
        _normalize_sqlalchemy_dsn(dsn), pool_pre_ping=True,
        connect_args={'connect_timeout': 5, 'options': '-c statement_timeout=10000'},
        hide_parameters=True,
    )


def read_source(dsn: str, revision: int, service_revision: int) -> SourceBatch:
    with source_engine(dsn).connect() as connection:
        connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        tickets = connection.execute(
            text(
                'SELECT ticket_id, title, created_at, sla_due_at, resolved_at, service_id, revision '
                'FROM public.demo_ticket WHERE revision > :revision ORDER BY revision LIMIT :limit'
            ),
            {'revision': revision, 'limit': MAX_BATCH + 1},
        ).mappings().all()
        services = connection.execute(
            text(
                'SELECT service_id, name, team, revision FROM public.demo_service '
                'WHERE revision > :revision ORDER BY revision LIMIT :limit'
            ),
            {'revision': service_revision, 'limit': MAX_BATCH + 1},
        ).mappings().all()
        return SourceBatch(list(tickets), list(services))


def _lock_queue(db: Session) -> None:
    db.execute(select(func.pg_advisory_xact_lock(QUEUE_LOCK)))


def enqueue(db: Session, trigger: Literal['manual', 'scheduled']) -> OpsSyncRun:
    """Return the current active run or create one. The caller commits."""
    _lock_queue(db)
    active = db.scalars(select(OpsSyncRun).where(OpsSyncRun.status.in_(ACTIVE_STATUSES))).one_or_none()
    if active is not None:
        return active
    run = OpsSyncRun(
        id=str(uuid4()), status='queued', trigger=trigger, queued_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    return run


def claim_run(db: Session) -> Claim | None:
    """Reap an expired lease, schedule a due check, and claim any queued run."""
    _lock_queue(db)
    now = datetime.now(timezone.utc)
    active = db.scalars(
        select(OpsSyncRun).where(OpsSyncRun.status.in_(ACTIVE_STATUSES)).with_for_update(),
    ).one_or_none()
    if active is not None and active.status == 'running':
        if active.started_at is not None and now - active.started_at < timedelta(seconds=LEASE_SECONDS):
            return None
        active.status = 'failed'
        active.error = '同步 worker 中断或超时，任务已释放'
        active.finished_at = now
        db.flush()
        active = None
    if active is None:
        latest = db.scalars(select(OpsSyncRun.queued_at).order_by(OpsSyncRun.queued_at.desc()).limit(1)).first()
        if latest is not None and now - latest < timedelta(seconds=POLL_SECONDS):
            return None
        active = enqueue(db, 'scheduled')
    checkpoint = db.get(OpsCheckpoint, 1)
    if checkpoint is None:
        raise RuntimeError('请先运行运维数据迁移')
    active.status = 'running'
    active.started_at = datetime.now(timezone.utc)
    return Claim(
        run_id=active.id, started_at=active.started_at,
        revision=checkpoint.revision, service_revision=checkpoint.service_revision,
    )


SourceRow = TypeVar('SourceRow', SourceTicket, SourceService)


def _validate_batch(
    rows: list[Mapping[str, object]], revision: int, model: type[SourceRow],
    *, id_field: str = 'ticket_id', label: str = '工单',
) -> list[SourceRow]:
    if len(rows) > MAX_BATCH:
        raise ValueError(f'{label}待同步记录超过演示上限 {MAX_BATCH}')
    validated: list[SourceRow] = []
    ids: set[str] = set()
    for number, row in enumerate(rows, start=1):
        try:
            record = model.model_validate(row)
        except ValidationError as exc:
            field = '.'.join(map(str, exc.errors()[0]['loc'])) or '时间顺序'
            raise ValueError(f'第 {number} 条{label}源记录校验失败（{field}）') from exc
        record_id = getattr(record, id_field)
        if record.revision <= revision or record_id in ids:
            raise ValueError(f'第 {number} 条{label}源记录的版本或 ID 重复')
        revision = record.revision
        ids.add(record_id)
        validated.append(record)
    return validated


def _owned_run(db: Session, claim: Claim) -> OpsSyncRun | None:
    run = db.get(OpsSyncRun, claim.run_id, with_for_update=True)
    if run is None or run.status != 'running' or run.started_at != claim.started_at:
        return None
    return run


def _upsert(db: Session, target: type[OpsTicket] | type[OpsService], rows: list[dict], id_field: str) -> None:
    statement = insert(target)
    db.execute(
        statement.on_conflict_do_update(
            index_elements=[id_field],
            set_={column: statement.excluded[column] for column in rows[0] if column != id_field},
        ),
        rows,
    )


def _publish(db: Session, claim: Claim, tickets: list[SourceTicket], services: list[SourceService]) -> None:
    run = _owned_run(db, claim)
    if run is None:
        return
    checkpoint = db.get(OpsCheckpoint, 1, with_for_update=True)
    if checkpoint is None or (checkpoint.revision, checkpoint.service_revision) != (
        claim.revision, claim.service_revision,
    ):
        raise RuntimeError('同步游标与领取任务时不一致')
    if tickets:
        _upsert(db, OpsTicket, [ticket.model_dump() for ticket in tickets], 'ticket_id')
        checkpoint.revision = tickets[-1].revision
    if services:
        _upsert(db, OpsService, [service.model_dump() for service in services], 'service_id')
        checkpoint.service_revision = services[-1].revision
    if tickets or services:
        checkpoint.data_version += 1
    finished_at = datetime.now(timezone.utc)
    checkpoint.last_success_at = finished_at
    run.rows_read = run.rows_written = len(tickets) + len(services)
    run.status = 'succeeded'
    run.finished_at = finished_at


def sync_once(
    session_factory: Callable[[], Session], read: Callable[[int, int], SourceBatch],
) -> str | None:
    """Execute at most one due run, recording recoverable failures without advancing the cursor."""
    with session_factory() as db, db.begin():
        claim = claim_run(db)
    if claim is None:
        return None

    rows_read = 0
    error = None
    try:
        batch = read(claim.revision, claim.service_revision)
        rows_read = len(batch.tickets) + len(batch.services)
        tickets = _validate_batch(batch.tickets, claim.revision, SourceTicket)
        services = _validate_batch(
            batch.services, claim.service_revision, SourceService, id_field='service_id', label='服务目录',
        )
        with session_factory() as db, db.begin():
            _publish(db, claim, tickets, services)
    except ValueError as exc:
        error = str(exc)
    except SQLAlchemyError as exc:
        logger.warning('Ops sync database error run=%s type=%s', claim.run_id, type(exc).__name__)
        error = '源库读取或目标库写入失败，请检查数据库连接及表结构'
    except RuntimeError as exc:
        logger.error('Ops sync state error run=%s type=%s', claim.run_id, type(exc).__name__)
        error = '同步状态异常，请检查游标和迁移'
    if error is not None:
        with session_factory() as db, db.begin():
            run = _owned_run(db, claim)
            if run is not None:
                run.status = 'failed'
                run.error = error
                run.rows_read = rows_read
                run.finished_at = datetime.now(timezone.utc)
    return claim.run_id
