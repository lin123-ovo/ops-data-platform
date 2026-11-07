from concurrent.futures import ThreadPoolExecutor
import json
from datetime import date, datetime, timedelta, timezone
from os import environ
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from app.config import get_settings
from app.core.postgres import get_engine
from app.main import app
from app.models.base import Base
from app.models.graph import GraphChatMeta
from app.models.query_log import QueryLog
from app.ops.metrics import (
    OPS_KNOWLEDGE_BASE, OPS_TIMEZONE, UNMATCHED_SERVICE, MetricSnapshot, MetricVersionChanged,
    OpsPeriod, SlaQuery, get_metric, get_ticket_page,
)
from app.ops.models import OpsBase, OpsCheckpoint, OpsService, OpsSyncRun, OpsTicket
from app.ops.sync import (
    LEASE_SECONDS, SourceBatch, SourceTicket, _validate_batch, claim_run, enqueue, read_source, sync_once,
)
from app.services.chat_service import ChatService

PERIOD = OpsPeriod(start_date=date(2024, 1, 1), end_date=date(2024, 1, 8))
START = datetime(2024, 1, 1, tzinfo=OPS_TIMEZONE)
END = START + timedelta(days=7)


def ticket(
    ticket_id: str, revision: int, due: datetime, resolved_at: datetime | None = None,
    *, service_id: str | None = None,
) -> dict:
    return {
        'ticket_id': ticket_id, 'title': 'Synthetic work order', 'revision': revision,
        'created_at': due - timedelta(hours=2), 'sla_due_at': due, 'resolved_at': resolved_at,
        'service_id': service_id,
    }


