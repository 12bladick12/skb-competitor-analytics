import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from requests.structures import CaseInsensitiveDict

from price_monitor.models import Observation
from price_monitor.robots import Robots
from price_monitor.sensoren_browser import SensorenBrowserClient, is_rcpc
from price_monitor.transport import FetchError, SourceClient

URL = 'https://sensoren.ru/product/example/'
RCPC = '<script>document.cookie="RCPC=fixture; path=/";location.href="?attempt=1"</script>'
HTML = '<html><title>Product</title><h1>SI5000</h1></html>'


class BrowserTransportChecks(unittest.TestCase):
    def setUp(self):
        self.client = SensorenBrowserClient()
        self.client.wait = Mock()
        self.client.policies['sensoren.ru'] = Robots('User-agent: *\nDisallow: /*?\n')
        self.client._policy_times['sensoren.ru'] = time.monotonic()
        self.dns = patch('price_monitor.sensoren_browser.check_public_host').start()

    def tearDown(self):
        self.client.close()
        patch.stopall()

    def configure_http(self, responses):
        context = Mock()
        objects = []
        for code, body, headers in responses:
            response = Mock(status=code, headers=headers)
            response.body.return_value = body.encode()
            objects.append(response)
        context.request.get.side_effect = objects
        self.client._context = context
        return context, objects

    def test_http_reuses_context_and_disposes_responses(self):
        context, responses = self.configure_http([(200, HTML, {'content-type': 'text/html'})] * 2)
        for _ in range(2):
            self.assertEqual(self.client.fetch(URL), (URL, 200, HTML))
        self.assertEqual(context.request.get.call_count, 2)
        for response in responses:
            response.dispose.assert_called_once()
        self.assertEqual(context.request.get.call_args.kwargs['max_redirects'], 0)
        self.assertEqual(context.request.get.call_args.kwargs['max_retries'], 0)

    def test_rcpc_recovers_once_then_retries_http(self):
        context, responses = self.configure_http([
            (503, RCPC, {}), (200, HTML, {'content-type': 'text/html'})])
        self.client._refresh_session = Mock()
        self.assertEqual(self.client.fetch(URL)[1], 200)
        self.client._refresh_session.assert_called_once_with(URL)
        self.assertEqual(context.request.get.call_count, 2)
        for response in responses:
            response.dispose.assert_called_once()

    def test_remaining_challenge_stops_source(self):
        self.configure_http([(503, RCPC, {})] * 2)
        self.client._refresh_session = Mock()
        with self.assertRaises(FetchError) as error:
            self.client.fetch(URL)
        self.assertTrue(error.exception.stop_source)
        self.client._refresh_session.assert_called_once()

    def test_auth_rate_limit_and_other_challenges_never_open_browser_page(self):
        self.client._refresh_session = Mock()
        for code, body in [(401, ''), (403, RCPC), (429, ''),
                           (503, '<title>Verify you are human CAPTCHA</title>')]:
            with self.subTest(code=code):
                self.configure_http([(code, body, {})])
                with self.assertRaises(FetchError) as error:
                    self.client.fetch(URL)
                self.assertTrue(error.exception.stop_source)
        self.client._refresh_session.assert_not_called()

    def test_retry_after_never_refreshes_session(self):
        self.configure_http([(503, RCPC, {'retry-after': '60'})])
        self.client._refresh_session = Mock()
        with self.assertRaises(FetchError):
            self.client.fetch(URL)
        self.client._refresh_session.assert_not_called()

    def test_disallowed_urls_are_rejected_before_browser_start(self):
        self.client._ensure_context = Mock()
        for url in [URL+'?attempt=1', 'https://127.0.0.1/product/x/',
                    'https://other.example/product/x/', 'http://sensoren.ru/product/x/']:
            with self.subTest(url=url):
                with self.assertRaises(FetchError):
                    self.client.fetch(url)
        self.client._ensure_context.assert_not_called()

    def test_http_redirect_cannot_escape_robots_or_host(self):
        for location in [URL+'?attempt=1', 'https://other.example/product/x/']:
            with self.subTest(location=location):
                context, _ = self.configure_http([(302, '', {'location': location})])
                with self.assertRaises(FetchError):
                    self.client.fetch(URL)
                self.assertEqual(context.request.get.call_count, 1)

    def test_robot_redirect_is_read_with_plain_http_and_fails_closed(self):
        self.client.policies.clear()
        self.client._ensure_context = Mock()
        with patch.object(SourceClient, '_request', side_effect=[
            (302, {'Location': '/robot-rules.txt'}, ''),
            (200, {}, 'User-agent: *\nDisallow: /product/\n')]) as request:
            with self.assertRaises(FetchError) as error:
                self.client.fetch(URL)
        self.assertEqual(error.exception.status, 'robots_denied')
        self.assertEqual(request.call_count, 2)
        self.client._ensure_context.assert_not_called()

    def test_robot_policy_is_reloaded_after_half_hour(self):
        self.client._policy_times['sensoren.ru'] -= 1801
        with patch.object(SourceClient, '_request', return_value=(200, {}, 'User-agent: *\nDisallow: /product/')) as request:
            with self.assertRaises(FetchError):
                self.client.policy(URL)
        request.assert_called_once()

    def test_cancel_and_oversize_release_response(self):
        context, responses = self.configure_http([(200, HTML, {'content-type': 'text/html'})])
        with self.assertRaises(FetchError):
            self.client._session_request(URL, 2)
        responses[0].dispose.assert_called_once()
        self.client.cancelled = lambda: True
        with self.assertRaises(FetchError) as error:
            self.client.fetch(URL)
        self.assertEqual(error.exception.status, 'cancelled')
        self.assertEqual(context.request.get.call_count, 1)

    def test_session_refresh_budget(self):
        self.client._refresh_times = [time.monotonic()] * 3
        with self.assertRaises(FetchError) as error:
            self.client._refresh_session(URL)
        self.assertTrue(error.exception.stop_source)
        self.assertIsNone(self.client._context)

    def test_sitemap_headers_and_404(self):
        context, _ = self.configure_http([(200, '<urlset/>', {'content-type': 'application/xml'}),
                                         (404, 'missing', {'content-type': 'text/html'})])
        self.assertEqual(self.client.fetch_document('https://sensoren.ru/sitemap.xml')[2], '<urlset/>')
        self.assertIn('application/xml', context.request.get.call_args.kwargs['headers']['Accept'])
        self.assertEqual(self.client.fetch(URL)[1], 404)

    def test_canonical_revisit_after_browser_rcpc(self):
        context, page = Mock(), Mock()
        self.client._context = context
        context.new_page.return_value = page
        context.cookies.return_value = [{'name': 'RCPC'}]
        callbacks = {}
        page.on.side_effect = lambda name, callback: callbacks.update({name: callback})
        replies = []
        for status, body in [(503, RCPC), (200, HTML)]:
            response = Mock(status=status, headers={}, url=URL, frame=page.main_frame)
            response.text.return_value = body
            response.request.is_navigation_request.return_value = True
            replies.append(response)
        page.goto.side_effect = lambda *a, **k: callbacks['response'](replies.pop(0))
        self.client._refresh_session(URL)
        self.assertEqual(page.goto.call_count, 2)
        self.assertTrue(all(c.args[0] == URL for c in page.goto.call_args_list))
        page.close.assert_called_once()
        context.unroute.assert_called_once()

    def test_browser_recovers_when_redirect_discards_original_response_body(self):
        context, page = Mock(), Mock()
        self.client._context = context
        context.new_page.return_value = page
        context.cookies.return_value = [{'name': 'RCPC'}]
        callbacks = {}
        context.route.side_effect = lambda pattern, callback: callbacks.update(route=callback)
        page.on.side_effect = lambda name, callback: callbacks.update({name: callback})
        responses = []
        for status in [503, 200]:
            response = Mock(status=status, headers={}, url=URL, frame=page.main_frame)
            response.request.is_navigation_request.return_value = True
            if status == 503:
                response.text.side_effect = RuntimeError('Response was discarded after navigation')
            else:
                response.text.return_value = HTML
            responses.append(response)
        page.content.return_value = '<html></html>'
        denied = Mock()
        denied.request.url = URL+'?attempt=1'
        denied.request.method = 'GET'
        denied.request.frame = page.main_frame
        denied.request.is_navigation_request.return_value = True
        def navigate(*args, **kwargs):
            response = responses.pop(0)
            callbacks['response'](response)
            if response.status == 503:
                callbacks['route'](denied)
        page.goto.side_effect = navigate
        self.client._refresh_session(URL)
        denied.abort.assert_called_once()
        denied.continue_.assert_not_called()
        self.assertEqual(page.goto.call_count, 2)

    def test_expired_session_refreshes_again_on_next_request(self):
        self.configure_http([(503, RCPC, {}), (200, HTML, {'content-type': 'text/html'})] * 2)
        self.client._refresh_session = Mock()
        self.assertEqual(self.client.fetch(URL)[1], 200)
        self.assertEqual(self.client.fetch(URL)[1], 200)
        self.assertEqual(self.client._refresh_session.call_count, 2)

    def test_thread_ownership_and_source_scope(self):
        self.client._thread = -1
        try:
            with self.assertRaises(RuntimeError):
                self.client._ensure_context()
        finally:
            self.client._thread = threading.get_ident()
        with self.assertRaises(ValueError):
            SensorenBrowserClient('teko')
        self.assertFalse(is_rcpc(403, RCPC))


