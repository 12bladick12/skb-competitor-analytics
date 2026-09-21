from copy import deepcopy
from io import BytesIO
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from docx import Document
from cloud.downloads import asset_download, release_download
from cloud.drive_store import StorageError
from cloud.exports import render_files
from cloud.document_style import document_html
from test_cloud_drafts import fixture, draft_fixture
from cloud.draft_rules import snapshot


class DownloadTests(unittest.TestCase):
    def test_archive_download_is_lazy_and_rechecks_authorization(self):
        settings = {'access': {'viewer_emails': ['reader@example.com']}}
        claims = {'email': 'reader@example.com'}
        with patch('cloud.screens.file_bytes', return_value=b'file') as read:
            callback = asset_download(lambda: settings, lambda: claims, 'import', 'asset')
            read.assert_not_called()
            self.assertEqual(callback(), b'file')
            self.assertEqual(read.call_args.args[0](), settings)
            read.side_effect = PermissionError('Access revoked')
            with self.assertRaises(PermissionError):
                callback()

    def test_release_waits_for_own_job_and_downloads_exact_published_file(self):
        with patch('cloud.downloads.JobService') as jobs, patch('cloud.downloads.Repository') as repo, \
             patch('cloud.downloads.asset_download', return_value=lambda: b'%PDF-test') as asset, \
             patch('cloud.downloads.time.sleep'):
            service = jobs.return_value
            service.export.return_value = {'id': 'job', 'status': 'queued'}
            store = MagicMock()
            service.context.return_value = (store, None)
            store.query.return_value = [{'id': 'job', 'status': 'success', 'result_id': 'report'}]
            repo.return_value.load.return_value = {'id': 'import', 'report': {'report': {'pdf_id': 'digest'}}}
            start = MagicMock()
            callback = release_download(lambda: {}, lambda: {}, 'import', '2026-09', 2, 'pdf_id', start, draft_id='draft')
            service.export.assert_not_called()
            self.assertEqual(callback(), b'%PDF-test')
            service.export.assert_called_once_with('import', '2026-09', 2, draft_id='draft')
            self.assertEqual(store.query.call_args.args[1], ['import', 'job'])
            self.assertEqual(asset.call_args.args[2:], ('import', 'digest'))
            start.assert_called_once()

    def test_release_failure_or_revocation_does_not_download(self):
        with patch('cloud.downloads.JobService') as jobs, patch('cloud.downloads.asset_download') as asset:
            service = jobs.return_value
            service.export.return_value = {'id': 'job', 'status': 'error', 'error': 'Выпуск не завершён'}
            callback = release_download(lambda: {}, lambda: {}, 'i', '2026-09', 1, 'pdf_id', MagicMock())
            with self.assertRaises(StorageError):
                callback()
            service.export.side_effect = PermissionError('Access revoked')
            with self.assertRaises(PermissionError):
                callback()
            asset.assert_not_called()

    def test_pdf_word_and_zip_have_same_frozen_content_and_safe_text(self):
        import hashlib
        data = fixture()
        raw = b'<html>Proof</html>'
        digest = hashlib.sha256(raw).hexdigest()
        for event in data['event'].values():
            event['evidence_ids'] = [digest]
        draft = draft_fixture(data)
        draft['items'][0]['title'] = '<script>Точный заголовок</script>'
        draft['items'][0]['description'] = 'Описание с Ё и переносом\nВторая строка'
        draft['conclusions'] = 'Выводы без изменений'
        value = snapshot(draft, data)
        before = deepcopy(value)
        with patch('cloud.exports.render_pdf', side_effect=lambda doc: document_html(doc).encode()) as pdf:
            files = render_files(value, {digest: raw}, data['competitor'])
        markup = files['pdf_id'].decode()
        self.assertIn('&lt;script&gt;Точный заголовок&lt;/script&gt;', markup)
        self.assertNotIn('<script>', markup)
        self.assertIn('Описание с Ё и переносом', markup)
        self.assertIn('Вторая строка', markup)
        self.assertIn('Выводы без изменений', markup)
        self.assertIn('href="https://example.com/news"', markup)
        doc = Document(BytesIO(files['docx_id']))
        self.assertEqual(document_html(doc), markup)
        with ZipFile(BytesIO(files['bundle_id'])) as archive:
            self.assertEqual(archive.read('report.docx'), files['docx_id'])
            self.assertEqual(archive.read('report.pdf'), files['pdf_id'])
        self.assertEqual(value, before)
        pdf.assert_called_once()
