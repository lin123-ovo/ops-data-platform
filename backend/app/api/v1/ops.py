from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.postgres import get_db
from app.ops.metrics import (
    METRIC_ID, DetailKind, LegacyMetricSnapshot, MetricSnapshot, MetricVersionChanged, OpsPeriod,
    ServiceItem, SlaQuery, TicketPage, UnknownService, get_metric, get_ticket_page, last_success_at, list_services,
)
from app.ops.models import OpsCheckpoint, OpsSyncRun
from app.ops.sync import POLL_SECONDS, enqueue


def require_ops() -> None:
    if not get_settings().ops_enabled:
        raise HTTPException(status_code=404, detail='运维数据演示尚未启用')


router = APIRouter(prefix='/ops', tags=['ops'], dependencies=[Depends(require_ops)])
Database = Annotated[Session, Depends(get_db)]
Period = Annotated[OpsPeriod, Query()]


class SyncRunItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    trigger: str
    status: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    rows_read: int
    rows_written: int
    error: str | None


class SyncRunList(BaseModel):
    items: list[SyncRunItem]
    last_success_at: datetime | None


@router.post('/sync-runs', response_model=SyncRunItem, status_code=202)
def request_sync(request: Request, db: Database) -> SyncRunItem:
    origin = request.headers.get('origin')
    try:
        origin_host = urlsplit(origin).hostname if origin else None
    except ValueError as exc:
        raise HTTPException(status_code=403, detail='请求来源无效') from exc
    if origin and origin_host not in {'127.0.0.1', 'localhost', '::1'}:
        raise HTTPException(status_code=403, detail='仅接受本机工作台发起的同步')
    try:
        run = SyncRunItem.model_validate(enqueue(db, 'manual'))
        db.commit()
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='无法提交同步，请检查中台数据库及迁移') from exc
    return run


@router.get('/sync-runs', response_model=SyncRunList)
def list_sync_runs(db: Database, limit: Annotated[int, Query(ge=1, le=100)] = 20) -> SyncRunList:
    try:
        runs = db.scalars(select(OpsSyncRun).order_by(OpsSyncRun.queued_at.desc()).limit(limit))
        items = [SyncRunItem.model_validate(run) for run in runs]
        checkpoint = db.get(OpsCheckpoint, 1)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='无法读取同步记录，请检查中台数据库及迁移') from exc
    return SyncRunList(items=items, last_success_at=checkpoint.last_success_at if checkpoint else None)


@router.get('/metrics/sla-breached-tickets', response_model=LegacyMetricSnapshot)
def metric(period: Period, db: Database) -> LegacyMetricSnapshot:
    try:
        return get_metric(db, period)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='取数失败，请检查中台数据库及迁移') from exc


@router.get('/services')
def services(db: Database) -> list[ServiceItem]:
    try:
        return list_services(db)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='服务目录暂不可用，请检查中台数据库及迁移') from exc


@router.get('/metrics/sla')
def sla_metrics(query: Annotated[SlaQuery, Query()], db: Database) -> MetricSnapshot:
    try:
        return get_metric(db, query)
    except UnknownService as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='取数失败，请检查中台数据库及迁移') from exc


class CatalogMetric(BaseModel):
    metric_id: str
    name: str
    formula: str
    scope: str
    source_tables: list[str]


class MetricCatalog(BaseModel):
    items: list[CatalogMetric]
    sync_interval_seconds: int
    last_success_at: datetime | None


@router.get('/metrics/catalog')
def metric_catalog(db: Database, language: Literal['zh', 'en'] = 'zh') -> MetricCatalog:
    try:
        synced_at = last_success_at(db)
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='指标目录暂不可用，请检查中台数据库及迁移') from exc
    if language == 'en':
        scope = 'Uses SLA deadlines in Hong Kong time. The end date is exclusive. Totals include unmatched tickets.'
        entries = [
            ('sla_due_ticket_count', 'Tickets due', 'All tickets with an SLA deadline in the selected date range'),
            (METRIC_ID, 'SLA breaches', 'Due tickets that are unresolved or were resolved after the deadline'),
            ('sla_compliance_rate', 'SLA compliance', '(Tickets due − SLA breaches) ÷ tickets due. No data if none are due.'),
        ]
    else:
        scope = '按香港时间的 SLA 截止日期统计，区间不含结束日。全部服务包含未匹配工单。'
        entries = [
            ('sla_due_ticket_count', '到期工单数', '区间内到期的工单总数'),
            (METRIC_ID, '超时工单数', '到期且尚未解决或晚于截止时间解决的工单数'),
            ('sla_compliance_rate', 'SLA 达标率', '（到期工单数 − 超时工单数）÷ 到期工单数。分母为零时显示无数据。'),
        ]
    return MetricCatalog(
        items=[
            CatalogMetric(metric_id=key, name=name, formula=formula,
                          scope=scope, source_tables=['public.demo_ticket', 'public.demo_service'])
            for key, name, formula in entries
        ],
        sync_interval_seconds=POLL_SECONDS, last_success_at=synced_at,
    )


class TicketQuery(SlaQuery):
    kind: DetailKind = 'breached'
    data_version: str = Field(pattern=r'^\d{1,19}$')
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=2_147_483_647)


@router.get('/metrics/sla-breached-tickets/items', response_model=TicketPage)
def ticket_items(
    query: Annotated[TicketQuery, Query()], db: Database,
) -> TicketPage:
    try:
        return get_ticket_page(
            db, OpsPeriod(start_date=query.start_date, end_date=query.end_date),
            query.data_version, limit=query.limit, offset=query.offset, service_id=query.service_id, kind=query.kind,
        )
    except UnknownService as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MetricVersionChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(status_code=503, detail='读取工单明细失败，请检查中台数据库') from exc
