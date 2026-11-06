from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from docx import Document as DocxDocument
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.chunk import Chunk
from app.models.document import Document
from app.rag.ingest import ingest_loaded_document, ingest_text_document, rebuild_document_chunks
from app.rag.loader import (
    LoadedDocument, LoadedSection, _build_ocr_loaded_document, _load_docx_file, _load_pdf_file,
    _split_markdown_sections, build_loaded_document_from_text,
)
from app.rag.splitters import SPLITTER_REGISTRY
from app.services.chunk_service import ChunkService
from app.services.ocr_service import OCRDetectionDecision, OCRService
from app.utils.markdown import has_unclosed_fence
from app.utils.quality import (
    QualityReport, decode_quality_report, encode_quality_report, inspect_text_quality,
)
from app.utils.text import clean_text


class CleaningTests(unittest.TestCase):
    def test_preserves_meaningful_whitespace_and_is_idempotent(self) -> None:
        text = '\r\n \t\r\n    if enabled:\r\n\tvalue = "a  b"  \r\n\r\n\r\n    done()\r\n \t'
        expected = '    if enabled:\n\tvalue = "a  b"  \n\n\n    done()'
        self.assertEqual(clean_text(text), expected)
        self.assertEqual(clean_text(expected), expected)

    def test_blank_input(self) -> None:
        for text in ('', ' \t\r\n\r\n ', '\n\n'):
            self.assertEqual(clean_text(text), '')

    def test_does_not_strip_first_line_or_markdown_hard_break(self) -> None:
        self.assertEqual(clean_text('    first  \nsecond  '), '    first  \nsecond  ')


class MarkdownTests(unittest.TestCase):
    def test_headings_inside_both_fence_types_are_protected(self) -> None:
        text = '# 正文\n```python\n# 注释\n    run()\n\n\n```\n~~~txt\n# 不是标题\n~~~\n## 下一节\n正文'
        sections = _split_markdown_sections(text)
        self.assertEqual([item.metadata['section_title'] for item in sections], ['正文', '下一节'])
        self.assertIn('# 注释\n    run()\n\n\n```', sections[0].text)
        self.assertIn('# 不是标题', sections[0].text)
        self.assertFalse(has_unclosed_fence(text))

    def test_indented_code_is_not_heading(self) -> None:
        sections = _split_markdown_sections('    # code\n    run()\n# 正文\n内容')
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].text, '    # code\n    run()')

    def test_unclosed_and_mismatched_fences(self) -> None:
        for text in ('```py\n# code\n~~~\n# still code', '~~~~\n# code\n~~~'):
            self.assertTrue(has_unclosed_fence(text))
            self.assertEqual(len(_split_markdown_sections(text)), 1)
            self.assertTrue(inspect_text_quality(text, markdown=True))

    def test_longer_closing_fence_and_invalid_backtick_info(self) -> None:
        self.assertFalse(has_unclosed_fence('```py\n# x\n````'))
        self.assertFalse(has_unclosed_fence('```invalid`info\n# heading'))


class SplitterTests(unittest.TestCase):
    def test_all_splitters_preserve_slices_and_cover_content(self) -> None:
        samples = (
            '    if enabled:\n\tvalue = "a  b"  \n\n\n    finish()',
            '参数:  value\n    continuation\n\n型号    电压\nA       220 V',
            '# 内容\n```python\n# comment\n\n\n    run()\n```\n\n尾部',
            '<table>\n<tr><td> A  B </td></tr>\n\n<tr><td> C </td></tr>\n</table>\n\n尾部',
        )
        for name, splitter in SPLITTER_REGISTRY.items():
            for text in samples:
                with self.subTest(splitter=name, text=text[:20]):
                    normalized = clean_text(text)
                    chunks = splitter(text, chunk_size=32, chunk_overlap=8)
                    covered: set[int] = set()
                    for index, chunk in enumerate(chunks):
                        self.assertEqual(chunk.chunk_index, index)
                        self.assertEqual(chunk.content, normalized[chunk.start_offset:chunk.end_offset])
                        covered.update(range(chunk.start_offset, chunk.end_offset))
                    self.assertTrue(all(pos in covered for pos, char in enumerate(normalized) if not char.isspace()))

    def test_short_lines_and_overlap_make_progress(self) -> None:
        for splitter in SPLITTER_REGISTRY.values():
            chunks = splitter('x\n' * 100 + 'end', chunk_size=20, chunk_overlap=15)
            self.assertTrue(chunks)
            self.assertLessEqual(len(chunks), 203)
            self.assertEqual(chunks[-1].end_offset, len(clean_text('x\n' * 100 + 'end')))

    def test_internal_whitespace_only_windows_are_not_lost(self) -> None:
        text = 'prefix\n' + '\n' * 100 + '    ending'
        for splitter in SPLITTER_REGISTRY.values():
            chunks = splitter(text, chunk_size=16, chunk_overlap=4)
            covered = {index for chunk in chunks for index in range(chunk.start_offset, chunk.end_offset)}
            self.assertEqual(covered, set(range(len(text))))
            self.assertTrue(all(chunk.content.strip() for chunk in chunks))

    def test_invalid_sizes(self) -> None:
        for splitter in SPLITTER_REGISTRY.values():
            for size, overlap in ((0, 0), (5, -1), (5, 5)):
                with self.subTest(splitter=splitter.__name__, size=size, overlap=overlap):
                    with self.assertRaises(ValueError):
                        splitter('some text', chunk_size=size, chunk_overlap=overlap)

    def test_html_block_retains_structure(self) -> None:
        table = '<table>\n<tr><td>    A  B</td></tr>\n\n<tr><td>C</td></tr>\n</table>'
        chunks = SPLITTER_REGISTRY['semi_structured'](table, chunk_size=20, chunk_overlap=5)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, table)


