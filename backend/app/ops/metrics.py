from datetime import date, datetime, time, timedelta
from fractions import Fraction
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.ops.models import OpsCheckpoint, OpsService, OpsTicket

OPS_TIMEZONE = ZoneInfo('Asia/Hong_Kong')
OPS_KNOWLEDGE_BASE = 'ops_demo_manual'
METRIC_ID = 'sla_breached_ticket_count'
UNMATCHED_SERVICE = '__unmatched__'
SERVICE_ID_PATTERN = r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$'
ServiceId = Annotated[str, Field(pattern=SERVICE_ID_PATTERN)]
ServiceScope = Annotated[str, Field(pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63}|__unmatched__)$')]
DetailKind = Literal['breached', 'due']


class OpsPeriod(BaseModel):
    """Hong Kong dates with an exclusive end date."""

    start_date: date
    end_date: date

    @model_validator(mode='after')
    def validate_range(self) -> 'OpsPeriod':
        if not 1 <= (self.end_date - self.start_date).days <= 90:
            raise ValueError('请选择 1 至 90 天的日期区间')
        if self.end_date > datetime.now(OPS_TIMEZONE).date():
            raise ValueError('请选择已经结束的日期区间')
        return self


class SlaQuery(OpsPeriod):
    service_id: ServiceScope | None = None


def last_full_week() -> OpsPeriod:
    today = datetime.now(OPS_TIMEZONE).date()
    monday = today - timedelta(days=today.weekday())
    return OpsPeriod(start_date=monday - timedelta(days=7), end_date=monday)


class ServiceItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    service_id: str
    name: str
    team: str | None


UNMATCHED = ServiceItem(service_id=UNMATCHED_SERVICE, name='未匹配服务', team=None)


class ServiceMetrics(ServiceItem):
    due_count: int
    breached_count: int
    compliance_rate: float | None
    rank: int | None = None


class LegacyMetricSnapshot(BaseModel):
    metric_id: Literal['sla_breached_ticket_count'] = METRIC_ID
    status: Literal['ok', 'not_ready', 'unavailable']
    start_date: date
    end_date: date
    count: int | None = None
    data_version: str | None = None
    last_success_at: datetime | None = None


class MetricSnapshot(LegacyMetricSnapshot):
    # Older chat history stores only the fields in LegacyMetricSnapshot.
    service_id: str | None = None
    service: ServiceItem | None = None
    due_count: int | None = None
    compliance_rate: float | None = None
    service_breakdown: list[ServiceMetrics] = Field(default_factory=list)


class TicketItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ticket_id: str
    title: str
    created_at: datetime
    sla_due_at: datetime
    resolved_at: datetime | None
    service_id: str
    service_name: str
    sla_met: bool


class TicketPage(BaseModel):
    items: list[TicketItem]
    total: int
    data_version: str


class MetricVersionChanged(ValueError):
    """The requested drill-down no longer refers to the published version."""


class UnknownService(ValueError):
    """The requested service is not in the published directory."""


def _due(period: OpsPeriod) -> tuple:
    start = datetime.combine(period.start_date, time.min, OPS_TIMEZONE)
    end = datetime.combine(period.end_date, time.min, OPS_TIMEZONE)
    return OpsTicket.sla_due_at >= start, OpsTicket.sla_due_at < end


def _breached():
    return or_(OpsTicket.resolved_at.is_(None), OpsTicket.resolved_at > OpsTicket.sla_due_at)


def _published_state(db: Session) -> OpsCheckpoint:
    # Readers pin the version while querying. Publication takes the same lock
    # exclusively before writing either source's data.
    return db.scalars(select(OpsCheckpoint).where(OpsCheckpoint.id == 1).with_for_update(read=True)).one()


def last_success_at(db: Session) -> datetime | None:
    return _published_state(db).last_success_at


def list_services(db: Session) -> list[ServiceItem]:
    services = db.scalars(select(OpsService).order_by(OpsService.service_id))
    return [*(ServiceItem.model_validate(service) for service in services), UNMATCHED]


