"""Offline tool selection, Alibaba Cloud HTTP contracts, citations, and history regressions."""
from __future__ import annotations

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent.runtime import RetrievalTrace, bind_retrieval_trace, get_current_retrieval_trace, preserve_stream_context
from app.agent.tools import get_agent_tools
from app.agent.graph import validate_citations
from app.agent.web_search import WebSearchInput, run_web_search
from app.config import get_settings
from app.models import Base
from app.models.graph import GraphChatMeta
from app.models.query_log import QueryLog
from app.services.chat_service import ChatService


class ScriptedModel(BaseChatModel):
    """Run the real agent loop with predetermined model decisions and no cloud access."""
    responses: list[AIMessage]

    @property
    def _llm_type(self):
        return 'scripted-tools'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self.responses.pop(0))])


def web_result(url='https://example.com/docs', content='Public documentation'):
    return {'title': 'Documentation', 'url': url, 'content': content, 'published_date': '2026-11-01'}


def aliyun_response(sources, summary='Search summary [ref_1]'):
    results = []
    for index, source in enumerate(sources, 1):
        result = {**source, 'index': index}
        result['snippet'] = result.pop('content', '')
        results.append(result)
    return {'output': {
        'choices': [{'message': {'content': summary}}],
        'search_info': {'search_results': results},
    }}


def events(stream):
    result = []
    for packet in stream.split('\n\n'):
        if packet.strip():
            name, data = packet.split('\ndata: ', 1)
            result.append((name.removeprefix('event: '), json.loads(data)))
    return result


