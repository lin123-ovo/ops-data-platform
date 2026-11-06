from __future__ import annotations

import itertools
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from requests import Response
from requests.exceptions import ConnectionError as RequestConnectionError
from requests.exceptions import Timeout
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core import embeddings, vector_indexing
from app.core.embeddings import ValidatedDashScopeEmbeddings, validate_vectors
from app.core.vector_errors import VectorConfigurationError, VectorError, VectorTimeoutError, VectorValidationError
from app.models.base import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag import retriever
from app.services.chunk_service import ChunkService
from app.services.document_service import DocumentService


_SDK_CALL = embeddings.TextEmbedding.call


def settings(**overrides: object) -> SimpleNamespace:
    return SimpleNamespace(**{
        'embedding_model': 'text-embedding-v1', 'milvus_dimension': 3, 'dashscope_api_key': 'test-secret',
        'embedding_timeout_seconds': 30, 'embedding_query_timeout_seconds': 10, 'embedding_max_attempts': 2,
        **overrides,
    })


def response(vectors: list[list[float]] | None = None, *, status: int = 200) -> SimpleNamespace:
    return SimpleNamespace(
        request_id='provider-request-1', status_code=status, message='provider-secret',
        output={'embeddings': [
            {'text_index': index, 'embedding': vector} for index, vector in enumerate(vectors or [[1, 2, 3]])
        ]},
    )


class ValidationTests(unittest.TestCase):
    def test_valid_vector_is_not_normalized(self) -> None:
        self.assertEqual(validate_vectors([[3, 4, 0]], expected_count=1, dimension=3), [[3, 4, 0]])

    def test_rejects_invalid_count_shape_type_values_and_zero(self) -> None:
        samples = [
            None, [], [[1, 2, 3], [1, 2, 3]], [[1, 2]], [[1, 2, 3, 4]], [None],
            [['1', 2, 3]], [[True, 2, 3]], [[None, 2, 3]],
            [[float('nan'), 1, 2]], [[float('inf'), 1, 2]], [[-float('inf'), 1, 2]],
            [[0, 0, -0.0]], [[1e100, 2, 3]],
        ]
        for sample in samples:
            with self.subTest(sample=sample), self.assertRaises(VectorValidationError):
                validate_vectors(sample, expected_count=1, dimension=3)


class EmbeddingAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ValidatedDashScopeEmbeddings(settings())
        self.call_patch = patch.object(embeddings.TextEmbedding, 'call', return_value=response())
        self.call = self.call_patch.start()
        self.sleep_patch = patch.object(embeddings.time, 'sleep')
        self.sleep = self.sleep_patch.start()
        self.addCleanup(self.call_patch.stop)
        self.addCleanup(self.sleep_patch.stop)

    def test_document_and_query_roles_timeouts_and_explicit_credentials(self) -> None:
        self.assertEqual(self.adapter.embed_documents(['a']), [[1, 2, 3]])
        self.assertEqual(self.call.call_args.kwargs['text_type'], 'document')
        self.assertEqual(self.call.call_args.kwargs['request_timeout'], 30)
        self.assertEqual(self.call.call_args.kwargs['api_key'], 'test-secret')
        self.assertEqual(self.adapter.embed_query('q'), [1, 2, 3])
        self.assertEqual(self.call.call_args.kwargs['text_type'], 'query')
        self.assertEqual(self.call.call_args.kwargs['request_timeout'], 10)

    def test_timeout_reaches_real_sdk_http_transport_without_network(self) -> None:
        http_response = Response()
        http_response.status_code = 200
        http_response.headers['Content-Type'] = 'application/json'
        http_response._content = json.dumps({
            'request_id': 'r1', 'output': {'embeddings': [{'text_index': 0, 'embedding': [1, 2, 3]}]},
            'usage': {'total_tokens': 1},
        }).encode()
        with patch('dashscope.api_entities.http_request._get_shared_sync_session') as session, \
             patch.object(embeddings.TextEmbedding, 'call', _SDK_CALL):
            session.return_value.post.return_value = http_response
            self.assertEqual(self.adapter.embed_documents(['a']), [[1, 2, 3]])
            self.assertEqual(session.return_value.post.call_args.kwargs['timeout'], 30)
            session.return_value.post.assert_called_once()

    def test_provider_indices_restore_input_order(self) -> None:
        self.call.return_value = response([[1, 2, 3], [4, 5, 6]])
        self.call.return_value.output['embeddings'].reverse()
        self.assertEqual(self.adapter.embed_documents(['a', 'b']), [[1, 2, 3], [4, 5, 6]])

    def test_missing_duplicate_out_of_range_and_invalid_indices_fail_without_retry(self) -> None:
        for indices in ([0, 0], [0, 2], [None, 1], [False, 1], ['0', 1]):
            result = response([[1, 2, 3], [4, 5, 6]])
            for entry, index in zip(result.output['embeddings'], indices):
                entry['text_index'] = index
            self.call.reset_mock()
            self.call.return_value = result
            with self.subTest(indices=indices), self.assertRaises(VectorValidationError):
                self.adapter.embed_documents(['a', 'b'])
            self.call.assert_called_once()

    def test_invalid_query_is_validated_without_retry(self) -> None:
        self.call.return_value = response([[0, 0, 0]])
        with self.assertRaises(VectorValidationError):
            self.adapter.embed_query('q')
        self.call.assert_called_once()

    def test_document_retries_transient_failures_once(self) -> None:
        failures = [Timeout('secret'), RequestConnectionError('secret'), response(status=429), response(status=503)]
        for failure in failures:
            self.call.reset_mock()
            self.sleep.reset_mock()
            self.call.side_effect = [failure, response()]
            self.assertEqual(self.adapter.embed_documents(['a']), [[1, 2, 3]])
            self.assertEqual(self.call.call_count, 2)
            self.sleep.assert_called_once_with(1)

    def test_query_timeout_does_not_retry(self) -> None:
        self.call.side_effect = Timeout('secret')
        with self.assertRaises(VectorTimeoutError):
            self.adapter.embed_query('q')
        self.call.assert_called_once()
        self.sleep.assert_not_called()

    def test_document_timeout_stops_after_configured_attempts(self) -> None:
        self.call.side_effect = Timeout()
        with self.assertRaises(VectorTimeoutError):
            self.adapter.embed_documents(['a'])
        self.assertEqual(self.call.call_count, 2)

    def test_auth_and_bad_input_are_not_retried(self) -> None:
        for code in (400, 401, 403):
            self.call.reset_mock()
            self.call.return_value = response(status=code)
            with self.assertRaises(VectorError):
                self.adapter.embed_documents(['a'])
            self.call.assert_called_once()

    def test_oversized_input_returns_safe_actionable_error(self) -> None:
        self.call.return_value = response(status=400)
        self.call.return_value.message = 'SECRET exceeds maximum token length'
        with self.assertRaisesRegex(VectorError, '长度限制') as error:
            self.adapter.embed_documents(['a'])
        self.assertNotIn('SECRET', str(error.exception))

    def test_batch_retry_does_not_repeat_previously_completed_batches(self) -> None:
        self.call.side_effect = [response([[1, 2, 3]] * 25), Timeout(), response()]
        self.assertEqual(len(self.adapter.embed_documents(['a'] * 26)), 26)
        self.assertEqual([len(call.kwargs['input']) for call in self.call.call_args_list], [25, 1, 1])

    def test_empty_document_list_does_not_call_service(self) -> None:
        self.assertEqual(self.adapter.embed_documents([]), [])
        self.call.assert_not_called()

    def test_logs_contain_identifiers_timing_counts_but_not_input_or_key(self) -> None:
        with self.assertLogs('app.core.vector_errors', level='INFO') as logs:
            self.adapter.embed_documents(['SECRET_INPUT'])
        output = '\n'.join(logs.output)
        for field in ('operation_id', 'duration_ms', 'batch', 'attempt', 'count', 'provider-request-1', 'document'):
            self.assertIn(field, output)
        for secret in ('SECRET_INPUT', 'test-secret', 'provider-secret'):
            self.assertNotIn(secret, output)


class ContextTests(unittest.TestCase):
    def test_context_and_original_body_are_separate(self) -> None:
        body = '    默认有效期为300秒。\n\t保留格式  '
        text = vector_indexing.build_embedding_text(body, {'filename': '说明.md', 'section_title': 'Redis 缓存'})
        self.assertEqual(text, '文档：说明\n章节：Redis 缓存\n\n' + body)

    def test_no_title_placeholders_duplicate_and_long_title(self) -> None:
        self.assertEqual(vector_indexing.build_embedding_text('body', {}), 'body')
        for title in ('', 'Introduction', 'Untitled', '说明'):
            self.assertEqual(vector_indexing.build_embedding_text('body', {'filename': '说明.md', 'section_title': title}),
                             '文档：说明\n\nbody')
        value = vector_indexing.build_embedding_text('完整正文', {'filename': 'a.md', 'section_title': '长' * 1000})
        self.assertTrue(value.endswith('完整正文'))
        self.assertLessEqual(len(value) - len('完整正文'), 256)


class TransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.store = Mock()
        self.next_id = itertools.count(100)
        self.store.add_embeddings.side_effect = lambda **kw: [next(self.next_id) for _ in kw['texts']]
        self.embedding = Mock()
        self.embedding.embed_documents.side_effect = lambda texts: [[1, 2, 3] for _ in texts]
        for target, value in [
            ('app.core.vector_indexing.get_settings', settings()),
            ('app.core.vector_indexing.get_embeddings', self.embedding),
            ('app.rag.ingest.get_vector_store', self.store),
            ('app.services.chunk_service.get_vector_store', self.store),
        ]:
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        dirty = patch('app.services.document_service.get_bm25_index')
        dirty.start()
        self.addCleanup(dirty.stop)
        dirty = patch('app.services.chunk_service.get_bm25_index')
        dirty.start()
        self.addCleanup(dirty.stop)
        self.service = DocumentService(self.db)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def create(self) -> Document:
        return self.service.ingest_text(filename='说明.md', content='# Redis\n\n    默认有效期300秒。')

    def test_ingest_encodes_context_but_persists_raw_body_and_offsets(self) -> None:
        document = self.create()
        chunk = self.db.scalars(select(Chunk)).one()
        encoded = self.embedding.embed_documents.call_args.args[0][0]
        self.assertIn('文档：说明\n章节：Redis', encoded)
        self.assertEqual(self.store.add_embeddings.call_args.kwargs['texts'], [chunk.content])
        self.assertNotIn('文档：', chunk.content)
        self.assertEqual(chunk.metadata_json['embedding_input_version'], 'title_context_v1')
        self.assertEqual(chunk.metadata_json['embedding_dimension'], 3)
        self.assertEqual(document.chunk_count, 1)
        self.assertEqual(chunk.start_offset, 0)
        self.assertEqual(chunk.end_offset, len(chunk.content))

    def test_encoding_failure_creates_no_database_rows_and_does_not_insert(self) -> None:
        self.embedding.embed_documents.side_effect = VectorValidationError()
        with self.assertRaises(VectorValidationError):
            self.create()
        self.assertEqual(self.db.scalars(select(Document)).all(), [])
        self.assertEqual(self.db.scalars(select(Chunk)).all(), [])
        self.store.add_embeddings.assert_not_called()

    def test_commit_failure_cleans_known_new_ids(self) -> None:
        with patch.object(self.db, 'commit', side_effect=RuntimeError('db commit failed')):
            with self.assertRaisesRegex(RuntimeError, 'db commit failed'):
                self.create()
        self.store.delete.assert_called_once_with(ids=[100])
        self.assertEqual(self.db.scalars(select(Document)).all(), [])

    def test_compensation_failure_is_logged_without_masking_database_error(self) -> None:
        self.store.delete.side_effect = VectorError('delete failed')
        with patch.object(self.db, 'commit', side_effect=RuntimeError('commit failed')):
            with self.assertLogs('app.core.vector_errors', level='WARNING') as logs:
                with self.assertRaisesRegex(RuntimeError, 'commit failed'):
                    self.create()
        self.assertIn('milvus_rollback_cleanup', '\n'.join(logs.output))

    def test_id_count_mismatch_rolls_back_and_cleans_known_ids(self) -> None:
        self.store.add_embeddings.side_effect = None
        self.store.add_embeddings.return_value = [100, 101]
        with self.assertRaises(VectorError):
            self.create()
        self.store.delete.assert_called_once_with(ids=[100, 101])
        self.assertEqual(self.db.scalars(select(Document)).all(), [])

    def test_rebuild_encoding_failure_keeps_old_chunks_and_vectors(self) -> None:
        document = self.create()
        old_id = self.db.scalars(select(Chunk.id)).one()
        self.embedding.embed_documents.side_effect = VectorTimeoutError()
        self.store.reset_mock()
        with self.assertRaises(VectorTimeoutError):
            self.service.rebuild_index(document.id)
        self.assertEqual(self.db.scalars(select(Chunk.id)).one(), old_id)
        self.store.delete.assert_not_called()
        self.store.add_embeddings.assert_not_called()

    def test_edit_encoding_failure_keeps_content_metadata_state_and_old_vector(self) -> None:
        self.create()
        chunk = self.db.scalars(select(Chunk)).one()
        old = (chunk.content, dict(chunk.metadata_json), chunk.enabled, chunk.vector_id)
        self.embedding.embed_documents.side_effect = VectorTimeoutError()
        self.store.reset_mock()
        with self.assertRaises(VectorTimeoutError):
            ChunkService(self.db).update_chunk(chunk.id, content='新正文', enabled=False, metadata_json={'section_title': '新标题'})
        self.db.refresh(chunk)
        self.assertEqual((chunk.content, chunk.metadata_json, chunk.enabled, chunk.vector_id), old)
        self.store.delete.assert_not_called()

    def test_title_only_edit_reembeds_but_unrelated_metadata_or_enabled_edit_does_not(self) -> None:
        self.create()
        chunk = self.db.scalars(select(Chunk)).one()
        old_text = chunk.content
        self.store.reset_mock()
        ChunkService(self.db).update_chunk(chunk.id, metadata_json={'section_title': '缓存过期'})
        self.assertIn('章节：缓存过期', self.embedding.embed_documents.call_args.args[0][0])
        self.assertEqual(chunk.content, old_text)
        self.store.delete.assert_called_once_with(ids=[100])
        self.store.reset_mock()
        self.embedding.reset_mock()
        ChunkService(self.db).update_chunk(chunk.id, enabled=False, metadata_json={'note': 'x'})
        self.embedding.embed_documents.assert_not_called()
        self.store.add_embeddings.assert_not_called()
        self.assertFalse(chunk.enabled)

    def test_successful_rebuild_cleans_old_vector_only_after_commit(self) -> None:
        document = self.create()
        self.store.reset_mock()
        def check_old_still_exists(**kwargs: object) -> list[int]:
            self.store.delete.assert_not_called()
            return [101]
        self.store.add_embeddings.side_effect = check_old_still_exists
        self.service.rebuild_index(document.id)
        self.store.delete.assert_called_once_with(ids=[100])
        self.assertEqual(self.db.scalars(select(Chunk.vector_id)).one(), '101')

    def test_no_implicit_reembedding_of_existing_documents(self) -> None:
        self.create()
        self.embedding.reset_mock()
        self.service.list_documents()
        self.embedding.embed_documents.assert_not_called()


