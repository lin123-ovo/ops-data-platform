from __future__ import annotations

import unittest

import grpc
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pymilvus import DataType

from app.core import milvus_client
from app.core.vector_errors import VectorConfigurationError, VectorError, VectorTimeoutError


class GetVectorStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            embedding_model='text-embedding-v1', milvus_collection='test_collection', resolved_milvus_uri='http://127.0.0.1:19530',
            milvus_dimension=3, milvus_timeout_seconds=10,
        )
        self.client = Mock()
        self.client.has_collection.return_value = True
        self.client.list_indexes.return_value = ['vector']
        self.client.describe_collection.return_value = {
            'fields': [{'name': 'vector', 'type': DataType.FLOAT_VECTOR, 'params': {'dim': 3}}],
        }
        self.client.insert.return_value = {'ids': [123]}
        self.settings_patch = patch.object(milvus_client, 'get_settings', return_value=self.settings)
        self.native_factory = milvus_client.get_milvus_client
        self.client_patch = patch.object(milvus_client, 'get_milvus_client', return_value=self.client)
        self.embeddings_patch = patch.object(milvus_client, 'get_embeddings')
        self.settings_patch.start()
        self.client_patch.start()
        self.embeddings = self.embeddings_patch.start().return_value
        self.store = milvus_client.get_vector_store()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.client_patch.stop)
        self.addCleanup(self.embeddings_patch.stop)

    def write(self) -> list[str | int]:
        return self.store.add_embeddings(texts=['RAW'], embeddings=[[1, 2, 3]], metadatas=[{'chunk_id': 'c1'}])

    def test_existing_collection_is_untouched_until_write_and_raw_text_is_stored(self) -> None:
        self.client.has_collection.assert_not_called()
        self.assertEqual(self.write(), [123])
        row = self.client.insert.call_args.kwargs['data'][0]
        self.assertEqual(row, {'chunk_id': 'c1', 'text': 'RAW', 'vector': [1, 2, 3]})
        self.client.create_collection.assert_not_called()
        self.embeddings.embed_documents.assert_not_called()
        self.assertEqual(self.client.insert.call_args.kwargs['timeout'], 10.0)
        self.assertEqual(self.client.insert.call_args.kwargs['retry_times'], 0)

    def test_new_collection_uses_dynamic_metadata_and_expected_index(self) -> None:
        self.client.has_collection.return_value = False
        self.client.list_indexes.return_value = []
        self.write()
        self.client.create_schema.assert_called_once_with(auto_id=True, enable_dynamic_field=True)
        self.client.prepare_index_params.return_value.add_index.assert_called_once_with(
            field_name='vector', index_type='AUTOINDEX', metric_type='COSINE',
        )
        self.client.create_schema.return_value.add_field.assert_any_call('vector', DataType.FLOAT_VECTOR, dim=3)
        self.assertEqual(self.client.create_collection.call_args.kwargs['timeout'], 10.0)

    def test_retry_after_failed_index_creation_recovers_missing_index_without_recreating_collection(self) -> None:
        self.client.list_indexes.return_value = []
        self.write()
        self.client.create_collection.assert_not_called()
        self.client.create_index.assert_called_once()
        self.assertFalse(self.client.create_index.call_args.kwargs['sync'])

    def test_dimension_mismatch_never_alters_existing_collection(self) -> None:
        self.client.describe_collection.return_value['fields'][0]['params']['dim'] = 2
        with self.assertRaises(VectorConfigurationError):
            self.write()
        self.client.insert.assert_not_called()
        self.client.create_collection.assert_not_called()

    def test_timeout_does_not_retry_or_dump_input(self) -> None:
        self.client.insert.side_effect = TimeoutError('SECRET RAW TEXT')
        with self.assertLogs('app.core.vector_errors', level='INFO') as logs:
            with self.assertRaises(VectorTimeoutError) as error:
                self.write()
        self.assertIn('结果未知', str(error.exception))
        self.client.insert.assert_called_once()
        self.assertNotIn('SECRET RAW TEXT', '\n'.join(logs.output))

    def test_short_id_response_cleans_acknowledged_ids(self) -> None:
        self.client.insert.return_value = {'ids': [123]}
        with self.assertRaises(VectorError):
            self.store.add_embeddings(texts=['a', 'b'], embeddings=[[1, 2, 3]] * 2, metadatas=[{}, {}])
        self.client.delete.assert_called_once()
        self.assertEqual(self.client.delete.call_args.kwargs['filter'], 'pk in [123]')

    def test_partial_batch_failure_cleans_previous_success(self) -> None:
        self.client.insert.side_effect = [{'ids': list(range(1000))}, TimeoutError()]
        with self.assertRaises(VectorTimeoutError):
            self.store.add_embeddings(texts=['a'] * 1001, embeddings=[[1, 2, 3]] * 1001, metadatas=[{}] * 1001)
        self.assertEqual(self.client.insert.call_count, 2)
        import json
        self.assertEqual(len(json.loads(self.client.delete.call_args.kwargs['filter'][6:])), 1000)

    def test_search_returns_original_text_and_cosine_score(self) -> None:
        self.embeddings.embed_query.return_value = [1, 2, 3]
        self.client.search.return_value = [[{
            'id': 123, 'distance': .9, 'entity': {'text': 'RAW', 'vector': [1, 2, 3], 'chunk_id': 'c1'},
        }]]
        hits = self.store.similarity_search_with_score('query', k=5)
        self.assertEqual(hits[0][0].page_content, 'RAW')
        self.assertEqual(hits[0][0].metadata['chunk_id'], 'c1')
        self.assertNotIn('vector', hits[0][0].metadata)
        self.assertEqual(hits[0][1], .9)
        self.assertEqual(self.client.search.call_args.kwargs['timeout'], 10.0)
        self.assertEqual(self.client.search.call_args.kwargs['search_params']['metric_type'], 'COSINE')

    def test_empty_collection_returns_no_hits_without_embedding(self) -> None:
        self.client.has_collection.return_value = False
        self.assertEqual(self.store.similarity_search_with_score('query', k=5), [])
        self.embeddings.embed_query.assert_not_called()

    def test_insert_retry_controls_reach_rpc_handler(self) -> None:
        # High-level PyMilvus 2.5 insert drops **kwargs; exercise our real bridge.
        client = object.__new__(milvus_client.BoundedMilvusClient)
        connection = Mock()
        connection.insert_rows.return_value = SimpleNamespace(primary_keys=[123], insert_count=1)
        with patch.object(client, '_get_connection', return_value=connection):
            result = client.insert('test_collection', [{'text': 'RAW'}], timeout=10.0,
                                   retry_times=0, retry_on_rate_limit=False)
        self.assertEqual(result['ids'], [123])
        self.assertEqual(connection.insert_rows.call_args.kwargs['timeout'], 10.0)
        self.assertEqual(connection.insert_rows.call_args.kwargs['retry_times'], 0)
        self.assertFalse(connection.insert_rows.call_args.kwargs['retry_on_rate_limit'])

    def test_real_sdk_insert_preserves_schema_payload_and_deadline_without_network(self) -> None:
        from pymilvus import CollectionSchema, FieldSchema
        from pymilvus.client.grpc_handler import GrpcHandler
        from pymilvus.grpc_gen import milvus_pb2, schema_pb2

        schema = CollectionSchema([
            FieldSchema('pk', DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema('text', DataType.VARCHAR, max_length=65535),
            FieldSchema('vector', DataType.FLOAT_VECTOR, dim=3),
        ], enable_dynamic_field=True)
        handler = object.__new__(GrpcHandler)
        handler.schema_cache = {}
        handler._stub = Mock()
        handler._stub.Insert.return_value = milvus_pb2.MutationResult(
            IDs=schema_pb2.IDs(int_id=schema_pb2.LongArray(data=[123])), insert_cnt=1, timestamp=1,
        )
        client = object.__new__(milvus_client.BoundedMilvusClient)
        with patch.object(client, '_get_connection', return_value=handler), \
             patch.object(handler, 'describe_collection', return_value=schema.to_dict()):
            result = client.insert('offline', [{'text': 'raw', 'vector': [1, 2, 3], 'chunk_id': 'c1'}],
                                   timeout=10.0, retry_times=0, retry_on_rate_limit=False)
        self.assertEqual(result, {'ids': [123], 'insert_count': 1})
        self.assertEqual(handler._stub.Insert.call_args.kwargs['timeout'], 10.0)
        request = handler._stub.Insert.call_args.kwargs['request']
        self.assertEqual(request.num_rows, 1)
        text_field = next(field for field in request.fields_data if field.field_name == 'text')
        self.assertEqual(list(text_field.scalars.string_data.data), ['raw'])

    def test_real_sdk_does_not_replay_ambiguous_insert(self) -> None:
        from pymilvus.client.grpc_handler import GrpcHandler
        from pymilvus.exceptions import MilvusException

        class RpcFailure(grpc.RpcError):
            def __init__(self, status: grpc.StatusCode) -> None:
                self.status = status

            def code(self) -> grpc.StatusCode:
                return self.status

            def details(self) -> str:
                return 'transport failure'

        for status in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
            handler = object.__new__(GrpcHandler)
            handler._stub = Mock()
            handler._stub.Insert.side_effect = RpcFailure(status)
            with patch.object(handler, '_prepare_row_insert_request', return_value=Mock()), \
                 patch('pymilvus.decorators.time.sleep') as sleep:
                with self.assertRaises((grpc.RpcError, MilvusException)):
                    handler.insert_rows('offline', [{}], timeout=10.0,
                                        retry_times=0, retry_on_rate_limit=False)
            handler._stub.Insert.assert_called_once()
            sleep.assert_not_called()

    def test_malformed_id_and_compensation_failure_preserve_original_error(self) -> None:
        self.client.insert.return_value = {'ids': [123, {'bad': 'id'}]}
        self.client.delete.side_effect = TimeoutError()
        with self.assertRaisesRegex(VectorError, 'ID'):
            self.store.add_embeddings(texts=['a', 'b'], embeddings=[[1, 2, 3]] * 2, metadatas=[{}, {}])
        self.assertEqual(self.client.delete.call_args.kwargs['filter'], 'pk in [123]')

    def test_connect_timeout_is_passed_to_native_client(self) -> None:
        with patch.object(milvus_client, 'BoundedMilvusClient', return_value=self.client) as factory:
            self.native_factory.__wrapped__()
        factory.assert_called_once_with(uri='http://127.0.0.1:19530', timeout=10)



class NormalizeVectorIdsTests(unittest.TestCase):
    def test_converts_decimal_string_ids_to_integers(self) -> None:
        self.assertEqual(milvus_client.normalize_vector_ids(['123', '456']), [123, 456])

    def test_preserves_non_decimal_ids(self) -> None:
        self.assertEqual(milvus_client.normalize_vector_ids(['uuid-value']), ['uuid-value'])
