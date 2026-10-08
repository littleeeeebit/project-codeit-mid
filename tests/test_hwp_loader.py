import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from rfp_assistant.storage.hwp_loader import load_hwp_hwpx

class LoaderTest(unittest.TestCase):
    def test_preserves_text_and_marks_unknown_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'example.hwpx'
            path.write_bytes(b'fixture')
            docs = [SimpleNamespace(page_content='한글 본문', metadata={'element_type': 'body'}),
                    SimpleNamespace(page_content='| 요구사항 | SFR-001 |', metadata={'element_type': 'table', 'row_count': 1})]
            with patch('langchain_hwp_hwpx.HwpHwpxLoader') as loader:
                loader.return_value.load.return_value = docs
                elements, warnings, reason = load_hwp_hwpx(path)
                self.assertIsNone(reason)
                self.assertEqual([e['raw_text'] for e in elements], [d.page_content for d in docs])
                self.assertFalse(elements[1]['location']['native_order_verified'])
                self.assertEqual(elements[1]['location']['loader_metadata']['element_type'], 'table')
                self.assertNotIn('table', elements[1])
                self.assertEqual(warnings[0]['code'], 'hwp_loader_structure_limited')
                self.assertFalse(loader.call_args.kwargs['include_images'])
    def test_failure_is_not_successful_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'broken.hwp'
            path.write_bytes(b'broken')
            elements, warnings, reason = load_hwp_hwpx(path)
            self.assertEqual(elements, [])
            self.assertEqual(reason, 'hwp_loader_failed')
    def test_rejects_pdf(self):
        with self.assertRaises(ValueError):
            load_hwp_hwpx(Path('example.pdf'))

    def mocked_result(self, result):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'example.hwp'
            path.write_bytes(b'fixture')
            with patch('langchain_hwp_hwpx.HwpHwpxLoader') as loader:
                loader.return_value.load.return_value = result
                return load_hwp_hwpx(path)

    def test_empty_document_list(self):
        elements, warnings, reason = self.mocked_result([])
        self.assertEqual(elements, [])
        self.assertEqual(reason, 'hwp_empty_output')

    def test_empty_body(self):
        for content in ['', ' \n\t']:
            with self.subTest(content=content):
                elements, _, reason = self.mocked_result([
                    SimpleNamespace(page_content=content, metadata={})])
                self.assertEqual(elements, [])
                self.assertEqual(reason, 'hwp_empty_output')

    def test_empty_body_does_not_discard_nonempty_table(self):
        elements, _, reason = self.mocked_result([
            SimpleNamespace(page_content='', metadata={'element_type': 'body'}),
            SimpleNamespace(page_content='| 항목 | 값 |', metadata={'element_type': 'table'})])
        self.assertIsNone(reason)
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]['path'], 'loader/e1')
        self.assertEqual(elements[0]['raw_text'], '| 항목 | 값 |')

    def test_unexpected_results_discard_partial_output(self):
        good = SimpleNamespace(page_content='정상 본문', metadata={})
        bad_documents = [None, object(), SimpleNamespace(page_content=123, metadata={}),
                         SimpleNamespace(page_content='본문', metadata=None),
                         SimpleNamespace(page_content='본문', metadata=[]),
                         SimpleNamespace(page_content='본문', metadata={1: '값'}),
                         SimpleNamespace(page_content='', metadata=None)]
        for result in [None, 'unexpected', {}, (good,)] + [[good, bad] for bad in bad_documents]:
            with self.subTest(result=result):
                elements, warnings, reason = self.mocked_result(result)
                self.assertEqual(elements, [])
                self.assertEqual(reason, 'hwp_loader_result_invalid')
                self.assertEqual(warnings[0]['code'], 'hwp_loader_result_error')

    def test_empty_metadata_is_preserved(self):
        elements, _, reason = self.mocked_result([
            SimpleNamespace(page_content='본문', metadata={})])
        self.assertIsNone(reason)
        self.assertEqual(elements[0]['location']['loader_metadata'], {})

    def test_missing_file_propagates(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                load_hwp_hwpx(Path(directory) / 'missing.hwp')
