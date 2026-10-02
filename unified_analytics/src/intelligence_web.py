"""Bounded public web access. No login, CAPTCHA solving or rotating proxies."""
from dataclasses import dataclass
import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests


class SourceUnavailable(RuntimeError):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.reason, self.status = reason, status


def public_url(url):
    p = urlsplit(url)
    if (p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password
            or p.port not in (None, 80, 443) or '\\' in url or any(ord(c) < 32 for c in url)):
        raise SourceUnavailable('unsafe_url')
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == 'https' else 80), type=socket.SOCK_STREAM)
    except OSError:
        raise SourceUnavailable('dns_unavailable') from None
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise SourceUnavailable('private_address')
    return urlunsplit((p.scheme, p.netloc, p.path or '/', p.query, ''))


@dataclass
class Page:
    url: str
    body: bytes
    content_type: str
    text: str
    status: int


class PublicWeb:
    def __init__(self, delay=1.5, session=None):
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.session.headers.update({'User-Agent': 'SKB-Induction-CompetitorMonitor/1.0'})
        self.delay, self.last = max(1.5, delay), {}
        self.robots, self.blocked = {}, {}

    def close(self):
        self.session.close()

    def _request(self, url, *, method='GET', payload=None, headers=None, limit=8*1024*1024):
        for _ in range(5):
            url = public_url(url)
            host = urlsplit(url).hostname
            if host in self.blocked:
                raise SourceUnavailable(self.blocked[host])
            time.sleep(max(0, self.delay - (time.monotonic() - self.last.get(host, 0))))
            started = time.monotonic()
            try:
                with self.session.request(method, url, json=payload, headers=headers,
                        timeout=(8, 20), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if status in (401, 403, 429, 451):
                        self.blocked[host] = f'access_restricted_http_{status}'
                        raise SourceUnavailable(self.blocked[host], status)
                    if status in (301, 302, 303, 307, 308):
                        target = urljoin(url, response.headers.get('Location', ''))
                        if target == url:
                            raise SourceUnavailable('invalid_redirect', status)
                        public_url(target)
                        self._policy(target)
                        url = target
                        if status == 303 or status in (301, 302) and method == 'POST':
                            method, payload = 'GET', None
                        continue
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > limit or time.monotonic() - started > 45:
                            raise SourceUnavailable('response_limit')
                        chunks.append(chunk)
                    body = b''.join(chunks)
                    encoding = response.encoding
                    if not encoding or encoding.lower() == 'iso-8859-1':
                        encoding = 'utf-8'
                    text = body.decode(encoding, errors='replace')
                    if status >= 400:
                        raise SourceUnavailable(f'http_{status}', status)
                    lowered = text[:20000].casefold()
                    title = re.search(r'<title[^>]*>(.*?)</title>', lowered, re.S)
                    challenge_title = title.group(1) if title else ''
                    if (any(s in challenge_title for s in ('доступ заблокирован', 'captcha', 'access denied', 'just a moment'))
                            or 'cf-chl-' in lowered or 'verify you are human' in lowered):
                        self.blocked[host] = 'access_challenge'
                        raise SourceUnavailable('access_challenge', status)
                    return Page(url, body, response.headers.get('Content-Type', ''), text, status)
            except requests.RequestException as exc:
                raise SourceUnavailable(type(exc).__name__) from None
            finally:
                self.last[host] = time.monotonic()
        raise SourceUnavailable('too_many_redirects')

    def _policy(self, url):
        p = urlsplit(url)
        origin = p.scheme + '://' + p.netloc
        if origin not in self.robots:
            # Install deny-all during retrieval to avoid recursive robots redirects.
            parser = RobotFileParser(); parser.parse(['User-agent: *', 'Disallow: /'])
            self.robots[origin] = parser
            try:
                page = self._request(origin + '/robots.txt', limit=512*1024)
                parser = RobotFileParser()
                parser.parse(page.text.splitlines())
            except SourceUnavailable as exc:
                if exc.status in (404, 410):
                    parser = RobotFileParser()
                    parser.parse(['User-agent: *', 'Allow: /'])
                else:
                    raise SourceUnavailable('robots_unavailable') from None
            self.robots[origin] = parser
        parser = self.robots[origin]
        if not parser.can_fetch('SKB-Induction-CompetitorMonitor', url):
            raise SourceUnavailable('robots_denied')
        crawl_delay = parser.crawl_delay('SKB-Induction-CompetitorMonitor') or parser.crawl_delay('*')
        if crawl_delay:
            self.delay = max(self.delay, crawl_delay)

    def fetch(self, url):
        public_url(url)
        self._policy(url)
        return self._request(url)

    def court_search(self, inn, page=1):
        if not inn.isdigit() or len(inn) != 10:
            raise ValueError('Для суда требуется точный ИНН юридического лица')
        url = 'https://kad.arbitr.ru/Kad/SearchInstances'
        self._policy(url)
        return self._request(url, method='POST', payload={
            'Sides': [{'Name': inn, 'Type': -1}], 'Judges': [], 'Courts': [],
            'CaseNumbers': [], 'Page': page, 'Count': 25},
            headers={'x-no-json': '1', 'Referer': 'https://kad.arbitr.ru/'})