class FallbackAndApiTests(unittest.TestCase):
    def test_vector_failure_still_returns_bm25_result(self) -> None:
        hits = [{'chunk_id': 'c1', 'content': '正文', 'score': 2.0, 'bm25_score': 2.0}]
        with patch.object(retriever, 'get_vector_store', side_effect=VectorTimeoutError()), \
             patch.object(retriever, 'get_bm25_index') as bm25:
            bm25.return_value.search.return_value = hits
            db = Mock()
            chunk = Mock(id='c1', content='正文', metadata_json={}, chunk_index=0, page_number=None)
            document = Mock(id='d1', filename='test.txt', knowledge_base='default')
            db.execute.return_value = [(chunk, document)]
            results = retriever.retrieve_chunks(db, query='q', top_k=5)
        self.assertEqual(results[0]['chunk_id'], 'c1')
        self.assertEqual(results[0]['retrieval_source'], 'bm25')

    def test_http_errors_keep_detail_shape(self) -> None:
        # Importing the app does not run lifespan/startup or touch external services.
        from app.main import handle_vector_error
        app = FastAPI()
        app.add_exception_handler(VectorError, handle_vector_error)
        cases = [VectorTimeoutError(), VectorValidationError(), VectorConfigurationError('配置不一致')]
        @app.get('/failure/{index}')
        def fail(index: int) -> None:
            raise cases[index]
        client = TestClient(app)
        for index, error in enumerate(cases):
            result = client.get(f'/failure/{index}')
            self.assertEqual(result.status_code, error.status_code)
            self.assertEqual(result.json(), {'detail': str(error)})

    def test_timeout_and_attempt_settings_reject_invalid_values(self) -> None:
        from app.config import Settings
        from pydantic import ValidationError
        for field in ('EMBEDDING_TIMEOUT_SECONDS', 'EMBEDDING_QUERY_TIMEOUT_SECONDS', 'MILVUS_TIMEOUT_SECONDS',
                      'EMBEDDING_MAX_ATTEMPTS', 'MILVUS_DIMENSION'):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Settings(**{field: 0})
