"""Bounded public-document transport and private content-addressed storage."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urljoin, quote
from urllib.robotparser import RobotFileParser

import requests
from .passport_sources import valid_url
from .transport import FetchError, check_public_host

MAX_BYTES = 50 * 1024 * 1024


@dataclass
class Download:
    url: str
    status: int
    body: bytes
    headers: dict


class DocumentClient:
    def __init__(self, manufacturer, origin_host='', cancelled=lambda: False):
        self.manufacturer, self.origin_host = manufacturer, origin_host
        self.cancelled = cancelled
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'PriceMonitor/1.0', 'Accept': 'application/pdf,text/html;q=0.8,*/*;q=0.5'})
        self.policies, self.next_request, self.delays = {}, {}, {}
        self.request_count=0;self.received_bytes=0

    def close(self):
        self.session.close()

    def _get(self, url, headers=None, limit=MAX_BYTES):
        if not valid_url(url, self.manufacturer, self.origin_host):
            raise FetchError('blocked', 'Домен документа не входит в официальные источники')
        host = urlsplit(url).hostname
        try:check_public_host(host)
        except OSError as exc:raise FetchError('network_error',type(exc).__name__) from None
        while time.monotonic() < self.next_request.get(host, 0):
            if self.cancelled():
                raise FetchError('cancelled', 'Загрузка остановлена')
            time.sleep(.1)
        try:
            self.request_count+=1
            with self.session.get(url, headers=headers or {}, stream=True, allow_redirects=False, timeout=(8, 25)) as r:
                chunks, size, started = [], 0, time.monotonic()
                for chunk in r.iter_content(65536):
                    if self.cancelled():
                        raise FetchError('cancelled', 'Загрузка остановлена')
                    size += len(chunk)
                    self.received_bytes+=len(chunk)
                    if size > limit or time.monotonic() - started > 90:
                        raise FetchError('too_large', 'Превышен лимит загрузки документа')
                    chunks.append(chunk)
                return Download(url, r.status_code, b''.join(chunks), dict(r.headers))
        except requests.RequestException as exc:
            raise FetchError('network_error', type(exc).__name__) from None
        finally:
            self.next_request[host] = time.monotonic() + self.delays.get(host,2)

    def policy(self, url):
        p = urlsplit(url)
        origin = f'https://{p.netloc}'
        if origin not in self.policies:
            target = origin + '/robots.txt'
            for _ in range(5):
                r = self._get(target, limit=2 * 1024 * 1024)
                if r.status in (301, 302, 303, 307, 308):
                    target = urljoin(target, requests.structures.CaseInsensitiveDict(r.headers).get('Location', ''))
                    continue
                break
            if r.status in (404, 410):
                lines = ['User-agent: *', 'Allow: /']
            elif r.status == 200 and not r.body.lstrip().startswith(b'<'):
                lines = r.body.decode('utf-8', errors='replace').splitlines()
            else:
                raise FetchError('robots_unavailable', 'Не удалось проверить robots.txt', r.status)
            policy = RobotFileParser()
            policy.parse(lines)
            self.policies[origin] = policy
        policy = self.policies[origin]
        if not policy.can_fetch('PriceMonitor', url):
            raise FetchError('robots_denied', 'Скачивание запрещено robots.txt')
        delay = policy.crawl_delay('PriceMonitor') or policy.crawl_delay('*') or 2
        self.delays[p.hostname]=max(2,delay)
        self.next_request[p.hostname] = max(self.next_request.get(p.hostname, 0), time.monotonic() + max(0, delay - 2))

    def fetch(self, url, etag='', last_modified=''):
        headers = {}
        if etag:
            headers['If-None-Match'] = etag
        if last_modified:
            headers['If-Modified-Since'] = last_modified
        for _ in range(5):
            self.policy(url)
            r = self._get(url, headers)
            h = requests.structures.CaseInsensitiveDict(r.headers)
            if r.status in (301, 302, 303, 307, 308):
                if not h.get('Location'):
                    raise FetchError('http_error', 'Пустое перенаправление', r.status)
                url = urljoin(url, h['Location'])
                headers = {}  # validators belong to the original resource
                continue
            if r.status in (403, 429):
                raise FetchError('rate_limited' if r.status == 429 else 'blocked',
                                 'Источник ограничил скачивание; Retry-After: ' + h.get('Retry-After', ''), r.status)
            if r.status not in (200, 304, 404, 410):
                raise FetchError('http_error', f'HTTP {r.status}', r.status)
            r.headers = h
            return r
        raise FetchError('http_error', 'Слишком много перенаправлений')


class LocalFiles:
    """Offline worker cache and test storage; never part of a repository release."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, fingerprint):
        if len(fingerprint) != 64 or any(c not in '0123456789abcdef' for c in fingerprint):
            raise ValueError('Invalid file fingerprint')
        return self.root / (fingerprint + '.pdf')

    def put(self, body):
        fingerprint = hashlib.sha256(body).hexdigest()
        target = self.path(fingerprint)
        if not target.exists():
            temporary = target.with_suffix('.'+uuid.uuid4().hex+'.part')
            temporary.write_bytes(body)
            temporary.replace(target)
        return fingerprint, fingerprint + '.pdf'

    def get(self, fingerprint):
        raw = self.path(fingerprint).read_bytes()
        if hashlib.sha256(raw).hexdigest() != fingerprint:
            raise ValueError('Повреждён файл паспорта')
        return raw