class OpsValidationTests(unittest.TestCase):
    def test_invalid_ranges(self):
        for start, end in [(date(2024, 1, 1), date(2024, 1, 1)), (date(2024, 1, 1), date(2024, 4, 2))]:
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                OpsPeriod(start_date=start, end_date=end)
        today = datetime.now(OPS_TIMEZONE).date()
        with self.assertRaises(ValidationError):
            OpsPeriod(start_date=today, end_date=today + timedelta(days=1))

    def test_bad_source_times_and_revisions(self):
        for row in [
            ticket('bad', 1, START, START - timedelta(hours=3)),
            ticket('bad', 1, START.replace(tzinfo=None)),
            ticket('bad', 0, START),
            {**ticket('bad', 1, START), 'title': ' '},
        ]:
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, '校验失败'):
                _validate_batch([row], 0, SourceTicket)
        with self.assertRaisesRegex(ValueError, '重复'):
            _validate_batch([ticket('same', 1, START), ticket('same', 2, END)], 0, SourceTicket)
        with patch('app.ops.sync.MAX_BATCH', 1), self.assertRaisesRegex(ValueError, '超过演示上限'):
            _validate_batch([ticket('one', 1, START), ticket('two', 2, END)], 0, SourceTicket)

    def test_feature_is_hidden_and_range_validated_before_chat(self):
        client = TestClient(app)
        self.addCleanup(client.close)
        with patch.object(get_settings(), 'ops_enabled', False):
            self.assertEqual(client.get('/api/v1/ops/sync-runs').status_code, 404)
            self.assertEqual(client.post('/api/v1/chat', json={
                'mode': 'ops', 'message': 'demo', 'session_id': 'ops-test',
            }).status_code, 404)
        with patch.object(get_settings(), 'ops_enabled', True):
            self.assertEqual(client.post('/api/v1/chat/stream', json={
                'mode': 'ops', 'message': 'demo', 'session_id': 'ops-test',
            }).status_code, 422)

    def test_chat_persists_the_same_metric_for_sync_stream_and_history_without_manual(self):
        engine = create_engine('sqlite://')
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine, tables=[QueryLog.__table__, GraphChatMeta.__table__])
        factory = sessionmaker(engine, expire_on_commit=False)
        expected = MetricSnapshot(
            status='ok', count=3, due_count=4, compliance_rate=0.25, service_id='gateway',
            data_version='5', **PERIOD.model_dump(),
        )
        agent = Mock()
        with (
            patch('app.services.chat_service.get_session_factory', return_value=factory),
            patch('app.services.chat_service.get_rag_agent', return_value=agent),
            patch('app.services.chat_service.get_metric', return_value=expected),
            patch('app.services.chat_service.retrieve_chunks', return_value=[]) as retrieval,
        ):
            service = ChatService()
            kwargs = {'session_id': 's', 'message': '多少工单超时？', 'ops_period': PERIOD}
            response = service.invoke(**kwargs)
            stream = ''.join(service.stream(**kwargs))
            history = service.get_session_history('s')
            self.assertEqual(response.metric_snapshot, expected)
            self.assertIn('未检索到相关运维手册', response.answer)
            event = next(item for item in stream.split('\n\n') if item.startswith('event: metric_snapshot'))
            payload = json.loads(event.split('\ndata: ', 1)[1])
            self.assertEqual(payload, expected.model_dump(mode='json'))
            self.assertEqual([item.metric_snapshot for item in history], [expected, expected])
            with factory.begin() as db:
                first = db.get(GraphChatMeta, history[0].id)
                first.diagnostics = {'metric_snapshot': expected.model_dump(mode='json', include={
                    'metric_id', 'status', 'start_date', 'end_date', 'count', 'data_version',
                })}
            legacy = service.get_session_history('s')[0].metric_snapshot
            self.assertEqual(legacy.count, 3)
            self.assertIsNone(legacy.due_count)
            self.assertEqual([call.kwargs['knowledge_base'] for call in retrieval.call_args_list],
                             [OPS_KNOWLEDGE_BASE, OPS_KNOWLEDGE_BASE])
            agent.invoke.assert_not_called()
            agent.stream.assert_not_called()

        with factory() as db:
            self.assertEqual(set(db.scalars(select(GraphChatMeta.thread_id))), {'s:ops'})

    def test_english_ops_prompt_and_unavailable_runbook(self):
        snapshot = MetricSnapshot(status='ok', count=2, **PERIOD.model_dump())
        agent = Mock()
        with (
            patch('app.services.chat_service.get_rag_agent', return_value=agent),
            patch.object(ChatService, '_ops_metric', return_value=snapshot),
            patch.object(ChatService, '_persist_query_log'),
            patch.object(ChatService, '_prefetch_source_chunks', return_value=[]) as prefetch,
        ):
            service = ChatService()
            request = {
                'session_id': 'ops-en', 'message': 'Which service breached its SLA?',
                'ops_period': PERIOD, 'reply_language': 'en',
            }
            answer = service.invoke(**request)
            stream = ''.join(service.stream(**request))
            messages = service._build_agent_input(request['message'], ops=True, reply_language='en')['messages']
        self.assertEqual(answer.answer, 'No relevant operations runbook was found.')
        self.assertIn('No relevant operations runbook was found.', stream)
        self.assertTrue(any('in English' in item['content'] for item in messages))
        self.assertIn('Cite each step', messages[-1]['content'])
        self.assertTrue(prefetch.call_args.kwargs['message'].startswith(request['message']))