class AliyunSearchTests(unittest.TestCase):
    def setUp(self):
        self.settings = get_settings()
        self.key = patch.object(self.settings, 'dashscope_api_key', 'test-dashscope-key')
        enabled = patch.object(self.settings, 'aliyun_web_search_enabled', True)
        enabled.start()
        self.addCleanup(enabled.stop)
        self.key.start()
        self.addCleanup(self.key.stop)

    def client(self, handler):
        original = httpx.Client
        return patch('app.agent.web_search.httpx.Client', side_effect=lambda **kwargs: original(
            transport=httpx.MockTransport(handler), **kwargs,
        ))

    def test_payload_limits_url_filtering_and_duplicate_citations(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=aliyun_response([
                web_result(), web_result('https://example.com/docs#same'),
                web_result('javascript:alert(1)'), web_result('https://other.example/page'),
            ], 'First [ref_1], duplicate [ref_2], unsafe [ref_3], second [ref_4]'))
        trace = RetrievalTrace()
        with self.client(handler):
            first = run_web_search(trace, 'latest  release', 'news', 'week')
            cached = run_web_search(trace, 'latest release', 'news', 'week')
            second = run_web_search(trace, 'other release', 'general', None)
            rejected = run_web_search(trace, 'third release', 'general', None)
        self.assertEqual(first, cached)
        self.assertEqual(len(requests), 2)
        payload = json.loads(requests[0].content)
        self.assertEqual(set(payload), {'model', 'input', 'parameters'})
        self.assertEqual(payload['model'], self.settings.aliyun_web_search_model)
        self.assertTrue(payload['parameters']['enable_search'])
        options = payload['parameters']['search_options']
        self.assertTrue(options['forced_search'])
        self.assertTrue(options['enable_source'])
        self.assertTrue(options['enable_citation'])
        self.assertEqual(options['citation_format'], '[ref_<number>]')
        self.assertEqual(options['freshness'], 7)
        self.assertEqual(options['search_strategy'], 'turbo')
        self.assertEqual(payload['input']['messages'][-1]['content'], 'latest release')
        self.assertEqual(requests[0].headers['authorization'], 'Bearer test-dashscope-key')
        self.assertEqual(first['summary'], 'First [W1], duplicate [W1], unsafe , second [W2]')
        self.assertEqual([s['ref_id'] for s in first['sources']], ['W1', 'W2'])
        self.assertEqual([s['ref_id'] for s in second['sources']], ['W1', 'W2'])
        self.assertIn('limit', rejected['error'])
        self.assertEqual(trace.route(), 'agent_web')

    def test_unconfigured_key_and_failures_are_visible_without_leaking_provider_details(self):
        with patch.object(self.settings, 'dashscope_api_key', ''), self.client(lambda _: self.fail('Unexpected network')):
            result = run_web_search(RetrievalTrace(), 'latest', 'general', None)
        self.assertEqual(result['status'], 'unavailable')
        for status in (400, 401, 403, 402, 429, 500):
            with self.subTest(status=status), self.client(lambda _: httpx.Response(status, text='private-provider-detail')):
                trace = RetrievalTrace()
                result = run_web_search(trace, 'latest', 'general', None)
                repeated = run_web_search(trace, 'latest', 'general', None)
                self.assertEqual(result, repeated)
                self.assertEqual(result['status'], 'error')
                self.assertNotIn('private-provider-detail', json.dumps(trace.web_snapshot()))
                self.assertNotIn('test-dashscope-key', json.dumps(trace.web_snapshot()))

    def test_empty_malformed_and_timeout_responses(self):
        for response in (aliyun_response([]), {}, {'output': {'search_info': {'search_results': 'invalid'}}}, aliyun_response([{}])):
            with self.subTest(response=response), self.client(lambda _: httpx.Response(200, json=response)):
                result = run_web_search(RetrievalTrace(), 'latest', 'general', None)
                self.assertIn(result['status'], ('no_results', 'error'))
        def timeout(request):
            raise httpx.ReadTimeout('private-provider-detail', request=request)
        with self.client(timeout):
            result = run_web_search(RetrievalTrace(), 'latest', 'general', None)
        self.assertIn('timed out', result['error'])

    def test_reordered_provider_numbers_keep_stable_turn_citations(self):
        responses = [
            aliyun_response([web_result('https://example.com/first')], 'First [ref_1]'),
            aliyun_response([
                web_result('https://example.com/second'), web_result('https://example.com/first'),
            ], 'Second [ref_1], first [ref_2]'),
        ]
        trace = RetrievalTrace()
        with self.client(lambda _: httpx.Response(200, json=responses.pop(0))):
            run_web_search(trace, 'first query', 'general', None)
            second = run_web_search(trace, 'second query', 'general', None)
        self.assertEqual(second['summary'], 'Second [W2], first [W1]')
        self.assertEqual(trace.web_snapshot()['web_search_summary'], 'First [W1]\n\nSecond [W2], first [W1]')
        self.assertTrue(all('_provider_ref' not in source for source in trace.web_snapshot()['web_sources']))

    def test_generated_summary_without_sources_is_not_published_as_web_evidence(self):
        with self.client(lambda _: httpx.Response(200, json=aliyun_response([], 'Unverified answer [ref_1]'))):
            trace = RetrievalTrace()
            result = run_web_search(trace, 'latest?', 'general', None)
        self.assertEqual(result['status'], 'no_results')
        self.assertEqual(result['summary'], '')
        self.assertIsNone(trace.web_snapshot()['web_search_summary'])

    def test_disabled_search_skips_http_and_provider_sources_are_bounded(self):
        with patch.object(self.settings, 'aliyun_web_search_enabled', False), self.client(lambda _: self.fail('Unexpected network')):
            self.assertEqual(run_web_search(RetrievalTrace(), 'query', 'general', None)['status'], 'unavailable')
        sources = [web_result(f'https://example.com/{number}') for number in range(25)]
        with self.client(lambda _: httpx.Response(200, json=aliyun_response(sources))):
            result = run_web_search(RetrievalTrace(), 'query', 'general', None)
        self.assertEqual(len(result['sources']), 20)

    def test_concurrent_identical_searches_share_one_request(self):
        entered, release = Event(), Event()
        def handler(request):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Search was not released')
            return httpx.Response(200, json=aliyun_response([web_result()]))
        trace = RetrievalTrace()
        with self.client(handler) as client, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(run_web_search, trace, 'latest', 'general', None)
            self.assertTrue(entered.wait(3))
            second = pool.submit(run_web_search, trace, 'latest', 'general', None)
            release.set()
            self.assertEqual(first.result(), second.result())
            self.assertEqual(client.call_count, 1)
        self.assertEqual(len(trace.web_sources), 1)

    def test_input_validation_and_operations_tool_isolation(self):
        for kwargs in ({'query': ''}, {'query': 'x', 'topic': 'invalid'}, {'query': 'x', 'time_range': 'invalid'}):
            with self.assertRaises(ValidationError):
                WebSearchInput.model_validate(kwargs)
        self.assertEqual([tool.name for tool in get_agent_tools(ops=True)], ['search_knowledge_base'])
        self.assertEqual({tool.name for tool in get_agent_tools()}, {'search_knowledge_base', 'search_web'})

    def test_failed_followup_keeps_evidence_and_new_turn_has_independent_cache(self):
        successful = {'status': 'ok', 'error': None, 'sources': [{
            **web_result(), 'retrieved_at': '2026-11-07T00:00:00Z',
        }]}
        failed = {'status': 'error', 'error': 'Search unavailable', 'sources': []}
        first, second = RetrievalTrace(), RetrievalTrace()
        with patch('app.agent.web_search._fetch', side_effect=[successful, failed, successful]) as fetch:
            run_web_search(first, 'first', 'general', None)
            run_web_search(first, 'followup', 'general', None)
            run_web_search(second, 'first', 'general', None)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(first.web_search_status, 'error')
        self.assertEqual(len(first.web_sources), 1)
        self.assertEqual(second.web_search_status, 'ok')
        self.assertEqual(second.web_sources[0]['ref_id'], 'W1')

    def test_capabilities_never_calls_aliyun(self):
        from app.api.v1.chat import router
        api = FastAPI()
        api.include_router(router)
        with TestClient(api) as client, self.client(lambda _: self.fail('Unexpected network')):
            payload = client.get('/chat/capabilities').json()
        self.assertEqual(payload, {'web_search': {'configured': True, 'provider': 'aliyun'}})


class AgentChatTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.factory_patch = patch('app.services.chat_service.get_session_factory', return_value=self.factory)
        self.factory_patch.start()
        self.addCleanup(self.factory_patch.stop)

    def agent(self, responses):
        return create_agent(
            ScriptedModel(responses=responses), tools=get_agent_tools(),
            checkpointer=InMemorySaver(), middleware=[validate_citations],
        )

    def test_greeting_writing_followup_and_thread_isolation_without_retrieval(self):
        agent = self.agent([AIMessage(content='你好'), AIMessage(content='一段短文'), AIMessage(content='followup')])
        with patch('app.services.chat_service.get_rag_agent', return_value=agent), \
             patch('app.services.chat_service.retrieve_chunks') as prefetch, \
             patch('app.agent.web_search._fetch') as web:
            service = ChatService()
            for question in ('你好', '帮我写一段短文'):
                response = service.invoke(session_id='greeting', message=question, knowledge_base='default')
                self.assertEqual(response.route, 'agent_chat')
                self.assertEqual(response.source_chunks, [])
            service.invoke(session_id='other', message='你好', knowledge_base='default')
            prefetch.assert_not_called()
            web.assert_not_called()
        history = service.get_session_history('greeting')
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].web_search_status, 'not_requested')
        with self.factory() as db:
            threads = set(db.scalars(select(GraphChatMeta.thread_id)))
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(':auto:v1:kb:' in thread for thread in threads))
        current = service._build_agent_input('today')['messages'][0]['content']
        self.assertIn(datetime.now(ZoneInfo('Asia/Hong_Kong')).date().isoformat(), current)

    def test_parallel_mixed_tools_persist_identical_sync_stream_and_history_sources(self):
        calls = [
            {'name': 'search_knowledge_base', 'args': {'query': 'RAG'}, 'id': 'kb'},
            {'name': 'search_web', 'args': {'query': 'RAG latest'}, 'id': 'web'},
        ]
        responses = [AIMessage(content='', tool_calls=calls), AIMessage(content='Document [1], web [W1].')]
        agent = self.agent(responses)
        hit = {'chunk_id': 'c1', 'document_id': 'd1', 'filename': 'rag.md', 'content': 'RAG retrieves evidence.', 'score': 1}
        fetched = {'status': 'ok', 'error': None, 'sources': [{
            **web_result(), 'retrieved_at': '2026-11-07T00:00:00Z',
        }]}
        with patch('app.services.chat_service.get_rag_agent', return_value=agent), \
             patch('app.agent.tools.get_session_factory', return_value=self.factory), \
             patch('app.agent.tools.retrieve_chunks', return_value=[hit]) as retrieve, \
             patch('app.agent.web_search._fetch', return_value=fetched):
            service = ChatService()
            sync = service.invoke(session_id='sync', message='Compare RAG with current docs', knowledge_base='private')
            agent = self.agent(responses)
            with patch('app.services.chat_service.get_rag_agent', return_value=agent):
                streamed = events(''.join(service.stream(session_id='stream', message='Compare RAG', knowledge_base='private')))
            self.assertTrue(all(call.kwargs['knowledge_base'] == 'private' for call in retrieve.call_args_list))
        self.assertEqual(sync.route, 'agent_mixed')
        self.assertEqual(sync.source_chunks[0].ref_id, 1)
        self.assertEqual(sync.web_sources[0].ref_id, 'W1')
        final = next(data for name, data in streamed if name == 'done')
        self.assertEqual(final['route'], sync.route)
        self.assertEqual(final['web_sources'], sync.model_dump(mode='json')['web_sources'])
        self.assertEqual({data['tool_name'] for name, data in streamed if name == 'tool_result'}, {'search_web', 'search_knowledge_base'})
        history = service.get_session_history('stream')[0]
        self.assertEqual(history.web_sources, sync.web_sources)
        self.assertTrue(any(name == 'web_sources' and data['web_sources'] for name, data in streamed))

    def test_web_failure_event_history_and_memory_clear(self):
        calls = [{'name': 'search_web', 'args': {'query': 'latest'}, 'id': 'web'}]
        agent = self.agent([AIMessage(content='', tool_calls=calls), AIMessage(content='Cannot verify current information.')])
        with patch('app.services.chat_service.get_rag_agent', return_value=agent), \
             patch.object(get_settings(), 'dashscope_api_key', ''):
            service = ChatService()
            streamed = events(''.join(service.stream(session_id='no-key', message='latest?')))
            history = service.get_session_history('no-key')[0]
            self.assertEqual(history.web_search_status, 'unavailable')
            self.assertEqual(history.route, 'agent_web')
            self.assertTrue(any(name == 'tool_error' for name, _ in streamed))
            with patch('app.services.chat_service.clear_thread_memory', return_value=True) as clear:
                result = service.clear_session('no-key')
                self.assertTrue(result.cleared_memory)
                self.assertIn('no-key:auto:v1', [call.args[0] for call in clear.call_args_list])
                self.assertEqual(service.get_session_history('no-key'), [])

    def test_legacy_history_without_metadata_has_empty_web_fields(self):
        with self.factory.begin() as db:
            db.add(QueryLog(session_id='legacy', user_question='old', answer='old', route='agent_rag', source_chunks=[]))
        item = ChatService().get_session_history('legacy')[0]
        self.assertEqual(item.web_sources, [])
        self.assertEqual(item.web_search_status, 'not_requested')

    def test_stale_citation_is_retrieved_again_and_final_stream_answer_is_corrected(self):
        agent = self.agent([
            AIMessage(content='Unsupported previous source [1]'),
            AIMessage(content='', tool_calls=[{'name': 'search_knowledge_base', 'args': {'query': 'RAG'}, 'id': 'kb'}]),
            AIMessage(content='Verified current source [1]'),
        ])
        hit = {'chunk_id': 'c', 'filename': 'rag.md', 'content': 'RAG evidence', 'score': 1}
        with patch('app.services.chat_service.get_rag_agent', return_value=agent), \
             patch('app.agent.tools.get_session_factory', return_value=self.factory), \
             patch('app.agent.tools.retrieve_chunks', return_value=[hit]):
            streamed = events(''.join(ChatService().stream(session_id='citation', message='Explain the same file')))
        done = next(data for name, data in streamed if name == 'done')
        self.assertEqual(done['answer'], 'Verified current source [1]')
        self.assertEqual(done['route'], 'agent_rag')
        self.assertEqual(done['source_chunks'][0]['filename'], 'rag.md')
        self.assertEqual(ChatService().get_session_history('citation')[0].answer, done['answer'])

    def test_repeated_unsupported_citation_stops_without_publishing_a_fake_reference(self):
        agent = self.agent([AIMessage(content='Unsupported [W1]'), AIMessage(content='Still unsupported [W1]')])
        with patch('app.services.chat_service.get_rag_agent', return_value=agent):
            response = ChatService().invoke(session_id='bad-citation', message='latest?', reply_language='en')
        self.assertNotIn('[W1]', response.answer)
        self.assertIn('Could not verify', response.answer)
        self.assertEqual(response.web_sources, [])

    def test_http_streaming_endpoint_retains_trace_across_threadpool_segments(self):
        from app.api.v1.chat import router
        api = FastAPI()
        api.include_router(router)
        calls = [{'name': 'search_web', 'args': {'query': 'latest'}, 'id': 'web'}]
        agent = self.agent([AIMessage(content='', tool_calls=calls), AIMessage(content='Cannot verify latest information.')])
        with patch('app.services.chat_service.get_rag_agent', return_value=agent), \
             patch.object(get_settings(), 'dashscope_api_key', ''), TestClient(api) as client:
            response = client.post('/chat/stream', json={'session_id': 'http', 'message': 'latest?'})
            self.assertEqual(response.status_code, 200)
            streamed = events(response.text)
            done = next(data for name, data in streamed if name == 'done')
            self.assertEqual(done['route'], 'agent_web')
            self.assertEqual(done['web_search_status'], 'unavailable')
            self.assertTrue(any(name == 'tool_error' for name, _ in streamed))
            history = client.get('/chat/sessions/http/history').json()[0]
            self.assertEqual(history['web_search_status'], 'unavailable')
            self.assertIn('DASHSCOPE_API_KEY', history['web_search_error'])

    def test_context_is_not_leaked_and_closes_when_consumer_stops(self):
        trace = RetrievalTrace()
        closed = []
        def stream():
            try:
                with bind_retrieval_trace(trace):
                    yield 'first'
                    self.assertIs(get_current_retrieval_trace(), trace)
                    yield 'second'
            finally:
                closed.append(True)
        iterator = preserve_stream_context(stream())
        self.assertEqual(next(iterator), 'first')
        self.assertIsNone(get_current_retrieval_trace())
        self.assertEqual(next(iterator), 'second')
        iterator.close()
        self.assertIsNone(get_current_retrieval_trace())
        self.assertEqual(closed, [True])


if __name__ == '__main__':
    unittest.main()
