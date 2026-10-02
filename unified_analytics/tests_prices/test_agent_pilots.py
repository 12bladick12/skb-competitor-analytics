import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_pilots.common import ModelError, ResponsesClient, fingerprint, write_json
from agent_pilots.documents import (FIELDS, content_for, digest_file, evaluate,
                                    save_proposal, validate_result, verify_task)
from agent_pilots.health import compare_samples, inspect_snapshot, parse_sample, probe_card
from agent_pilots.report import write_report


def snapshot():
    return {'observed_at_epoch': 1000,
            'runs': [{'id': 7, 'state': 'running', 'cancel_requested': 0}],
            'sources': [{'run_id': 7, 'source': 'sensoren', 'state': 'running'}],
            'queue': [{'source': 'sensoren', 'state': 'processing', 'pages': 1}],
            'leases': [{'source': 'sensoren', 'owner': 'new-worker', 'heartbeat_age_seconds': 5}],
            'health': [{'source': 'sensoren', 'owner': 'new-worker', 'run_id': 7,
                        'phase': 'parse', 'activity_age_seconds': 200}]}


def unknown_result():
    return {'document_model': 'TEST-1', 'applicability': 'same_variant',
            'applicability_evidence': 'Совпадает маркировка TEST-1', 'notes': '',
            'fields': [{'name': name, 'status': 'not_found', 'value': None, 'unit': '',
                        'raw_value': '', 'page': None, 'bbox': None, 'evidence': 'Не обозначено',
                        'datum': ''} for name in FIELDS]}


def task_in(directory):
    # Tiny genuine PNG; no network or original company passports in tests.
    import base64
    image = directory / 'page.png'
    image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aVuoAAAAASUVORK5CYII='))
    text = directory / 'text.txt'
    text.write_text('PAGE 1\nTEST-1', encoding='utf-8')
    task = {'id': 'test-1', 'requested_variant': 'TEST-1', 'source_document': 'not-sent-to-api',
            'source_document_sha256': 'a' * 64, 'page_count': 1,
            'text_path': str(text), 'text_sha256': digest_file(text),
            'images': [{'page': 1, 'path': str(image), 'sha256': digest_file(image)}],
            'dataset': 'holdout', 'source_kind': 'fixture'}
    task['task_hash'] = fingerprint(task)
    return task


