from __future__ import annotations

import ipaddress
import re
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .robots import Robots
from .sources import SOURCES, validate_url

MAX_RESPONSE = 5 * 1024 * 1024


class FetchError(Exception):
    def __init__(self, status, message, http_status=None, stop_source=False):
        super().__init__(message)
        self.status, self.http_status, self.stop_source = status, http_status, stop_source


def check_public_host(host):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FetchError("blocked", "DNS источника указывает на непубличный адрес", stop_source=True)


def challenge(body: str) -> bool:
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    title = title.group(1).lower() if title else ""
    return bool(re.search(r"just a moment|access denied|checking your browser|verify.*human|captcha|доступ ограничен|проверка браузера|подтвердите.*человек", title) or "cf-chl-" in body[:20000])


class SourceClient:
    """One instance per source/run; never shared by request threads."""
    def __init__(self, source: str, cancelled=lambda: False, delay=2.0):
        self.spec = SOURCES[source]
        self.cancelled = cancelled
        self.delay = max(2.0, delay)
        self.next_request = 0.0
        self.policies = {}
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PriceMonitor/1.0 (+internal public-price research)", "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9", "Accept-Language": "ru,en;q=0.5"})

    def close(self):
        self.session.close()

    def redirect(self, original, location):
        target = urljoin(original, location)
        parts = urlsplit(target)
        # Some legacy TEKO links redirect to HTTP. Try that same path over TLS;
        # never send an unencrypted request or change to an unapproved host.
        if parts.scheme == "http" and parts.hostname in self.spec.hosts and parts.port in (None,80) and not parts.username and not parts.password:
            target = urlunsplit(("https",parts.hostname,parts.path,parts.query,parts.fragment))
        return target

    def wait(self, seconds):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if self.cancelled():
                raise FetchError("cancelled", "Запуск остановлен пользователем")
            time.sleep(min(0.25, max(0, until - time.monotonic())))

    def _request(self, url):
        try:
            validate_url(self.spec.id, url, product=False)
            check_public_host(urlsplit(url).hostname)
            self.wait(max(0, self.next_request - time.monotonic()))
            if self.cancelled():
                raise FetchError("cancelled", "Запуск остановлен пользователем")
            started = time.monotonic()
            with self.session.get(url, timeout=(8, 20), allow_redirects=False, stream=True) as r:
                chunks, size = [], 0
                for chunk in r.iter_content(65536):
                    if self.cancelled():
                        raise FetchError("cancelled", "Запуск остановлен пользователем")
                    size += len(chunk)
                    if size > MAX_RESPONSE or time.monotonic()-started > 45:
                        raise FetchError("http_error", "Превышен лимит размера/времени ответа", r.status_code)
                    chunks.append(chunk)
                r._content = b"".join(chunks)
                r._content_consumed = True
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = r.apparent_encoding or "utf-8"
                return r.status_code, dict(r.headers), r.text
        except (requests.RequestException, OSError) as e:
            raise FetchError("network_error", "Не удалось получить ответ источника: " + type(e).__name__) from e
        except ValueError as e:
            raise FetchError("blocked", str(e), stop_source=True) from e
        finally:
            self.next_request = time.monotonic() + self.delay

    def _check_status(self, status, headers, body):
        if status == 429:
            retry_after = headers.get("Retry-After", "не указан")
            raise FetchError("rate_limited", f"Источник остановлен: HTTP 429; Retry-After: {retry_after}", status, True)
        if status in (401, 403) or challenge(body):
            raise FetchError("blocked", "Источник требует авторизацию или ограничил автоматический доступ", status, True)

    def policy(self, url):
        host = urlsplit(url).hostname
        if host not in self.policies:
            target = f"https://{host}/robots.txt"
            try:
                for _ in range(4):
                    code, headers, body = self._request(target)
                    self._check_status(code, headers, body)
                    if code in (301,302,303,307,308) and headers.get("Location"):
                        target = self.redirect(target, headers["Location"])
                        continue
                    if code in (404,410):
                        policy = Robots("")
                    elif code == 200 and not re.search(r"<!doctype html|<html", body[:1000], re.I):
                        policy = Robots(body)
                    else:
                        raise FetchError("robots_unavailable", f"robots.txt не проверен (HTTP {code}); источник остановлен", code, True)
                    self.policies[host] = policy
                    self.delay = max(self.delay, policy.delay)
                    self.next_request = max(self.next_request, time.monotonic() + self.delay)
                    break
                else:
                    raise FetchError("robots_unavailable", "Слишком много перенаправлений robots.txt", stop_source=True)
            except FetchError as e:
                if e.status in {"cancelled", "blocked", "rate_limited", "robots_unavailable"}:
                    raise
                raise FetchError("robots_unavailable", str(e), e.http_status, True) from e
        if not self.policies[host].allows(url):
            raise FetchError("robots_denied", "URL запрещён правилами robots.txt; используйте разрешённую карточку или согласованный источник")

    def fetch(self, url):
        target = url
        for _ in range(5):
            try:
                validate_url(self.spec.id, target, product=False)
            except ValueError as e:
                raise FetchError("blocked", "Отклонено перенаправление: " + str(e), stop_source=True) from e
            self.policy(target)
            for attempt in range(2):
                try:
                    code, headers, body = self._request(target)
                except FetchError as e:
                    if e.status != "network_error" or attempt:
                        raise
                    self.wait(3)
                    continue
                self._check_status(code, headers, body)
                if code in (500,502,503,504) and not attempt:
                    # Do not retry before an explicitly requested waiting period.
                    if headers.get("Retry-After"):
                        raise FetchError("http_error", "Источник временно недоступен; Retry-After: " + headers["Retry-After"], code, True)
                    self.wait(3)
                    continue
                break
            if code in (301,302,303,307,308):
                if not headers.get("Location"):
                    raise FetchError("http_error", "Пустое перенаправление", code)
                target = self.redirect(target, headers["Location"])
                continue
            if code not in (200,404,410):
                raise FetchError("http_error", f"HTTP {code}", code)
            if code == 200 and "html" not in headers.get("Content-Type", "").lower():
                raise FetchError("parse_error", "Ожидалась HTML-карточка товара", code)
            return target, code, body
        raise FetchError("http_error", "Слишком много перенаправлений")
