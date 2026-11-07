"""Offline graph contracts, version fencing, isolation and API regressions."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.models import Base, Document, Chunk
from app.models.graph import GraphJob, GraphState
from app.graph.extraction import Entity, Extraction, digest, extract
from app.graph.jobs import enqueue
from app.graph.retrieval import valid_paths, graph_candidates
from app.graph.worker import claim, process
from app.agent.runtime import RetrievalTrace
from app.rag.retriever import retrieve_chunks, validate_hits, _fuse_hits
from app.schemas.chat import ChatRequest
from app.schemas.retrieval import RetrievalSearchRequest


CONTENT = '苍穹系统依赖澄明组件。'
PAYLOAD = {'entities': [{'name': '苍穹系统', 'type': 'system'}, {'name': '澄明组件', 'type': 'component'}],
           'relations': [{'subject': 0, 'predicate': 'DEPENDS_ON', 'object': 1, 'evidence': CONTENT}]}


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.factory()
        self.settings = patch.object(get_settings(), 'graph_enabled', True)
        self.settings.start()
        self.add_doc('d1', 'c1', CONTENT)

    def tearDown(self):
        self.settings.stop()
        self.db.close()
        self.engine.dispose()

    def add_doc(self, did, cid, content, kb='default'):
        self.db.add(Document(id=did, filename=did+'.txt', file_type='txt', knowledge_base=kb))
        self.db.flush()
        self.db.add(Chunk(id=cid, document_id=did, chunk_index=0, content=content, metadata_json={}, enabled=True, token_count=10))
        self.db.commit()

    def build(self, did='d1', extraction=None):
        job = enqueue(self.db, did)
        self.db.commit()
        self.assertEqual(claim(self.factory), job.id)
        store = Mock()
        process(job.id, self.factory, store, lambda _: (extraction or Extraction.model_validate(PAYLOAD), {'input_tokens': 10}))
        self.db.expire_all()
        return self.db.get(GraphJob, job.id), store

    def path(self):
        job, store = self.build()
        rows = store.write.call_args.args[0]
        return {'entities': [{'id': rows[0]['subject_id'], 'name': '苍穹系统'},
                             {'id': rows[0]['object_id'], 'name': '澄明组件'}],
                'assertions': rows}

    def test_extraction_rejects_missing_evidence(self):
        extraction = Extraction.model_validate(PAYLOAD)
        extraction.relations[0].evidence = '原文没有的句子'
        with self.assertRaises(ValueError):
            extraction.validated_rows(content=CONTENT, chunk_id='c1', document_id='d1', knowledge_base='default', version='v', token='t')

    def test_entity_and_relation_type_whitelists(self):
        with self.assertRaises(ValidationError):
            Entity(name='x', type='unknown')
        with self.assertRaises(ValidationError):
            Extraction.model_validate({**PAYLOAD, 'relations': [{'subject': 0, 'object': 1, 'predicate': 'EXECUTE', 'evidence': CONTENT}]})

    def test_invalid_indices(self):
        e = Extraction.model_validate(PAYLOAD)
        e.relations[0].object = 9
        with self.assertRaises(ValueError):
            e.validated_rows(content=CONTENT, chunk_id='c1', document_id='d1', knowledge_base='default', version='v', token='t')

    def test_aliases_and_ambiguity_do_not_merge(self):
        a = Entity(name='组件', type='component', qualifier='计费系统', aliases=['组件A'])
        b = Entity(name='组件', type='component', qualifier='订单系统')
        self.assertNotEqual(a.identity('kb'), b.identity('kb'))
        self.assertNotEqual(a.identity('kb'), a.identity('other'))
        self.assertNotEqual(Entity(name='组件A', type='component').identity('kb'), a.identity('kb'))

    def test_same_canonical_identity_is_normalized(self):
        self.assertEqual(Entity(name='ＡＢＣ', type='system').identity('kb'), Entity(name='abc', type='system').identity('kb'))

    def test_duplicate_task_reused(self):
        a = enqueue(self.db, 'd1'); self.db.commit()
        b = enqueue(self.db, 'd1'); self.db.commit()
        self.assertEqual(a.id, b.id)
        self.assertEqual(len(self.db.scalars(select(GraphJob)).all()), 1)

    def test_enqueue_rolls_back_with_source_transaction(self):
        enqueue(self.db, 'd1'); self.db.rollback()
        self.assertIsNone(self.db.get(GraphState, 'd1'))
        self.assertEqual(self.db.scalars(select(GraphJob)).all(), [])

    def test_worker_publishes_complete_snapshot(self):
        job, store = self.build()
        self.assertEqual(job.status, 'succeeded')
        state = self.db.get(GraphState, 'd1')
        self.assertEqual(state.published_token, job.token)
        self.assertEqual(job.metrics['token_usage']['input_tokens'], 10)
        store.initialize.assert_called_once()

    def test_invalid_extraction_never_publishes(self):
        bad = Extraction.model_validate(PAYLOAD); bad.relations[0].evidence='不存在'
        job, _ = self.build(extraction=bad)
        self.assertEqual(job.status, 'pending')
        self.assertIsNone(self.db.get(GraphState, 'd1').published_token)

    def test_late_worker_cannot_publish(self):
        job = enqueue(self.db, 'd1'); self.db.commit(); claim(self.factory)
        def changing(_):
            with self.factory() as db, db.begin():
                db.get(Chunk, 'c1').content = '文档已更新'
                enqueue(db, 'd1')
            return Extraction.model_validate(PAYLOAD), {}
        process(job.id, self.factory, Mock(), changing)
        self.db.expire_all()
        self.assertEqual(self.db.get(GraphJob, job.id).status, 'expired')
        self.assertIsNone(self.db.get(GraphState, 'd1').published_token)

    def test_expired_lease_is_reclaimed_with_new_token(self):
        job = enqueue(self.db, 'd1'); self.db.commit(); claim(self.factory)
        self.db.expire_all(); job = self.db.get(GraphJob, job.id); old = job.token
        job.lease_until = 0; self.db.commit()
        self.assertEqual(claim(self.factory), job.id)
        self.db.expire_all()
        self.assertNotEqual(self.db.get(GraphJob, job.id).token, old)

    def test_reverting_content_requeues_previously_completed_version(self):
        job, _ = self.build()
        self.db.get(Chunk, 'c1').content = 'changed'; enqueue(self.db, 'd1'); self.db.commit()
        self.db.get(Chunk, 'c1').content = CONTENT; enqueue(self.db, 'd1'); self.db.commit()
        self.assertEqual(self.db.get(GraphJob, job.id).status, 'pending')

    def test_deleted_document_creates_cleanup_tombstone(self):
        self.build()
        self.db.delete(self.db.get(Document, 'd1'))
        job = enqueue(self.db, 'd1'); self.db.commit()
        self.assertEqual(job.operation, 'delete')
        self.assertEqual(self.db.get(GraphState, 'd1').desired_version, 'deleted')

    def test_paths_discard_disabled_changed_deleted_and_other_kb(self):
        path = self.path()
        self.assertEqual(len(valid_paths(self.db, [path], 'default')), 1)
        self.assertEqual(valid_paths(self.db, [path], 'other'), [])
        chunk = self.db.get(Chunk, 'c1'); chunk.enabled = False; self.db.commit()
        self.assertEqual(valid_paths(self.db, [path], 'default'), [])
        chunk.enabled = True; chunk.content = 'changed'; self.db.commit()
        self.assertEqual(valid_paths(self.db, [path], 'default'), [])
        self.db.delete(chunk); self.db.commit()
        self.assertEqual(valid_paths(self.db, [path], 'default'), [])

    def test_stale_token_is_not_visible(self):
        path = self.path(); path['assertions'][0]['token'] = 'late-token'
        self.assertEqual(valid_paths(self.db, [path], 'default'), [])

    def test_no_database_transaction_spans_model_call(self):
        job = enqueue(self.db, 'd1'); self.db.commit(); claim(self.factory)
        def checking(_):
            # Worker closes its snapshot transaction before invoking extraction.
            self.assertEqual(self.db.in_transaction(), False)
            return Extraction.model_validate(PAYLOAD), {}
        process(job.id, self.factory, Mock(), checking)

    def test_three_route_rrf_deduplicates(self):
        row = {'chunk_id':'c1', 'content':CONTENT, 'score':1}
        hits = _fuse_hits(query='q', vector_hits=[row], bm25_hits=[row], graph_hits=[{**row, 'graph_paths':[]}], top_k=5)
        self.assertEqual(len(hits), 1)
        self.assertAlmostEqual(hits[0]['fused_score'], 3/61)
        self.assertEqual(hits[0]['rank_graph'], 1)

    def test_authoritative_filter_rejects_stale_and_foreign_hits(self):
        self.assertEqual(validate_hits(self.db, [{'chunk_id':'c1', 'content':'stale'}]), [])
        self.assertEqual(validate_hits(self.db, [{'chunk_id':'c1', 'content':CONTENT}], 'other'), [])

    def test_graph_outage_degrades_with_diagnostics(self):
        vector = [{'chunk_id':'c1', 'content':CONTENT, 'score':1}]
        with patch('app.rag.retriever._vector_retrieve_chunks', return_value=vector), \
             patch('app.rag.retriever._bm25_retrieve_chunks', return_value=[]), \
             patch('app.graph.retrieval.graph_candidates', side_effect=TimeoutError()):
            info = {}
            hits = retrieve_chunks(self.db, query='苍穹系统', retrieval_mode='graph_hybrid', knowledge_base='default', diagnostics=info)
        self.assertEqual(info['graph_status'], 'degraded')
        self.assertEqual(hits[0]['chunk_id'], 'c1')

    def test_each_hop_requires_current_support(self):
        path = self.path()
        path['entities'].append({'id':'rule', 'name':'规则'})
        path['assertions'].append({**path['assertions'][0], 'id':'missing', 'chunk_id':'missing'})
        self.assertEqual(valid_paths(self.db, [path], 'default'), [])

    def test_stable_citation_registry_survives_empty_and_repeated_search(self):
        trace = RetrievalTrace()
        self.assertEqual(trace.register([{'chunk_id':'a'}])[0]['ref_id'], 1)
        trace.register([])
        refs = trace.register([{'chunk_id':'b'}, {'chunk_id':'a', 'graph_paths':[{'entities':[]}]}])
        self.assertEqual([r['ref_id'] for r in refs], [2, 1])
        self.assertEqual(len(trace.source_chunks), 2)
        self.assertTrue(trace.source_chunks[0]['graph_paths'])

    def test_graph_requests_require_scope_old_requests_work(self):
        self.assertEqual(RetrievalSearchRequest(query='q').retrieval_mode, 'hybrid')
        with self.assertRaises(ValidationError):
            ChatRequest(session_id='s', message='q', retrieval_mode='graph_hybrid')
        with self.assertRaises(ValidationError):
            RetrievalSearchRequest(query='q', knowledge_base='  ')

    def test_graph_api_batch_is_atomic_and_idempotent(self):
        from app.api.v1.graph import router
        from app.core.deps import get_database
        app = FastAPI(); app.include_router(router)
        def dependency():
            with self.factory() as db:
                yield db
        app.dependency_overrides[get_database] = dependency
        client = TestClient(app)
        self.assertEqual(client.post('/graph/build', json={'document_ids':['d1','missing']}).status_code, 404)
        a = client.post('/graph/build', json={'document_ids':['d1']})
        b = client.post('/graph/build', json={'document_ids':['d1']})
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.json()['items'][0]['id'], b.json()['items'][0]['id'])

    def test_model_timeout_is_forwarded_to_sdk(self):
        response = SimpleNamespace(content='{"entities":[],"relations":[]}', response_metadata={})
        with patch('langchain_community.chat_models.tongyi.ChatTongyi') as model:
            model.return_value.invoke.return_value = response
            extract('text')
        self.assertEqual(model.call_args.kwargs['max_retries'], 1)
        self.assertEqual(model.call_args.kwargs['model_kwargs']['request_timeout'], get_settings().graph_model_timeout_seconds)


if __name__ == '__main__':
    unittest.main()

class ChatIntegrationTests(unittest.TestCase):
    setUp = GraphTests.setUp
    tearDown = GraphTests.tearDown
    add_doc = GraphTests.add_doc
    # Reuse isolated relational fixtures without invoking remote models.
    def test_chat_history_preserves_graph_diagnostics_and_paths(self):
        from app.services.chat_service import ChatService
        from langchain_core.messages import AIMessage
        agent = Mock()
        from app.agent.tools import search_knowledge_base
        def invoke(*args, **kwargs):
            search_knowledge_base.invoke({'query': '问题'})
            return {'messages': [AIMessage(content='回答 [1]')]}
        agent.invoke.side_effect = invoke
        hit = {'chunk_id':'c1', 'document_id':'d1', 'content':CONTENT, 'score':1,
               'graph_paths':[], 'rank_graph':1}
        def retrieve(*_, diagnostics=None, **kwargs):
            diagnostics.update(graph_status='degraded', degradation_reason='测试降级')
            return [hit]
        with patch('app.services.chat_service.get_session_factory', return_value=self.factory), \
             patch('app.services.chat_service.get_rag_agent', return_value=agent), \
             patch('app.agent.tools.retrieve_chunks', side_effect=retrieve), \
             patch('app.agent.tools.get_session_factory', return_value=self.factory):
            service = ChatService()
            response = service.invoke(session_id='s', message='问题', retrieval_mode='graph_hybrid', knowledge_base='default')
            self.assertEqual(response.graph_status, 'degraded')
            history = service.get_session_history('s')
            self.assertEqual(history[0].degradation_reason, '测试降级')
            self.assertEqual(history[0].source_chunks[0].ref_id, 1)
            self.assertEqual(history[0].source_chunks[0].rank_graph, 1)

    def test_stream_emits_diagnostics_and_stable_sources(self):
        from app.services.chat_service import ChatService
        from langchain_core.messages import AIMessageChunk
        agent = Mock()
        from app.agent.tools import search_knowledge_base
        def stream(*args, **kwargs):
            search_knowledge_base.invoke({'query': '问题'})
            yield ('messages', (AIMessageChunk(content='回答 [1]'), {'langgraph_node': 'model'}))
        agent.stream.side_effect = stream
        def retrieve(*_, diagnostics=None, **kwargs):
            diagnostics.update(graph_status='no_hits', degradation_reason=None)
            return [{'chunk_id':'c1', 'content':CONTENT, 'score':1}]
        with patch('app.services.chat_service.get_session_factory', return_value=self.factory), \
             patch('app.services.chat_service.get_rag_agent', return_value=agent), \
             patch('app.agent.tools.retrieve_chunks', side_effect=retrieve), \
             patch('app.agent.tools.get_session_factory', return_value=self.factory):
            output = ''.join(ChatService().stream(session_id='s', message='问题', retrieval_mode='graph_hybrid', knowledge_base='default'))
        self.assertIn('event: sources', output)
        self.assertIn('no_hits', output)
        self.assertIn('event: done', output)
        self.assertIn('"ref_id": 1', output)

    def test_store_binds_query_named_parameter_without_collision(self):
        from app.graph.store import GraphStore
        store = GraphStore.__new__(GraphStore)
        store.timeout = 5
        store.driver = Mock()
        class FakeSession:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def run(self, query, parameters):
                self.params = parameters
                return SimpleNamespace(data=lambda: [parameters])
        session = FakeSession(); store.driver.session.return_value = session
        self.assertEqual(store.run('RETURN $query', query='企业知识')[0]['query'], '企业知识')

class ConcurrentCitationTests(unittest.TestCase):
    def test_parallel_tools_share_unique_stable_refs_and_degradation(self):
        from concurrent.futures import ThreadPoolExecutor
        trace=RetrievalTrace()
        def register(index):
            trace.register([{'chunk_id':str(index%10)}])
            trace.record_diagnostics({'graph_status':'degraded' if index==0 else 'ok', 'degradation_reason':'临时故障' if index==0 else None})
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(register,range(80)))
        self.assertEqual(len(trace.source_chunks),10)
        self.assertEqual(sorted(h['ref_id'] for h in trace.source_chunks),list(range(1,11)))
        self.assertEqual(trace.diagnostics['graph_status'],'degraded')
