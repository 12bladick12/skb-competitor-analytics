from __future__ import annotations

import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests import exceptions as request_exceptions

from .logger import logger
from .models import AppSettings
from .network import resolved_proxy
from .utils import is_http_url, normalize_text, safe_filename, utcnow_naive


class CrawlerBase:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.session = requests.Session()
        # An explicit project proxy must win over inherited HTTP_PROXY values.
        # This matters in scheduled jobs and managed shells where environment
        # proxies can point to an unavailable isolation endpoint.
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": settings.user_agent})
        self.proxy = resolved_proxy(settings)
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})
        self.last_url = ''
        self._browser = None
        self._playwright = None
        self._browser_status = 0
        self.last_error = ''

    def sleep(self) -> None:
        time.sleep(self.settings.request_delay_seconds)

    def fetch(self, url: str, timeout_seconds: int | None = None, crawl_mode: str = "http") -> tuple[str, int, str]:
        if not is_http_url(url):
            logger.debug("Skipped non-http URL: {}", url)
            return "", 0, "skipped_non_http_url"
        if crawl_mode == "playwright":
            html = self.fetch_with_playwright(url)
            if html:
                return html, self._browser_status, "ok_playwright"
            browser_error=self.last_error or 'browser returned no content'
            if browser_error == 'page_not_found':
                return '',self._browser_status,'error: page_not_found'
            # Some publishers serve a complete public article over plain HTTP
            # even when the browser navigation times out. Validate it normally.
            html,code,status=self.fetch(url,timeout_seconds=timeout_seconds,crawl_mode='http_no_browser')
            if html:
                return html,code,'ok_http_fallback'
            return '',code,'error: '+browser_error+'; '+status
        try:
            response = self.session.get(url, timeout=timeout_seconds or self.settings.timeout_seconds)
            response.raise_for_status()
            self.last_url = response.url
            html = self._decode_response(response)
            if self.invalid_content(html):
                return '', response.status_code, 'error: ' + self.invalid_content(html)
            return html, response.status_code, "ok"
        except (request_exceptions.ProxyError, request_exceptions.ConnectionError, request_exceptions.Timeout) as exc:
            logger.warning("HTTP fetch failed for {}: {}", url, exc)
            if self.proxy and self.settings.allow_direct_fallback:
                try:
                    with requests.Session() as direct:
                        direct.trust_env = False
                        response = direct.get(url, headers=self.session.headers, timeout=timeout_seconds or self.settings.timeout_seconds)
                        response.raise_for_status()
                        html = self._decode_response(response)
                        if not self.invalid_content(html):
                            self.last_url = response.url
                            return html, response.status_code, 'ok_direct_fallback'
                except request_exceptions.RequestException:
                    pass
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            return "", status, f"error: {exc}"
        except Exception as exc:
            logger.warning("HTTP fetch failed for {}: {}", url, exc)
            if self.settings.use_playwright_fallback and crawl_mode!='http_no_browser':
                html = self.fetch_with_playwright(url)
                if html:
                    return html, self._browser_status, "ok_playwright"
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            return "", status, f"error: {exc}"

    @staticmethod
    def _decode_response(response: requests.Response) -> str:
        """Decode HTML from bytes and avoid common UTF-8/CP1251 mojibake."""
        raw = response.content
        # A valid UTF-8 byte stream must not be reinterpreted as CP1251 merely
        # because a server supplied a legacy header or a detector guessed it.
        try:
            return raw.decode('utf-8-sig', errors='strict')
        except UnicodeDecodeError:
            pass
        head = raw[:8192].decode("ascii", errors="ignore")
        meta_match = re.search(r"<meta[^>]+charset=[\"']?([\w.-]+)", head, flags=re.IGNORECASE)
        declared = (response.encoding or "").lower()
        declared_candidate = response.encoding if declared not in {"iso-8859-1", "latin-1", "ascii"} else None
        candidates = [
            declared_candidate,
            meta_match.group(1) if meta_match else None,
            response.apparent_encoding,
            "utf-8",
            "cp1251",
            response.encoding if declared_candidate is None else None,
        ]
        best_text = ""
        best_score: tuple[int, int] | None = None
        for index, encoding in enumerate(candidates):
            if not encoding:
                continue
            try:
                decoded = raw.decode(encoding, errors="replace")
            except (LookupError, UnicodeDecodeError):
                continue
            corruption = sum(decoded.count(marker) for marker in ("�", "Ð", "Ñ", "Ã", "Â"))
            latin1_letters = sum("À" <= character <= "ÿ" for character in decoded)
            if latin1_letters >= 4:
                corruption += latin1_letters
            score = (corruption, index)
            if best_score is None or score < best_score:
                best_text, best_score = decoded, score
        return best_text or raw.decode("utf-8", errors="replace")

    def fetch_with_playwright(self, url: str) -> str:
        self.last_error = ''
        try:
            from playwright.sync_api import sync_playwright

            if self._browser is None:
                self._playwright = sync_playwright().start()
                p = self._playwright
                launch_options: dict[str, object] = {"headless": True}
                if self.proxy:
                    launch_options["proxy"] = {"server": self.proxy}
                edge_paths = (
                    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
                )
                edge_path = next((path for path in edge_paths if path.exists()), None)
                if edge_path:
                    launch_options["executable_path"] = str(edge_path)
                elif Path('/usr/bin/chromium').is_file():
                    launch_options['executable_path'] = '/usr/bin/chromium'
                self._browser = p.chromium.launch(**launch_options)
            page = self._browser.new_page()
            try:
                self._browser_status = 0
                def track_response(response):
                    if response.request.resource_type == 'document' and response.frame == page.main_frame:
                        self._browser_status = response.status
                page.on('response', track_response)
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=self.settings.timeout_seconds * 1000)
                except Exception:
                    # A slow subresource must not discard an already received page.
                    if not 200<=self._browser_status<400:
                        raise
                try:
                    page.wait_for_load_state("networkidle", timeout=min(self.settings.timeout_seconds, 8) * 1000)
                except Exception:
                    pass
                page.wait_for_timeout(1200)
                # Some sites return an empty application shell before rendering
                # either the article or a soft 404. Do not snapshot that shell.
                if len(page.locator('body').inner_text().strip()) < 200:
                    try:
                        page.wait_for_function("document.body && document.body.innerText.trim().length >= 200",timeout=15000)
                    except Exception:
                        self.last_error='content_not_loaded'
                        return ''
                if self.invalid_content(page.content()):
                    try:
                        page.wait_for_function("!(/just a moment|один момент/i.test(document.title))",timeout=12000)
                    except Exception:
                        pass
                html = page.content()
                self.last_url = page.url
                if self.invalid_content(html) or self._browser_status >= 400:
                    self.last_error=self.invalid_content(html) or f'HTTP {self._browser_status}'
                    return ''
                return html
            finally:
                page.close()
        except Exception as exc:
            self.last_error=type(exc).__name__+': '+str(exc).splitlines()[0][:220]
            logger.debug("Playwright fallback failed for {}: {}", url, exc)
            return ""

    def close(self) -> None:
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None
        self.session.close()

    @staticmethod
    def invalid_content(html: str) -> str:
        soup = BeautifulSoup(html or '', 'lxml')
        title = normalize_text(soup.title.get_text(' ', strip=True) if soup.title else '').casefold()
        body = normalize_text(soup.get_text(' ', strip=True)).casefold()
        if soup.body and len(soup.body.get_text(' ',strip=True)) < 40 and soup.select_one('script[src],app-root,webx-root'):
            return 'content_not_loaded'
        headings = [normalize_text(h.get_text(' ',strip=True)).casefold() for h in soup.select('h1')]
        missing = {'not found','page not found','404','404 not found','страница не найдена'}
        if title in missing or any(h in missing for h in headings):
            return 'page_not_found'
        if any(marker in body for marker in ('resource you are looking for does not exist',"resource you are looking for doesn't exist",'ресурс, который вы ищете, не существует')):
            return 'page_not_found'
        if any(marker in title for marker in ('just a moment', 'один момент', '403 forbidden', 'access denied')):
            return 'access_block'
        if any(marker in body for marker in ('sorry, something went wrong when loading the data', 'generated by cloudfront', 'выполнение проверки безопасности')):
            return 'content_load_error'
        return ''

    def parse_html(self, html: str) -> tuple[str, str, BeautifulSoup]:
        soup = BeautifulSoup(html or "", "lxml")
        for tag in soup(["style", "noscript"]):
            tag.decompose()
        for tag in soup.find_all("script"):
            if (tag.get("type") or "").lower() != "application/ld+json":
                tag.decompose()
        title = normalize_text(soup.title.get_text(" ", strip=True) if soup.title else "")
        visible_soup = BeautifulSoup(str(soup), "lxml")
        for tag in visible_soup.find_all("script"):
            tag.decompose()
        text = normalize_text(visible_soup.get_text(" ", strip=True))
        return title, text, soup

    def save_snapshot(self, competitor_code: str, source_type: str, url: str, html: str) -> Path:
        import hashlib
        now = utcnow_naive()
        folder = self.settings.snapshots_dir / competitor_code / f"{now:%Y-%m}" / source_type
        folder.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256((html or '').encode('utf-8')).hexdigest()[:16]
        filename = f"{now:%Y%m%d_%H%M%S}_{safe_filename(url)}_{digest}.html"
        path = folder / filename
        path.write_text(html or "", encoding="utf-8")
        return path