class QualityTests(unittest.TestCase):
    def test_counts_anomalies_without_modifying_text(self) -> None:
        text = '正文��\x00\x01\t\r\n'
        warnings = inspect_text_quality(text)
        self.assertEqual(len(warnings), 2)
        self.assertIn('2 个替代字符', warnings[0])
        self.assertIn('2 个异常控制字符', warnings[1])
        self.assertEqual(text, '正文��\x00\x01\t\r\n')

    def test_normal_code_and_tables_have_no_warning(self) -> None:
        self.assertEqual(inspect_text_quality('```py\n\tvalue = "a  b"\n```', markdown=True), [])

    def test_round_trip_and_replacing_report(self) -> None:
        report = QualityReport(checked=True, warnings=['请核对原文。'])
        summary = encode_quality_report('parser=test', report)
        self.assertEqual(decode_quality_report(summary), report)
        updated = encode_quality_report(summary, QualityReport(checked=True))
        self.assertEqual(updated.count('quality_report_v1='), 1)
        self.assertEqual(decode_quality_report(updated), QualityReport(checked=True))

    def test_legacy_and_malformed_reports_are_unchecked(self) -> None:
        for summary in (None, '', 'parser=old', 'quality_report_v1=broken',
                        'quality_report_v1=[]', 'quality_report_v1={"checked":1,"warnings":[]}',
                        'quality_report_v1={"checked":true,"warnings":[1]}'):
            with self.subTest(summary=summary):
                self.assertFalse(decode_quality_report(summary).checked)

    @patch('app.rag.loader._try_load_with_ocr', return_value=None)
    @patch('app.rag.loader.PdfReader')
    def test_pdf_reports_empty_pages_before_skipping(self, reader: Mock, ocr: Mock) -> None:
        reader.return_value.pages = [SimpleNamespace(extract_text=lambda: '正文'),
                                     SimpleNamespace(extract_text=lambda: ''),
                                     SimpleNamespace(extract_text=lambda: ' \n')]
        loaded = _load_pdf_file(Path('sample.pdf'))
        self.assertEqual(len(loaded.sections), 1)
        self.assertIn('2, 3', loaded.quality_warnings[0])
        self.assertIn('空白页或扫描页', loaded.quality_warnings[0])

    @patch('app.rag.loader._try_load_with_ocr', return_value=None)
    def test_docx_preserves_indentation_tabs_and_blank_paragraphs(self, ocr: Mock) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'code.docx'
            document = DocxDocument()
            document.add_paragraph('    if enabled:')
            document.add_paragraph('')
            document.add_paragraph('\trun()  ')
            document.save(path)
            loaded = _load_docx_file(path)
        self.assertEqual(loaded.sections[0].text, '    if enabled:\n\n\trun()  ')


class IngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine('sqlite://')
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.vector = Mock()
        self.vector.add_embeddings.side_effect = lambda *, texts, embeddings, metadatas: [str(index) for index in range(len(texts))]
        self.embeddings_patch = patch('app.core.vector_indexing.get_embeddings')
        self.embeddings_patch.start().return_value.embed_documents.side_effect = lambda texts: [[1.0] * 1536 for _ in texts]
        self.vector_patch = patch('app.rag.ingest.get_vector_store', return_value=self.vector)
        self.vector_factory = self.vector_patch.start()

    def tearDown(self) -> None:
        self.vector_patch.stop()
        self.embeddings_patch.stop()
        self.db.close()
        self.engine.dispose()

    def test_warning_document_is_indexed_and_report_persisted(self) -> None:
        document = ingest_text_document(self.db, filename='sample.md', content='# 正文\n    value = "a  b�"')
        self.db.commit()
        self.assertEqual(document.status, 'indexed')
        report = decode_quality_report(document.summary)
        self.assertTrue(report.checked)
        self.assertEqual(len(report.warnings), 1)
        chunk = self.db.scalars(select(Chunk)).one()
        self.assertIn('    value = "a  b�"', chunk.content)
        self.vector.add_embeddings.assert_called_once()

    def test_normal_document_has_checked_empty_warnings(self) -> None:
        document = ingest_text_document(self.db, filename='sample.txt', content='正常正文')
        self.assertEqual(decode_quality_report(document.summary), QualityReport(checked=True))

    def test_control_only_and_empty_table_documents_are_rejected(self) -> None:
        for source, content in (('\x00\x01', '\x00\x01'), ('', '<table><tr><td></td></tr></table>')):
            loaded = LoadedDocument('empty.docx', 'docx', 'test', [LoadedSection(content)], quality_text=source)
            with self.assertRaisesRegex(ValueError, '有效文本'):
                ingest_loaded_document(self.db, loaded_document=loaded)
        self.vector_factory.assert_not_called()

    def test_empty_document_never_creates_records_or_calls_vector_service(self) -> None:
        for sections in ([], [LoadedSection(text=' \n\t')]):
            with self.subTest(sections=sections):
                with self.assertRaisesRegex(ValueError, '有效文本'):
                    ingest_loaded_document(self.db, loaded_document=LoadedDocument('empty.pdf', 'pdf', 'test', sections))
        self.vector_factory.assert_not_called()
        self.assertEqual(self.db.scalars(select(Document)).all(), [])

    def test_rebuild_empty_source_preserves_old_chunks_and_vectors(self) -> None:
        document = ingest_text_document(self.db, filename='sample.txt', content='旧正文')
        document.source_path = '/unused/source.txt'
        self.db.commit()
        old_ids = list(self.db.scalars(select(Chunk.id)))
        self.vector.reset_mock()
        with patch('app.rag.ingest.load_document', return_value=LoadedDocument('sample.txt', 'txt', 'test', [])):
            with self.assertRaisesRegex(ValueError, '原有索引未重建'):
                rebuild_document_chunks(self.db, document=document)
        self.vector.delete.assert_not_called()
        self.assertEqual(list(self.db.scalars(select(Chunk.id))), old_ids)

    def test_rebuild_replaces_report_and_preserves_format(self) -> None:
        document = ingest_text_document(self.db, filename='sample.txt', content='旧正文�')
        document.source_path = '/unused/source.txt'
        self.db.commit()
        loaded = build_loaded_document_from_text('sample.txt', '    new  text\n\tcontinued')
        with patch('app.rag.ingest.load_document', return_value=loaded):
            rebuild_document_chunks(self.db, document=document)
            self.vector.delete.assert_not_called()
            self.db.commit()
        self.assertEqual(decode_quality_report(document.summary), QualityReport(checked=True))
        self.assertEqual(self.db.scalars(select(Chunk)).one().content, '    new  text\n\tcontinued')
        self.vector.delete.assert_called_once()

    def test_chunk_edit_preserves_format_without_rechecking_document(self) -> None:
        document = ingest_text_document(self.db, filename='sample.txt', content='正文')
        self.db.commit()
        summary = document.summary
        chunk = self.db.scalars(select(Chunk)).one()
        with patch('app.services.chunk_service.get_vector_store', return_value=self.vector), \
             patch('app.services.chunk_service.get_bm25_index'):
            updated = ChunkService(self.db).update_chunk(chunk.id, content='    changed  value\n\tgo()')
        self.assertIsNotNone(updated)
        self.assertEqual(updated.content, '    changed  value\n\tgo()')
        self.assertEqual(document.summary, summary)

    def test_ocr_result_preserves_format_and_reports_without_guessing_pages(self) -> None:
        text = '# 正文\r\n```python\r\n    value = "a  b�"\r\n\trun()'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ocr.pdf'
            path.write_bytes(b'fixture')
            service = OCRService()
            with patch.object(service, 'can_use_ocr', return_value=True), \
                 patch.object(service, '_create_task', return_value={'result': {'task_id': 'test'}}), \
                 patch.object(service, '_poll_markdown_url', return_value='mock://result'), \
                 patch.object(service, '_download_text', return_value=text):
                markdown = service.parse_to_markdown(path)
            loaded = _build_ocr_loaded_document(
                file_path=path, file_type='pdf', markdown_text=markdown,
                decision=OCRDetectionDecision(should_use_ocr=True, file_type='pdf'),
            )
        document = ingest_loaded_document(self.db, loaded_document=loaded)
        self.assertEqual(self.db.scalars(select(Chunk)).one().content, clean_text(text))
        report = decode_quality_report(document.summary)
        self.assertEqual(len(report.warnings), 2)
        self.assertFalse(any('页未提取到文字' in warning for warning in report.warnings))


if __name__ == '__main__':
    unittest.main()