class SupabaseFiles:
    def __init__(self, settings, cache=None):
        self.url = str(settings.get('url', '')).rstrip('/')
        p = urlsplit(self.url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.path not in ('', '/'):
            raise ValueError('Укажите HTTPS URL проекта Supabase в passports.url')
        self.key = settings.get('service_key', '')
        if not self.key:
            raise ValueError('Не задан passports.service_key')
        self.bucket = settings.get('bucket', 'competitor-passports')
        self.cache = cache
        self.session = requests.Session()
        self.session.headers.update({'apikey': self.key})
        # New Supabase secret keys are not JWTs. Legacy service_role keys
        # still use Bearer authentication; never send sb_secret as a JWT.
        if not self.key.startswith('sb_secret_'):
            self.session.headers['Authorization']='Bearer '+self.key

    def ensure(self):
        r = self.session.get(f'{self.url}/storage/v1/bucket/{quote(self.bucket, safe="")}', timeout=25,allow_redirects=False)
        try:response=r.json()
        except ValueError:response={}
        missing=r.status_code==404 or (r.status_code==400 and any(str(response.get(k,'')).lower()=='bucket not found' for k in ('error','message')))
        if missing:
            r = self.session.post(self.url + '/storage/v1/bucket', json={
                'id': self.bucket, 'name': self.bucket, 'public': False,
                'file_size_limit': MAX_BYTES, 'allowed_mime_types': ['application/pdf']}, timeout=25,allow_redirects=False)
        if not r.ok:
            raise RuntimeError(f'Хранилище паспортов: HTTP {r.status_code}')
        data = r.json()
        if data.get('public') is True:
            raise ValueError('Хранилище паспортов должно быть закрытым')

    def put(self, body):
        fingerprint = hashlib.sha256(body).hexdigest()
        object_key = fingerprint + '.pdf'
        r = self.session.post(f'{self.url}/storage/v1/object/{quote(self.bucket)}/{object_key}',
                              data=body, headers={'Content-Type': 'application/pdf', 'x-upsert': 'false'}, timeout=90,allow_redirects=False)
        # Storage reports an existing content-addressed object as 400 or 409.
        duplicate = r.status_code in (400, 409) and r.json().get('error') in ('Duplicate', 'KeyAlreadyExists')
        if not r.ok and not duplicate:
            raise RuntimeError(f'Сохранение паспорта: HTTP {r.status_code}')
        if self.cache:
            self.cache.put(body)
        return fingerprint, object_key

    def get(self, fingerprint):
        if self.cache and self.cache.path(fingerprint).exists():
            return self.cache.get(fingerprint)
        if len(fingerprint) != 64 or not all(c in '0123456789abcdef' for c in fingerprint):
            raise ValueError('Invalid fingerprint')
        r = self.session.get(f'{self.url}/storage/v1/object/authenticated/{quote(self.bucket)}/{fingerprint}.pdf', timeout=90,allow_redirects=False)
        if not r.ok or hashlib.sha256(r.content).hexdigest() != fingerprint:
            raise RuntimeError('Не удалось получить проверенный файл паспорта')
        if self.cache:
            self.cache.put(r.content)
        return r.content

    def signed_url(self, fingerprint):
        if len(fingerprint) != 64 or not all(c in '0123456789abcdef' for c in fingerprint):
            raise ValueError('Invalid fingerprint')
        r = self.session.post(f'{self.url}/storage/v1/object/sign/{quote(self.bucket)}/{fingerprint}.pdf', json={'expiresIn': 600}, timeout=25,allow_redirects=False)
        if not r.ok:
            raise RuntimeError(f'Ссылка на паспорт: HTTP {r.status_code}')
        return self.url + '/storage/v1' + r.json()['signedURL']