class HealthTests(unittest.TestCase):
    def test_heartbeat_does_not_hide_stalled_work(self):
        self.assertEqual(inspect_snapshot(snapshot(), now=1005)[0]['code'], 'stalled_work')

    def test_old_snapshot_never_claims_current_health(self):
        self.assertEqual(inspect_snapshot(snapshot(), now=3000)[0]['severity'], 'unknown')

    def test_completed_monthly_run_does_not_require_new_prices(self):
        value = snapshot()
        value['sources'][0]['state'] = 'completed'
        value['leases'] = []
        self.assertEqual(inspect_snapshot(value, now=1005)[0]['code'], 'idle')

    def test_foreign_worker_progress_not_used(self):
        value = snapshot()
        value['health'][0]['owner'] = 'previous-worker'
        self.assertEqual(inspect_snapshot(value, now=1005)[0]['code'], 'health_unavailable')

    def test_database_failure_not_reported_as_no_products(self):
        value = snapshot()
        value['queue'] = {'error': 'TimeoutError'}
        self.assertEqual(inspect_snapshot(value, now=1005)[0]['code'], 'telemetry_unavailable')

    def test_reconciliation_is_work_even_without_pending_pages(self):
        value = snapshot()
        value['queue'] = []
        value['health'][0].update(phase='reconcile', activity_age_seconds=15)
        self.assertEqual(inspect_snapshot(value, now=1005)[0]['code'], 'progressing')

    def test_lost_fields_detected_when_price_still_parses(self):
        rule = dict(source='sensoren', manufacturer='ifm', article='SI5000',
                    product_url='https://sensoren.ru/product/datchik_ifm_si5000/')
        html = '''<h1>Датчик ifm SI5000</h1><div class="product-info">
          <div class="product-info__brand-name">ifm</div><div class="product-info__all-order-price">
          <span class="price">29530 руб.</span></div><ul class="characteristics-all">
          <li>Напряжение:<span>24 В</span></li></ul></div>'''
        before = parse_sample(html, rule)
        after = parse_sample(html.replace('characteristics-all', 'changed-properties'), rule)
        self.assertEqual(after['observation']['status'], 'priced')
        self.assertIn('attributes_lost', [f['code'] for f in compare_samples(before, after)])

    def test_different_variants_cannot_be_compared(self):
        with self.assertRaises(ValueError):
            compare_samples({'rule': {'article': 'A'}}, {'rule': {'article': 'B'}})

    def test_probe_block_is_unknown_and_client_closed(self):
        from price_monitor.transport import FetchError
        rule = dict(source='sensoren', manufacturer='ifm', article='SI5000',
                    product_url='https://sensoren.ru/product/datchik_ifm_si5000/')
        with patch('price_monitor.transport.SourceClient') as client:
            client.return_value.fetch.side_effect = FetchError('blocked', 'No access', 403, True)
            sample, findings = probe_card(rule)
            self.assertIsNone(sample)
            self.assertEqual(findings[0]['severity'], 'unknown')
            client.return_value.close.assert_called_once()


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.task = task_in(self.directory)

    def test_unknown_is_null_not_zero(self):
        result = unknown_result()
        result['fields'][0]['value'] = 0
        with self.assertRaises(ValueError):
            validate_result(result, self.task)

    def test_dimension_requires_measurement_baseline_and_visible_page(self):
        result = unknown_result()
        field = result['fields'][1]
        field.update(status='found', value=75, unit='mm', raw_value='75', page=1,
                     bbox=[0.1, 0.2, 0.4, 0.5], datum='От торца до заднего конца')
        validate_result(result, self.task)
        field['datum'] = ''
        with self.assertRaises(ValueError):
            validate_result(result, self.task)
        field['datum'], field['page'] = 'От торца', 2
        with self.assertRaises(ValueError):
            validate_result(result, self.task)

    def test_reject_duplicate_fields(self):
        result = unknown_result()
        result['fields'][1] = result['fields'][0]
        with self.assertRaises(ValueError):
            validate_result(result, self.task)

    def test_reject_nan_and_boolean_sizes(self):
        for value in (float('nan'), float('inf'), True):
            result = unknown_result()
            result['fields'][1].update(status='found', value=value, unit='mm', raw_value='12',
                page=1, bbox=[0.1, 0.1, 0.5, 0.5], datum='От уступа')
            with self.assertRaises(ValueError):
                validate_result(result, self.task)

    def test_changed_image_and_metadata_invalidate_task(self):
        verify_task(self.task)
        changed = {**self.task, 'requested_variant': 'OTHER'}
        with self.assertRaises(ValueError):
            verify_task(changed)
        Path(self.task['images'][0]['path']).write_bytes(b'new revision')
        with self.assertRaises(ValueError):
            verify_task(self.task)

    def test_prompt_does_not_include_gold_or_internal_source_path(self):
        serialized = json.dumps(content_for(self.task))
        self.assertNotIn('not-sent-to-api', serialized)
        self.assertNotIn('expected', serialized)
        self.assertIn('input_image', serialized)

    def test_proposal_is_idempotent_and_always_needs_review(self):
        reply = {'result': unknown_result(), 'model': 'test'}
        first = save_proposal(self.directory, self.task, reply)
        second = save_proposal(self.directory, self.task, {**reply, 'cache_hit': True, 'seconds': 0})
        self.assertEqual(first['id'], second['id'])
        with closing(sqlite3.connect(self.directory / 'proposals.sqlite3')) as conn:
            self.assertEqual(conn.execute('SELECT state FROM proposals').fetchall(), [('needs_review',)])

    def test_evaluation_does_not_reward_abstaining_on_every_field(self):
        record = {'task': self.task, 'reply': {'result': unknown_result()}, 'origin': 'api'}
        gold = [{'source_sha256': 'a' * 64, 'process_thread': 'G1/2', 'overall_dimension_mm': 75,
                 'tip_diameter_mm': 7, 'sensitive_element_length_mm': None, 'shoulder_to_tip_mm': None}]
        result = evaluate([record, record], gold)
        self.assertEqual(result['evaluated_documents'], 1)
        self.assertEqual(result['known_value_accuracy'], 0)
        self.assertEqual(result['counts']['missed_known'], 3)
        self.assertEqual(result['counts']['abstained_unverified'], 2)

    def test_empty_evaluation_is_not_zero_accuracy(self):
        self.assertIsNone(evaluate([], [])['known_value_accuracy'])

    def test_report_escapes_instructions_and_markup(self):
        path = write_report(self.directory / 'report.html', 'Пилот', '<script>alert(1)</script>')
        text = path.read_text(encoding='utf-8')
        self.assertNotIn('<script>', text)
        self.assertIn('&lt;script&gt;', text)

    def test_prepare_cli_does_not_call_api(self):
        from agent_pilots.__main__ import main
        source = self.directory / 'inputs'
        source.mkdir()
        (source / '01_text.txt').write_text('PAGE 1\nTEST-1', encoding='utf-8')
        (source / '01_drawing_1.png').write_bytes(Path(self.task['images'][0]['path']).read_bytes())
        write_json(source / 'index.json', [{'index': 1, 'name': 'TEST-1.pdf',
                   'source': 'https://example.com/test.pdf', 'sha256': 'a'*64, 'pages': 1, 'drawing_pages': [1]}])
        output = self.directory / 'prepared'
        with patch('agent_pilots.common.requests.Session') as session:
            self.assertEqual(main(['prepare', '--source', str(source), '--output', str(output)]), 0)
            session.assert_not_called()
        self.assertTrue((output / 'documents.html').exists())
        self.assertEqual(len(json.loads((output / 'manifest.json').read_text(encoding='utf-8'))['tasks']), 1)