class AgentFactoryChecks(unittest.TestCase):
    def test_explicit_jobs_use_shared_factory_and_close_it(self):
        from price_monitor.sensoren_agent import SensorenAgent
        store, client = Mock(), Mock()
        client.fetch.return_value = (URL, 200, HTML)
        factory = Mock(return_value=client, __name__='TestClient')
        job = dict(run_id=7, source='sensoren', manufacturer='ifm', article='SI5000',
                   product_url=URL, url_template='', stop_reason='')
        store.claim.side_effect = [{**job, 'job_id': 1}, {**job, 'job_id': 2}, RuntimeError('test finished')]
        root = Path(__file__).resolve().parents[1] / 'data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            agent = SensorenAgent(store, directory, client_factory=factory)
            with patch('price_monitor.sensoren_agent.ADAPTERS') as adapters:
                adapters['sensoren'].parse.return_value = Observation('priced', URL, price='10', currency='RUB')
                with self.assertRaisesRegex(RuntimeError, 'test finished'):
                    agent.run()
        factory.assert_called_once()
        self.assertEqual(client.fetch.call_count, 2)
        client.close.assert_called_once()
        self.assertEqual(store.record.call_count, 2)
        store.release.assert_called_once_with(agent.owner)

    def test_catalog_factory_persists_specs_and_rejects_unselected_brands(self):
        from price_monitor.catalog import process_catalog
        from price_monitor.storage import Store
        from price_monitor.library import Library
        root = Path(__file__).resolve().parents[1] / 'data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store = Store(Path(directory) / 'catalog.sqlite3')
            run_id = store.enqueue_catalog({'sensoren': ['ifm']})
            owner = 'router2-test'
            self.assertTrue(store.acquire(owner))
            self.assertEqual(store.claim_run(owner), run_id)
            client = Mock()
            html = '''<h1>Датчик ifm SI5000</h1><div class="product-info">
                <div class="product-info__brand-name">ifm</div>
                <div class="product-info__all-order-price"><span class="price">29530 руб.</span></div>
                <ul class="characteristics-all"><li>Напряжение:<span>24 В</span></li></ul></div>'''
            own = 'https://sensoren.ru/product/datchik_ifm_si5000/'
            foreign = 'https://sensoren.ru/product/unknown_model/'
            def fetch(url, html_only=False):
                if url == own:
                    return url, 200, html
                if url == foreign:
                    return url, 200, html.replace('ifm', 'Balluff')
                if url.endswith('.xml'):
                    return url, 200, '<urlset/>'
                return url, 200, '<a href="'+own+'">own</a><a href="'+foreign+'">other</a>'
            client.fetch_document.side_effect = fetch
            factory = Mock(return_value=client)
            process_catalog(store.catalog, run_id, 'sensoren', owner, threading.Event(), factory)
            factory.assert_called_once()
            client.close.assert_called_once()
            total, rows = Library(store.catalog).products(source='sensoren')
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]['manufacturer'], 'ifm')
            self.assertEqual(rows[0]['attributes_count'], 1)
            self.assertEqual(rows[0]['status'], 'priced')
            states = store.catalog.batch('SELECT state FROM catalog_pages WHERE url=%(url)s', {'url': foreign})
            self.assertEqual(states[0]['state'], 'skipped')
            self.assertEqual(store.catalog.source(run_id, 'sensoren')['state'], 'completed')
            store.close()


if __name__ == '__main__':
    unittest.main()