def _service_filter(service_id: str | None) -> tuple:
    if service_id is None:
        return ()
    if service_id == UNMATCHED_SERVICE:
        return (OpsService.service_id.is_(None),)
    return (OpsService.service_id == service_id,)


def _rate(due: int, breached: int) -> float | None:
    return (due - breached) / due if due else None


def _service_breakdown(
    services: list[ServiceItem], counts: dict[str, tuple[int, int]],
) -> list[ServiceMetrics]:
    rows = []
    for service in services:
        due, breached = counts.get(service.service_id, (0, 0))
        rows.append(ServiceMetrics(
            **service.model_dump(), due_count=due, breached_count=breached,
            compliance_rate=_rate(due, breached),
        ))
    ranked = [row for row in rows if row.due_count and row.service_id != UNMATCHED_SERVICE]
    ranked.sort(key=lambda row: (Fraction(row.due_count - row.breached_count, row.due_count), row.service_id))
    for rank, row in enumerate(ranked, start=1):
        row.rank = rank
    return ranked + [row for row in rows if row.rank is None]


def get_metric(db: Session, period: OpsPeriod) -> MetricSnapshot:
    checkpoint = _published_state(db)
    service_id = period.service_id if isinstance(period, SlaQuery) else None
    fields = {'start_date': period.start_date, 'end_date': period.end_date, 'service_id': service_id}
    if checkpoint.last_success_at is None:
        return MetricSnapshot(status='not_ready', **fields)
    services = list_services(db)
    service = next((item for item in services if item.service_id == service_id), None)
    if service_id is not None and service is None:
        raise UnknownService('服务目录中没有该服务，请重新选择')
    groups = db.execute(
        select(OpsService.service_id, func.count(), func.count().filter(_breached()))
        .select_from(OpsTicket).outerjoin(OpsService, OpsTicket.service_id == OpsService.service_id)
        .where(*_due(period), *_service_filter(service_id)).group_by(OpsService.service_id),
    )
    counts = {key or UNMATCHED_SERVICE: (due, breached) for key, due, breached in groups}
    due = sum(values[0] for values in counts.values())
    breached = sum(values[1] for values in counts.values())
    return MetricSnapshot(
        status='ok', count=breached, due_count=due, compliance_rate=_rate(due, breached),
        service=service, service_breakdown=_service_breakdown(services, counts) if service is None else [],
        data_version=str(checkpoint.data_version), last_success_at=checkpoint.last_success_at, **fields,
    )


def get_ticket_page(
    db: Session, period: OpsPeriod, version: str, *, limit: int, offset: int,
    service_id: str | None = None, kind: DetailKind = 'breached',
) -> TicketPage:
    checkpoint = _published_state(db)
    if checkpoint.last_success_at is None or str(checkpoint.data_version) != version:
        raise MetricVersionChanged('数据版本已更新，请刷新指标后再查看明细')
    if service_id not in (None, UNMATCHED_SERVICE) and db.get(OpsService, service_id) is None:
        raise UnknownService('服务目录中没有该服务，请重新选择')

    joined = OpsTicket.__table__.outerjoin(OpsService, OpsTicket.service_id == OpsService.service_id)
    filters = (*_due(period), *_service_filter(service_id))
    if kind == 'breached':
        filters += (_breached(),)
    total = db.scalar(select(func.count()).select_from(joined).where(*filters))
    rows = db.execute(
        select(
            OpsTicket.ticket_id, OpsTicket.title, OpsTicket.created_at, OpsTicket.sla_due_at, OpsTicket.resolved_at,
            func.coalesce(OpsService.service_id, UNMATCHED_SERVICE).label('service_id'),
            func.coalesce(OpsService.name, UNMATCHED.name).label('service_name'),
            (~_breached()).label('sla_met'),
        ).select_from(joined).where(*filters)
        .order_by(OpsTicket.sla_due_at, OpsTicket.ticket_id).limit(limit).offset(offset),
    ).mappings()
    return TicketPage(items=[TicketItem.model_validate(row) for row in rows], total=total or 0, data_version=version)