class ApiTests(unittest.TestCase):
    def test_401_redacted_and_no_retry(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'OPENAI_API_KEY': 'secret-for-test'}):
            session = Mock()
            session.post.return_value.status_code = 401
            client = ResponsesClient('test', folder, session=session)
            with self.assertRaisesRegex(ModelError, 'HTTP 401') as caught:
                client.structured('test', [], {}, 'test')
            self.assertNotIn('secret-for-test', str(caught.exception))
            self.assertEqual(session.post.call_count, 1)
            self.assertFalse(session.post.call_args.kwargs['allow_redirects'])

    def test_cache_and_budget(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'OPENAI_API_KEY': 'secret-for-test'}):
            session = Mock()
            session.post.return_value.status_code = 200
            session.post.return_value.json.return_value = {'status': 'completed', 'model': 'test', 'output': [
                {'type': 'message', 'content': [{'type': 'output_text', 'text': '{"ok":true}'}]}]}
            client = ResponsesClient('test', folder, session=session)
            self.assertFalse(client.structured('test', [], {}, 'test')['cache_hit'])
            self.assertTrue(client.structured('test', [], {}, 'test')['cache_hit'])
            with self.assertRaisesRegex(ModelError, 'лимит'):
                client.structured('changed prompt', [], {}, 'test')
            self.assertEqual(session.post.call_count, 1)

    def test_incomplete_and_refused_outputs_not_cached(self):
        for raw in ({'status': 'incomplete'}, {'status': 'completed', 'output': [
                {'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'No'}]}]}):
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'OPENAI_API_KEY': 'test'}):
                session = Mock()
                session.post.return_value.status_code = 200
                session.post.return_value.json.return_value = raw
                with self.assertRaises(ModelError):
                    ResponsesClient('test', folder, session=session).structured('test', [], {}, 'test')
                self.assertFalse((Path(folder) / 'cache').exists())


if __name__ == '__main__':
    unittest.main()
