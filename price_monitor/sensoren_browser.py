"""Sensoren transport: ordinary Chromium and its shared HTTP session.

Only the external agent opts into this transport. Browser execution is bounded
and limited to the observed RCPC response on public, robots-allowed URLs.
Cookies stay in memory; no user profile, stealth mode or proxy is involved.
"""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import urlsplit

from requests.structures import CaseInsensitiveDict

from .sources import validate_url
from .transport import MAX_RESPONSE, FetchError, SourceClient, challenge, check_public_host


def is_rcpc(status, body):
    return status in (200, 503) and bool(re.search(
        r'document\.cookie\s*=\s*[\x22\x27]RCPC=', body[:20000], re.I))


class SensorenBrowserClient(SourceClient):
    """Single-thread owner, lazy browser, one context for a source/run."""

    def __init__(self, source='sensoren', cancelled=lambda: False, delay=3.0):
        if source != 'sensoren':
            raise ValueError('Браузерная сессия предназначена только для Sensoren')
        super().__init__(source, cancelled=cancelled, delay=max(3.0, delay))
        self._thread = threading.get_ident()
        self._playwright = self._browser = self._context = None
        self._reading_policy = False
        self._policy_times = {}
        self._refresh_times = []
        self.last_browser_state = {}
        self.metrics = {'http_requests': 0, 'browser_navigations': 0,
                        'session_refreshes': 0, 'contexts': 0}

    def _check_cancelled(self, cached=False):
        check = getattr(self.cancelled, 'cached', self.cancelled) if cached else self.cancelled
        if check():
            raise FetchError('cancelled', 'Запуск остановлен пользователем')

    def _assert_thread(self):
        if threading.get_ident() != self._thread:
            raise RuntimeError('Сессия Sensoren должна использоваться в потоке её владельца')

    def policy(self, url):
        host = urlsplit(url).hostname
        # A long catalog run must not keep yesterday's robots policy forever.
        if time.monotonic() - self._policy_times.get(host, 0) >= 1800:
            self.policies.pop(host, None)
        needs_load = host not in self.policies
        self._reading_policy = True
        try:
            super().policy(url)
        finally:
            self._reading_policy = False
        if needs_load:
            self._policy_times[host] = time.monotonic()

    def _ensure_context(self):
        self._assert_thread()
        self._check_cancelled()
        if self._context is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._context = self._browser.new_context(
                locale='ru-RU', service_workers='block', accept_downloads=False)
            self.metrics['contexts'] += 1
        except Exception as exc:
            self._close_browser()
            raise FetchError('network_error',
                'Не удалось запустить Chromium. На компьютере сборщика выполните '
                'python -m playwright install chromium (' + type(exc).__name__ + ')',
                stop_source=True) from exc

    def _close_browser(self):
        self._assert_thread()
        # Each response is disposed immediately; this also releases driver and
        # browser resources on cancellation, reconnect and normal completion.
        for name, method in (('_context', 'close'), ('_browser', 'close'), ('_playwright', 'stop')):
            obj = getattr(self, name)
            setattr(self, name, None)
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:
                    pass

    def close(self):
        self._close_browser()
        super().close()

    def _session_request(self, url, max_response):
        self.wait(max(0, self.next_request - time.monotonic()))
        self._check_cancelled()
        response = None
        try:
            self.metrics['http_requests'] += 1
            response = self._context.request.get(url, timeout=20000,
                max_redirects=0, max_retries=0,
                headers={'Accept': self.session.headers['Accept'], 'Cache-Control': 'no-cache'})
            self._check_cancelled()
            headers = CaseInsensitiveDict(response.headers)
            raw = response.body()
            if len(raw) > max_response:
                raise FetchError('http_error', 'Превышен лимит размера ответа', response.status)
            charset = re.search(r'charset=[\"\x27]?([\w-]+)', headers.get('Content-Type', ''), re.I)
            encoding = charset.group(1) if charset else 'utf-8'
            try:
                body = raw.decode(encoding, errors='replace')
            except LookupError:
                body = raw.decode('utf-8', errors='replace')
            return response.status, headers, body
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError('network_error', 'HTTP-сессия Sensoren: ' + type(exc).__name__) from exc
        finally:
            self.next_request = time.monotonic() + self.delay
            if response is not None:
                response.dispose()

    def _refresh_session(self, url):
        """At most two canonical navigations, never a disallowed ?attempt URL."""
        now = time.monotonic()
        self._refresh_times = [t for t in self._refresh_times if now - t < 600]
        if len(self._refresh_times) >= 3:
            raise FetchError('blocked', 'Sensoren слишком часто повторяет проверку RCPC; '
                             'сбор остановлен после трёх восстановлений за 10 минут', 503, True)
        self._refresh_times.append(now)
        self.metrics['session_refreshes'] += 1
        self.policy(url)
        host = urlsplit(url).hostname
        page = self._context.new_page()
        active = {'deadline': 0.0, 'requests': 0, 'error': None, 'attempt_blocked': False}
        documents = []

        def route_request(route):
            request = route.request
            try:
                # Refresh cancellation outside Playwright's event dispatcher:
                # synchronous DB calls here can stall routing or leak errors.
                self._check_cancelled(cached=True)
                validate_url('sensoren', request.url, product=False)
                parts = urlsplit(request.url)
                allowed = (parts.hostname == host and request.method == 'GET'
                           and self.policies[host].allows(request.url))
                if not allowed:
                    if (request.is_navigation_request() and parts.hostname == host
                            and parts.path == urlsplit(url).path
                            and re.fullmatch(r'attempt=\d+', parts.query)):
                        active['attempt_blocked'] = True
                    elif request.is_navigation_request() and request.frame == page.main_frame:
                        active['error'] = FetchError('robots_denied',
                            'Переход браузера отклонён: домен или robots.txt', stop_source=True)
                    route.abort()
                    return
                if (time.monotonic() > active['deadline'] or active['requests'] >= 80
                        or request.resource_type in ('image', 'media', 'font', 'websocket')):
                    route.abort()
                    return
                # Every allowed URL stays on the host whose public DNS and
                # robots policy were checked before this browser navigation.
                active['requests'] += 1
                route.continue_()
            except (FetchError, ValueError) as exc:
                if isinstance(exc, FetchError):
                    active['error'] = exc
                elif request.is_navigation_request() and request.frame == page.main_frame:
                    active['error'] = FetchError('blocked', 'Недопустимый адрес перехода браузера', stop_source=True)
                route.abort()

        def received(response):
            if response.request.is_navigation_request() and response.frame == page.main_frame:
                documents.append(response)

        self._context.route('**/*', route_request)
        page.on('response', received)
        try:
            for attempt in range(2):
                self.wait(max(0, self.next_request - time.monotonic()))
                self._check_cancelled()
                active.update(deadline=time.monotonic()+25, requests=0,
                              error=None, attempt_blocked=False)
                before = len(documents)
                self.metrics['browser_navigations'] += 1
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=20000)
                    # Allow inline RCPC JS/its denied redirect to finish. Do not
                    # wait for networkidle: supplier widgets can remain active.
                    page.wait_for_timeout(500)
                except Exception:
                    # Inspect the actual document response below; a blocked
                    # script redirect may interrupt an otherwise valid goto.
                    pass
                finally:
                    self.next_request = time.monotonic() + self.delay
                self._check_cancelled()
                if active['error']:
                    raise active['error']
                responses = documents[before:]
                last = responses[-1] if responses else None
                if last is None:
                    raise FetchError('network_error', 'Браузер не получил документ Sensoren')
                headers = CaseInsensitiveDict(last.headers)
                try:
                    body = last.text()
                except Exception:
                    body = page.content()
                cookie_present = any(c['name'] == 'RCPC' for c in self._context.cookies())
                # Chromium may discard the 503 response body after its inline
                # script navigates to the (blocked) attempt URL. The observed
                # redirect plus the server-issued cookie still identifies that
                # RCPC step; an arbitrary 503 never qualifies by cookie alone.
                observed_rcpc = is_rcpc(last.status, body) or (
                    last.status in (200, 503) and active['attempt_blocked'] and cookie_present)
                self.last_browser_state = dict(status=last.status, url=last.url,
                    body_bytes=len(body.encode()), rcpc=observed_rcpc,
                    attempt_blocked=active['attempt_blocked'], cookie_present=cookie_present)
                if last.status in (401, 403, 429) or (challenge(body) and not is_rcpc(last.status, body)):
                    self._check_status(last.status, headers, body, last.url, 'браузер')
                if headers.get('Retry-After'):
                    raise FetchError('http_error', 'Sensoren запросил паузу; Retry-After: '
                                     + headers['Retry-After'], last.status, True)
                if last.status in (200, 404, 410) and not challenge(body):
                    return
                if not observed_rcpc:
                    raise FetchError('http_error', f'Браузер Sensoren: HTTP {last.status}', last.status)
                if attempt or not cookie_present:
                    break
            raise FetchError('blocked', 'Проверка RCPC не завершилась за две попытки; источник остановлен', 503, True)
        finally:
            active['deadline'] = 0
            try:
                page.close()
            finally:
                self._context.unroute('**/*', route_request)

    def _request(self, url, max_response=MAX_RESPONSE):
        self._assert_thread()
        # robots.txt is always read by the original HTTP client. Its failure
        # must never start browser execution or silently allow a catalog.
        if self._reading_policy or urlsplit(url).path == '/robots.txt':
            return super()._request(url, max_response=max_response)
        try:
            validate_url('sensoren', url, product=False)
            check_public_host(urlsplit(url).hostname)
        except ValueError as exc:
            raise FetchError('blocked', str(exc), stop_source=True) from exc
        except OSError as exc:
            raise FetchError('network_error', 'DNS Sensoren недоступен') from exc
        self.policy(url)
        self._ensure_context()
        code, headers, body = self._session_request(url, max_response)
        if is_rcpc(code, body) and not headers.get('Retry-After'):
            self._refresh_session(url)
            code, headers, body = self._session_request(url, max_response)
        # SourceClient checks this final response. Any remaining RCPC/CAPTCHA
        # stops the source instead of being sent to the product parser.
        return code, headers, body