@unittest.skipUnless(environ.get('OPS_TEST_POSTGRES') == '1', 'requires opt-in local PostgreSQL')
class OpsPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = 'ops_test_' + uuid4().hex
        self.admin = get_engine()
        with self.admin.begin() as connection:
            connection.execute(CreateSchema(self.schema))
        self.addCleanup(self.drop_test_schema)
        self.engine = create_engine(self.admin.url, connect_args={'options': f'-c search_path={self.schema}'})
        self.addCleanup(self.engine.dispose)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        OpsBase.metadata.create_all(self.engine)
        with self.factory.begin() as db:
            db.add(OpsCheckpoint(id=1, revision=0))

    def drop_test_schema(self):
        with self.admin.begin() as connection:
            connection.execute(DropSchema(self.schema, cascade=True))

    def submit(self) -> str:
        with self.factory.begin() as db:
            return enqueue(db, 'manual').id

    def snapshot(self):
        with self.factory() as db:
            return get_metric(db, PERIOD)

    def test_cutoffs_pagination_noop_and_corrected_ticket(self):
        self.assertEqual(self.snapshot().status, 'not_ready')
        source = [
            ticket('start', 1, START),
            ticket('early', 2, START, START - timedelta(minutes=1)),
            ticket('exact', 3, START, START),
            ticket('late', 4, START + timedelta(days=1), START + timedelta(days=2)),
            ticket('before', 5, START - timedelta(seconds=1)),
            ticket('end', 6, END),
        ]
        sync_once(self.factory, lambda *_: SourceBatch(source, []))
        published = self.snapshot()
        self.assertEqual((published.count, published.data_version), (2, '1'))
        with self.factory() as db:
            first = get_ticket_page(db, PERIOD, '1', limit=1, offset=0)
            second = get_ticket_page(db, PERIOD, '1', limit=1, offset=1)
            self.assertEqual(first.total, 2)
            self.assertEqual([first.items[0].ticket_id, second.items[0].ticket_id], ['start', 'late'])
            empty = OpsPeriod(start_date=date(2024, 1, 3), end_date=date(2024, 1, 4))
            self.assertEqual(get_metric(db, empty).count, 0)

        self.submit()
        sync_once(self.factory, lambda revision, _: SourceBatch([row for row in source if row['revision'] > revision], []))
        self.assertEqual(self.snapshot().data_version, '1')
        self.submit()
        sync_once(self.factory, lambda *_: SourceBatch([ticket('start', 7, START, START)], []))
        self.assertEqual((self.snapshot().count, self.snapshot().data_version), (1, '2'))
        with self.factory() as db, self.assertRaises(MetricVersionChanged):
            get_ticket_page(db, PERIOD, '1', limit=20, offset=0)

    def test_failures_leave_cursor_and_published_rows_unchanged(self):
        sync_once(self.factory, lambda *_: SourceBatch([ticket('stable', 1, START)], []))
        published = self.snapshot()

        def unavailable(*_revisions: int):
            raise OperationalError('simulated failure', {}, RuntimeError('offline'))

        def expect_failure(read, *, rows_read: int):
            run_id = self.submit()
            sync_once(self.factory, read)
            with self.factory() as db:
                run = db.get(OpsSyncRun, run_id)
                self.assertEqual((run.status, run.rows_read, run.rows_written), ('failed', rows_read, 0))
                self.assertEqual(db.scalars(select(func.count()).select_from(OpsTicket)).one(), 1)
            self.assertEqual(self.snapshot(), published)

        expect_failure(unavailable, rows_read=0)
        expect_failure(lambda *_: SourceBatch([ticket('bad', 2, START, START - timedelta(days=1))], []), rows_read=1)

        def fail_after_writing(db: Session, _claim, _tickets, _services) -> None:
            db.add(OpsTicket(**ticket('rolled-back', 2, START)))
            db.flush()
            unavailable(1)

        with patch('app.ops.sync._publish', side_effect=fail_after_writing):
            expect_failure(lambda *_: SourceBatch([ticket('valid', 2, START)], []), rows_read=1)
        self.submit()
        sync_once(self.factory, lambda *_: SourceBatch([ticket('valid', 2, START)], []))
        self.assertEqual((self.snapshot().count, self.snapshot().data_version), (2, '2'))

    def test_concurrent_triggers_and_expired_worker(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            manual = executor.submit(self.submit)

            def schedule() -> str:
                with self.factory.begin() as db:
                    return enqueue(db, 'scheduled').id

            scheduled = executor.submit(schedule)
            run_id = manual.result(timeout=10)
            self.assertEqual(run_id, scheduled.result(timeout=10))
        with self.factory.begin() as db:
            claim = claim_run(db)
            self.assertEqual(claim.run_id, run_id)
        with self.factory.begin() as db:
            self.assertIsNone(claim_run(db))
            expired = db.get(OpsSyncRun, run_id)
            expired.started_at = datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS + 1)
            expired.queued_at = expired.started_at
        recovered = sync_once(self.factory, lambda *_: SourceBatch([ticket('after-restart', 1, START)], []))
        self.assertNotEqual(run_id, recovered)
        with self.factory() as db:
            self.assertEqual(db.get(OpsSyncRun, run_id).status, 'failed')
            self.assertEqual(db.get(OpsSyncRun, recovered).status, 'succeeded')
        self.assertEqual(self.snapshot().count, 1)

    def test_service_ranking_unmatched_and_due_drilldown(self):
        def service(key: str, revision: int) -> dict:
            return {'service_id': key, 'name': key.upper(), 'team': '运维组', 'revision': revision}

        batch = SourceBatch(
            tickets=[
                ticket('alpha-breach', 1, START, service_id='alpha'),
                ticket('alpha-pass', 2, START, START, service_id='alpha'),
                ticket('beta-breach', 3, START, service_id='beta'),
                ticket('beta-pass', 4, START, START, service_id='beta'),
                ticket('unknown', 5, START, service_id='missing'),
                ticket('legacy', 6, START, START),
                ticket('worse', 7, START, service_id='worse'),
            ],
            services=[service('beta', 1), service('alpha', 2), service('empty', 3), service('worse', 4)],
        )
        sync_once(self.factory, lambda *_: batch)
        with self.factory() as db:
            snapshot = get_metric(db, PERIOD)
            self.assertEqual((snapshot.due_count, snapshot.count), (7, 4))
            self.assertEqual([(row.service_id, row.rank) for row in snapshot.service_breakdown], [
                ('worse', 1), ('alpha', 2), ('beta', 3), ('empty', None), (UNMATCHED_SERVICE, None),
            ])
            self.assertIsNone(snapshot.service_breakdown[3].compliance_rate)
            self.assertEqual(snapshot.service_breakdown[4].due_count, 2)
            alpha = get_metric(db, SlaQuery(**PERIOD.model_dump(), service_id='alpha'))
            self.assertEqual((alpha.due_count, alpha.count, alpha.compliance_rate), (2, 1, 0.5))
            self.assertEqual(alpha.service.team, '运维组')
            page = get_ticket_page(db, PERIOD, '1', limit=10, offset=0, service_id='alpha', kind='due')
            self.assertEqual([(row.ticket_id, row.sla_met) for row in page.items], [
                ('alpha-breach', False), ('alpha-pass', True),
            ])
            unmatched = get_ticket_page(
                db, PERIOD, '1', limit=10, offset=0, service_id=UNMATCHED_SERVICE, kind='due',
            )
            self.assertEqual([row.ticket_id for row in unmatched.items], ['legacy', 'unknown'])

        self.submit()
        updated = {**service('alpha', 5), 'name': '改名服务'}
        sync_once(self.factory, lambda *_: SourceBatch([], [updated]))
        with self.factory() as db:
            new = get_metric(db, SlaQuery(**PERIOD.model_dump(), service_id='alpha'))
            self.assertEqual((new.data_version, new.service.name, new.count), ('2', '改名服务', 1))
            with self.assertRaises(MetricVersionChanged):
                get_ticket_page(db, PERIOD, '1', limit=10, offset=0, service_id='alpha')

    def test_failure_of_second_source_read_and_write_rolls_back_both(self):
        stable = {'service_id': 'stable', 'name': 'Stable', 'team': 'Operations', 'revision': 1}
        sync_once(self.factory, lambda *_: SourceBatch([ticket('stable', 1, START)], [stable]))
        published = self.snapshot()
        new_ticket = ticket('new', 2, START, service_id='new')
        new_service = {'service_id': 'new', 'name': 'Rejected', 'team': 'Operations', 'revision': 2}

        connection = Mock()
        connection.execute.side_effect = [
            Mock(**{'mappings.return_value.all.return_value': [new_ticket]}),
            OperationalError('simulated directory outage', {}, RuntimeError('offline')),
        ]
        source = Mock()
        source.connect.return_value.__enter__ = Mock(return_value=connection)
        source.connect.return_value.__exit__ = Mock(return_value=False)
        self.submit()
        with patch('app.ops.sync.source_engine', return_value=source):
            sync_once(self.factory, lambda revision, directory: read_source('mock', revision, directory))
        self.assertEqual(connection.execute.call_count, 2)
        connection.exec_driver_sql.assert_called_once_with('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        self.assertEqual(self.snapshot(), published)

        with self.factory.begin() as db:
            db.execute(text("ALTER TABLE ops_service ADD CONSTRAINT reject_demo CHECK (name <> 'Rejected')"))
        self.submit()
        sync_once(self.factory, lambda *_: SourceBatch([new_ticket], [new_service]))
        self.assertEqual(self.snapshot(), published)
        with self.factory() as db:
            checkpoint = db.get(OpsCheckpoint, 1)
            self.assertEqual((checkpoint.revision, checkpoint.service_revision, checkpoint.data_version), (1, 1, 1))
            self.assertIsNone(db.get(OpsTicket, 'new'))
            self.assertIsNone(db.get(OpsService, 'new'))

        self.submit()
        sync_once(self.factory, lambda *_: SourceBatch([new_ticket], [{**new_service, 'name': 'Recovered'}]))
        self.assertEqual((self.snapshot().count, self.snapshot().data_version), (2, '2'))
