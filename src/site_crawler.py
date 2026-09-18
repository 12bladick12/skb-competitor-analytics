from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import feedparser
from bs4 import BeautifulSoup

from .crawler_base import CrawlerBase
from .logger import logger
from .models import AppSettings, CompetitorConfig, CrawlResult, NewsExtractionProfile, ReportPeriod
from .utils import is_http_url, join_url, normalize_crawl_url, normalize_text, parse_datetime_or_none, utcnow_naive


SECTION_KEYWORDS: dict[str, list[str]] = {
    "news": ["новости", "статьи", "публикации", "news"],
    "products": ["каталог", "продукция", "товары", "products", "catalog"],
    "promotions": ["акции", "скидки", "спецпредложения", "promo"],
    "docs": ["документация", "файлы", "скачать", "инструкции", "каталоги", "docs"],
}

DATE_SELECTORS = [
    "meta[property='article:published_time']",
    "meta[property='og:published_time']",
    "meta[name='pubdate']",
    "meta[itemprop='datePublished']",
    ".article-item_date",
    ".article__date",
    ".news-date-time",
    ".date",
    ".news-date",
    ".news-detail-date",
    ".published",
    ".post-date",
    ".article-date",
]
CONTEXTUAL_DATE_LABELS = (
    ("published_text", ("post time", "publication time", "published", "время публикации", "дата публикации", "опубликовано")),
)
GLOBAL_DATE_SELECTORS = [
    "meta[property='article:published_time']",
    "meta[property='og:published_time']",
    "meta[name='pubdate']",
    "meta[itemprop='datePublished']",
]
ARTICLE_DATE_CONTAINERS = [
    ".article-main",
    ".article-item",
    ".news-detail",
    ".news-item-detail",
    ".article-detail",
    "article",
]

ATTACHMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar")
LISTING_PATHS = ("/news/", "/articles/", "/new-products/", "/text-news/")
ARTICLE_PATH_HINTS = ("news", "press", "blog", "article", "product-news", "insight", "story")
NEWS_CTA_LABELS = {"read more", "view", "learn more", "подробнее", "читать", "читать далее"}
MAIN_CONTENT_SELECTORS = [
    ".article-main",
    ".article-item",
    ".news-detail",
    ".news-item-detail",
    ".article-detail",
    "article",
    "main",
    ".content",
    ".page-content",
    "#content",
]
NOISE_SELECTORS = [
    "header",
    "footer",
    "nav",
    "aside",
    "form",
    ".menu",
    ".breadcrumb",
    ".breadcrumbs",
    ".pagination",
    ".catalog-menu",
    ".sidebar",
    ".footer",
    ".header",
]
UTILITY_PATH_PARTS = {
    "cart", "basket", "checkout", "account", "login", "search", "contact", "contacts",
    "privacy", "policy", "terms", "catalog", "product", "products", "collection",
    "productcomparison", "product-returns", "returns",
}
ARTICLE_SCHEMA_TYPES = {"newsarticle", "article", "blogposting", "report", "pressrelease"}


@dataclass(frozen=True)
class PublicationDateEvidence:
    published_at: datetime | None = None
    source: str = "missing"
    evidence: str = ""
    status: str = "missing"


class SiteCrawler(CrawlerBase):
    def __init__(self, settings: AppSettings):
        super().__init__(settings)
        self.stats = {
            "pages_fetched": 0,
            "links_skipped": 0,
            "http_errors": 0,
            "sources_seen": 0,
        }
        self._total_pages_seen = 0
        self._last_section_page_attempts = 0

    @property
    def total_pages_seen(self) -> int:
        return self._total_pages_seen

    def crawl_competitor(
        self,
        competitor: CompetitorConfig,
        period: ReportPeriod | None = None,
        page_limit: int | None = None,
    ) -> list[CrawlResult]:
        if not competitor.base_url:
            logger.info("Base URL is empty for {}, skipping site crawl", competitor.name)
            return []

        discovered = self.discover_sections(competitor) if competitor.discover_sections else {key: set() for key in SECTION_KEYWORDS}
        section_urls = self._merge_section_urls(self._configured_section_urls(competitor), discovered)
        results: list[CrawlResult] = []

        competitor_pages_seen = 0
        for source_type in ["news", "promotions", "docs", "products"]:
            urls = section_urls.get(source_type, set())
            for section_url in self._ordered_section_urls(urls, source_type):
                section_url = normalize_crawl_url(competitor.base_url, section_url)
                if not section_url:
                    self.stats["links_skipped"] += 1
                    continue
                if self._budget_exhausted(competitor_pages_seen, page_limit):
                    logger.info("Crawl budget exhausted for {}", competitor.name)
                    return results
                self.sleep()
                section_results = self.crawl_section(competitor, source_type, section_url, competitor_pages_seen, period, page_limit)
                competitor_pages_seen += self._last_section_page_attempts
                self._total_pages_seen += self._last_section_page_attempts
                results.extend(section_results)
        return results

    def discover_sections(self, competitor: CompetitorConfig) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {key: set() for key in SECTION_KEYWORDS}
        html, status_code, status = self.fetch(competitor.base_url, crawl_mode=competitor.crawl_mode)
        now = utcnow_naive()
        if status_code >= 400 or not html:
            logger.warning("Cannot discover sections for {}: {}", competitor.name, status)
            return found
        _, _, soup = self.parse_html(html)
        for link in soup.find_all("a", href=True):
            label = normalize_text(link.get_text(" ", strip=True)).lower()
            href = normalize_crawl_url(competitor.base_url, link.get("href"))
            if not href:
                self.stats["links_skipped"] += 1
                continue
            for source_type, words in SECTION_KEYWORDS.items():
                if any(word in label or word in href.lower() for word in words):
                    if not self._is_listing_url(href):
                        continue
                    if href not in found[source_type] and len(found[source_type]) < self.settings.max_discovered_sections_per_type:
                        found[source_type].add(href)
                        logger.info("Discovered {} section for {}: {}", source_type, competitor.name, href)
        logger.debug("Section discovery finished for {} at {}", competitor.name, now)
        return found

    def crawl_section(
        self,
        competitor: CompetitorConfig,
        source_type: str,
        section_url: str,
        competitor_pages_seen: int = 0,
        period: ReportPeriod | None = None,
        page_limit: int | None = None,
    ) -> list[CrawlResult]:
        logger.info("Fetch section [{}:{}] {}", competitor.code, source_type, section_url)
        self._last_section_page_attempts = 1
        html, status_code, status = self.fetch(section_url, crawl_mode=competitor.crawl_mode)
        if not html:
            self.stats["http_errors"] += 1
            logger.warning("Section unavailable {} {}: {}", competitor.name, section_url, status)
            return [
                CrawlResult(
                    competitor_code=competitor.code,
                    competitor_name=competitor.name,
                    source_type=source_type,
                    url=section_url,
                    discovered_at=utcnow_naive(),
                    checked_at=utcnow_naive(),
                    status=status,
                )
            ]

        title, text, soup = self.parse_html(html)
        if source_type == "news":
            self.save_snapshot(competitor.code, source_type, section_url, html)
        listing_items = self.extract_listing_items(section_url, soup, competitor.news_profile) if source_type == "news" else []
        results: list[CrawlResult] = []

        if source_type == "news" and listing_items:
            for idx, item in enumerate(listing_items[: self.settings.max_pages_per_section], start=1):
                published = item.get("published_at")
                if published and period and not self._in_period(published, period):
                    continue
                page_url = str(item["url"])
                if self._budget_exhausted(competitor_pages_seen + self._last_section_page_attempts, page_limit):
                    logger.info("Crawl budget exhausted while reading {}", competitor.name)
                    break
                logger.info(
                    "Fetch news item [{}:{}] {}/{} {}",
                    competitor.code,
                    source_type,
                    idx,
                    min(len(listing_items), self.settings.max_pages_per_section),
                    page_url,
                )
                self.sleep()
                self._last_section_page_attempts += 1
                page_html, _, page_status = self.fetch(page_url, crawl_mode=competitor.crawl_mode)
                if page_html:
                    page_title, page_text, page_soup = self.parse_html(page_html)
                    result = self._make_result(
                        competitor,
                        source_type,
                        page_url,
                        str(item.get("title") or page_title),
                        page_text or str(item.get("summary", "")),
                        page_html,
                        page_soup,
                        page_status,
                        fallback_date=PublicationDateEvidence(
                            published_at=published if isinstance(published, datetime) else None,
                            source=str(item.get("published_at_source", "missing")),
                            evidence=str(item.get("published_at_evidence", "")),
                            status=str(item.get("publication_date_status", "missing")),
                        ),
                    )
                    if result.published_at and period and not self._in_period(result.published_at, period):
                        continue
                    results.append(result)
                    self.stats["pages_fetched"] += 1
                else:
                    self.stats["http_errors"] += 1
                    results.append(
                        CrawlResult(
                            competitor_code=competitor.code,
                            competitor_name=competitor.name,
                            source_type=source_type,
                            url=page_url,
                            title=str(item.get("title", "")),
                            text=str(item.get("summary", "")),
                            html=str(item.get("summary", "")),
                            published_at=published if isinstance(published, datetime) else None,
                            published_at_source=str(item.get("published_at_source", "missing")),
                            published_at_evidence=str(item.get("published_at_evidence", "")),
                            publication_date_status=str(item.get("publication_date_status", "missing")),
                            discovered_at=utcnow_naive(),
                            checked_at=utcnow_naive(),
                            status=page_status,
                        )
                    )
            self.stats["sources_seen"] += 1
            for feed_url in self.extract_feed_links(section_url, soup):
                feed_results = self.crawl_feed(
                    competitor,
                    source_type,
                    feed_url,
                    period,
                    page_limit,
                    competitor_pages_seen + len(results),
                    skip_urls={self._result_url_key(item.url) for item in results},
                )
                results = self._merge_results_by_url(results, feed_results)
            return results

        if source_type != "news":
            result = self._make_result(competitor, source_type, section_url, title, text, html, soup, status)
            results.append(result)
        self.stats["pages_fetched"] += 1
        self.stats["sources_seen"] += 1

        page_links = self.extract_relevant_links(section_url, soup, source_type)
        remaining_section = max(0, self.settings.max_pages_per_section - self._last_section_page_attempts)
        for idx, page_url in enumerate(page_links[:remaining_section], start=1):
            if source_type == "news" and not self._is_news_article_url(section_url, page_url, competitor.news_profile):
                self.stats["links_skipped"] += 1
                continue
            if self._budget_exhausted(competitor_pages_seen + self._last_section_page_attempts, page_limit):
                logger.info("Crawl budget exhausted while reading {}", competitor.name)
                break
            logger.info(
                "Fetch page [{}:{}] {}/{} {}",
                competitor.code,
                source_type,
                idx,
                min(len(page_links), remaining_section),
                page_url,
            )
            self.sleep()
            self._last_section_page_attempts += 1
            page_html, _, page_status = self.fetch(page_url, crawl_mode=competitor.crawl_mode)
            if not page_html:
                self.stats["http_errors"] += 1
                continue
            page_title, page_text, page_soup = self.parse_html(page_html)
            result = self._make_result(competitor, source_type, page_url, page_title, page_text, page_html, page_soup, page_status)
            if source_type == "news" and result.published_at and period and not self._in_period(result.published_at, period):
                continue
            results.append(result)
            self.stats["pages_fetched"] += 1

        for feed_url in self.extract_feed_links(section_url, soup):
            feed_results = self.crawl_feed(
                competitor,
                source_type,
                feed_url,
                period,
                page_limit,
                competitor_pages_seen + len(results),
                skip_urls={self._result_url_key(item.url) for item in results},
            )
            results = self._merge_results_by_url(results, feed_results)

        return results

    def crawl_feed(
        self,
        competitor: CompetitorConfig,
        source_type: str,
        feed_url: str,
        period: ReportPeriod | None = None,
        page_limit: int | None = None,
        competitor_pages_seen: int = 0,
        skip_urls: set[str] | None = None,
    ) -> list[CrawlResult]:
        parsed = feedparser.parse(feed_url)
        results: list[CrawlResult] = []
        skip_urls = skip_urls or set()
        for entry in parsed.entries[: self.settings.max_pages_per_section]:
            if self._budget_exhausted(competitor_pages_seen + len(results), page_limit):
                break
            url = normalize_crawl_url(feed_url, entry.get("link", feed_url))
            if not url:
                self.stats["links_skipped"] += 1
                continue
            if self._result_url_key(url) in skip_urls:
                continue
            title = normalize_text(entry.get("title", ""))
            text = normalize_text(entry.get("summary", ""))
            raw_feed_date = entry.get("published") or entry.get("updated")
            published = parse_datetime_or_none(raw_feed_date)
            if published and period and not self._in_period(published, period):
                continue

            if source_type == "news":
                self.sleep()
                self._last_section_page_attempts += 1
                page_html, _, page_status = self.fetch(url, crawl_mode=competitor.crawl_mode)
                if page_html:
                    page_title, page_text, page_soup = self.parse_html(page_html)
                    result = self._make_result(
                        competitor,
                        source_type,
                        url,
                        page_title or title,
                        page_text or text,
                        page_html,
                        page_soup,
                        page_status,
                    )
                    if result.published_at and period and not self._in_period(result.published_at, period):
                        continue
                    results.append(result)
                    self.stats["pages_fetched"] += 1
                    continue
                self.stats["http_errors"] += 1
                results.append(
                    CrawlResult(
                        competitor_code=competitor.code,
                        competitor_name=competitor.name,
                        source_type=source_type,
                        url=url,
                        title=title,
                        text=text,
                        html=text,
                        published_at_source="feed_date_unverified",
                        published_at_evidence=f"feed={raw_feed_date}" if raw_feed_date else "",
                        publication_date_status="missing",
                        discovered_at=utcnow_naive(),
                        checked_at=utcnow_naive(),
                        status=page_status,
                    )
                )
                continue
            results.append(
                CrawlResult(
                    competitor_code=competitor.code,
                    competitor_name=competitor.name,
                    source_type=source_type,
                    url=url,
                    title=title,
                    text=text,
                    html=text,
                    published_at=published,
                    discovered_at=utcnow_naive(),
                    checked_at=utcnow_naive(),
                    status="ok_feed",
                )
            )
        return results

    @staticmethod
    def _result_url_key(url: str) -> str:
        return url.split("#", 1)[0].split("?", 1)[0].rstrip("/").lower()

    def _merge_results_by_url(self, current: list[CrawlResult], incoming: list[CrawlResult]) -> list[CrawlResult]:
        """Keep one logical news page and prefer verified, full-page evidence."""
        merged = list(current)
        positions = {self._result_url_key(item.url): index for index, item in enumerate(merged)}

        def score(item: CrawlResult) -> tuple[int, int, int]:
            return (
                int(item.publication_date_status == "verified"),
                int(bool(item.local_snapshot_path)),
                len(item.html or ""),
            )

        for item in incoming:
            key = self._result_url_key(item.url)
            position = positions.get(key)
            if position is None:
                positions[key] = len(merged)
                merged.append(item)
            elif score(item) > score(merged[position]):
                merged[position] = item
        return merged

    def extract_relevant_links(self, base_url: str, soup: BeautifulSoup, source_type: str) -> list[str]:
        base_host = urlparse(base_url).netloc
        links: list[str] = []
        keywords = SECTION_KEYWORDS.get(source_type, []) + ["подробнее", "читать", "детальнее", "new-products", "product", "item"]
        if source_type == "news":
            keywords = [word for word in keywords if word not in {"new-products", "product", "item"}]
        for a in soup.find_all("a", href=True):
            href = normalize_crawl_url(base_url, a.get("href"))
            if not href:
                self.stats["links_skipped"] += 1
                continue
            parsed = urlparse(href)
            if parsed.netloc and parsed.netloc != base_host:
                continue
            label = normalize_text(a.get_text(" ", strip=True)).lower()
            path = parsed.path.lower()
            if any(word in label or word in path for word in keywords):
                if href not in links:
                    links.append(href)
        return links

    def extract_listing_items(
        self,
        base_url: str,
        soup: BeautifulSoup,
        profile: NewsExtractionProfile | None = None,
    ) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        seen: set[str] = set()
        profile_card_links: set[int] = set()
        profile = profile or NewsExtractionProfile()
        candidates = list(soup.select("a.news-item[href], .news-list a[href], article a[href], .post a[href], .article a[href], .news-card a[href]"))
        content = soup.select_one("main, [role='main'], .newsroom, .content, .page-content, #content") or soup
        for selector in profile.article_selectors:
            try:
                for container in content.select(selector):
                    if container.name == "a" and container.get("href") and container not in candidates:
                        candidates.append(container)
                    if container.name == "a" and container.get("href"):
                        profile_card_links.add(id(container))
                    for link in container.select("a[href]"):
                        if link not in candidates:
                            candidates.append(link)
            except Exception:
                logger.warning("Invalid article selector for {}: {}", base_url, selector)

        for link in candidates:
            href = normalize_crawl_url(base_url, link.get("href"))
            if not href or href in seen:
                continue
            parsed = urlparse(href)
            if parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
                continue
            if not self._is_news_article_url(base_url, href, profile) or self._is_navigation_link(link):
                continue

            container = link if id(link) in profile_card_links else self._article_container(link)
            local_soup = BeautifulSoup(str(container), "lxml")
            date_evidence = self.extract_listing_publication_date(local_soup, profile)
            if not self._looks_like_article(link, href, container, date_evidence.published_at):
                continue

            title = self._first_text(container, [".news-title", ".article-title", ".title", "h1", "h2", "h3", "h4"]) or normalize_text(link.get_text(" ", strip=True))
            summary = self._first_text(container, [".news-text", ".preview", ".summary", ".description", "p"])
            if not title or len(title) < 4:
                continue

            seen.add(href)
            items.append({
                "url": href,
                "title": title,
                "summary": summary,
                "published_at": date_evidence.published_at,
                "published_at_source": "listing_card" if date_evidence.status == "verified" else date_evidence.source,
                "published_at_evidence": date_evidence.evidence,
                "publication_date_status": date_evidence.status,
            })
        return items

    def _article_container(self, link) -> object:
        if self._is_semantic_article_card(link):
            return link
        container = link
        for _ in range(8):
            parent = container.parent
            if not parent or parent.name in {"body", "html"}:
                break
            container = parent
            if self._is_semantic_article_card(container):
                break
        return container

    @staticmethod
    def _is_semantic_article_card(element) -> bool:
        classes = {str(value).lower() for value in (element.get("class") or [])}
        semantic_class = any(
            value in {"article", "news-item", "article-item", "news-list-item", "post", "card", "tile", "teaser", "teaser_item"}
            or value.endswith(("-card", "__card", "-item", "__item", "-teaser", "__teaser"))
            for value in classes
        )
        return element.name == "article" or semantic_class

    def _is_navigation_link(self, link) -> bool:
        if self._is_semantic_article_card(link):
            return False
        if link.find_parent(["header", "footer", "nav", "aside"]):
            return True
        for parent in link.parents:
            if parent.name == "article":
                return False
            classes = {str(value).lower() for value in (parent.get("class") or [])}
            if classes & {"pagination", "breadcrumb", "breadcrumbs", "menu", "footer", "header", "sidebar"}:
                return True
        return False

    def _looks_like_article(self, link, href: str, container, published_at: datetime | None) -> bool:
        path = urlparse(href).path.lower()
        if any(part in path for part in ("/category/", "/author/", "/tag/", "/page/")):
            return False
        label = normalize_text(link.get_text(" ", strip=True)).lower()
        heading = self._first_text(container, [".news-title", ".title", "h1", "h2", "h3", "h4"]).lower()
        if container.name == "article" and label and heading and label != heading and label not in NEWS_CTA_LABELS:
            return False
        if published_at:
            return True
        classes = " ".join(container.get("class") or []).lower()
        if container.name == "article" or any(token in classes for token in ["news", "article", "post", "card", "tile", "teaser"]):
            return True
        return any(hint in path for hint in ARTICLE_PATH_HINTS) and label not in {"news", "product news", *NEWS_CTA_LABELS}

    def extract_feed_links(self, base_url: str, soup: BeautifulSoup) -> list[str]:
        feeds: list[str] = []
        for link in soup.find_all("link", href=True):
            if "rss" in (link.get("type") or "").lower() or "atom" in (link.get("type") or "").lower():
                feed_url = normalize_crawl_url(base_url, link.get("href"))
                if feed_url:
                    feeds.append(feed_url)
        return feeds

    def _make_result(
        self,
        competitor: CompetitorConfig,
        source_type: str,
        url: str,
        title: str,
        text: str,
        html: str,
        soup: BeautifulSoup,
        status: str,
        fallback_date: PublicationDateEvidence | None = None,
    ) -> CrawlResult:
        snapshot = self.save_snapshot(competitor.code, source_type, url, html)
        clean_text = self.extract_main_text(soup) or text
        date_evidence = self.extract_publication_date(soup, competitor.news_profile) if source_type == "news" else PublicationDateEvidence()
        if source_type == "news" and date_evidence.status != "verified" and fallback_date and fallback_date.status == "verified":
            date_evidence = fallback_date
        return CrawlResult(
            competitor_code=competitor.code,
            competitor_name=competitor.name,
            source_type=source_type,
            url=url,
            title=title,
            text=clean_text,
            html=html,
            published_at=date_evidence.published_at,
            published_at_source=date_evidence.source,
            published_at_evidence=date_evidence.evidence,
            publication_date_status=date_evidence.status,
            content_kind="article" if source_type != "news" or self._is_news_article_url("", url, competitor.news_profile) else "unknown",
            discovered_at=utcnow_naive(),
            checked_at=utcnow_naive(),
            status=status,
            local_snapshot_path=str(snapshot),
            attachment_links=self.extract_attachments(url, soup),
        )

    def extract_published_at(self, soup: BeautifulSoup) -> datetime | None:
        evidence = self.extract_publication_date(soup)
        return evidence.published_at if evidence.status == "verified" else None

    def extract_publication_date(
        self,
        soup: BeautifulSoup,
        profile: NewsExtractionProfile | None = None,
    ) -> PublicationDateEvidence:
        """Return only an unambiguous publication date from a scoped source."""
        profile = profile or NewsExtractionProfile()
        article_scope = self._article_scope(soup)
        for source, candidates in (
            ("json_ld", self._json_ld_date_candidates(soup)),
            ("metadata", self._date_candidates_by_selectors(soup, GLOBAL_DATE_SELECTORS)),
        ):
            evidence = self._evidence_from_candidates(source, candidates)
            if evidence.status != "missing":
                return evidence

        selector_evidence = self._first_selector_evidence(article_scope, [*profile.date_selectors, *DATE_SELECTORS])
        if selector_evidence.status != "missing":
            return selector_evidence

        contextual_evidence = self._contextual_date_evidence(article_scope)
        if contextual_evidence.status != "missing":
            return contextual_evidence

        evidence = self._evidence_from_candidates("article_time", self._time_date_candidates(article_scope))
        if evidence.status != "missing":
            return evidence
        return PublicationDateEvidence()

    def extract_listing_publication_date(
        self,
        soup: BeautifulSoup,
        profile: NewsExtractionProfile | None = None,
    ) -> PublicationDateEvidence:
        """Extract a date only from the card that owns the selected article URL."""
        profile = profile or NewsExtractionProfile()
        evidence = self.extract_publication_date(soup, profile)
        if evidence.status != "missing":
            return evidence
        return self._first_selector_evidence(soup, profile.listing_date_selectors)

    def _article_scope(self, soup: BeautifulSoup) -> BeautifulSoup:
        for selector in ARTICLE_DATE_CONTAINERS:
            container = soup.select_one(selector)
            if container:
                return BeautifulSoup(str(container), "lxml")
        return soup

    def _json_ld_date_candidates(self, soup: BeautifulSoup) -> list[tuple[datetime, str]]:
        candidates: list[tuple[datetime, str]] = []
        for script in soup.select("script[type='application/ld+json']"):
            try:
                payload = json.loads(script.get_text())
            except (TypeError, ValueError):
                continue
            for item in self._walk_json_ld(payload):
                raw_type = item.get("@type", "")
                types = {str(value).lower() for value in (raw_type if isinstance(raw_type, list) else [raw_type])}
                if not types.intersection(ARTICLE_SCHEMA_TYPES):
                    continue
                raw_date = item.get("datePublished")
                parsed = parse_datetime_or_none(str(raw_date)) if raw_date else None
                if parsed:
                    candidates.append((parsed, f"datePublished={raw_date}"))
        return candidates

    def _walk_json_ld(self, value: object):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from self._walk_json_ld(child)
        elif isinstance(value, list):
            for child in value:
                yield from self._walk_json_ld(child)

    def _time_date_candidates(self, soup: BeautifulSoup) -> list[tuple[datetime, str]]:
        candidates: list[tuple[datetime, str]] = []
        for element in soup.select("time"):
            raw = element.get("datetime") or element.get_text(" ", strip=True)
            parsed = parse_datetime_or_none(raw)
            if parsed:
                candidates.append((parsed, f"time={raw}"))
        return candidates

    def _first_selector_evidence(self, soup: BeautifulSoup, selectors: list[str]) -> PublicationDateEvidence:
        """Use selectors as an ordered site profile, not as one mixed pool.

        A page can contain related-news cards with their own dates.  Combining
        all configured selectors made a valid article date look ambiguous.
        """
        seen: set[str] = set()
        for selector in selectors:
            if selector in seen:
                continue
            seen.add(selector)
            evidence = self._evidence_from_candidates(
                "article_selector",
                self._date_candidates_by_selectors(soup, [selector]),
            )
            if evidence.status != "missing":
                return evidence
        return PublicationDateEvidence()

    def _contextual_date_evidence(self, soup: BeautifulSoup) -> PublicationDateEvidence:
        candidates_by_source: dict[str, list[tuple[datetime, str]]] = {}
        strings = [normalize_text(str(value)) for value in soup.stripped_strings]
        for index, text in enumerate(strings):
            lowered = text.lower()
            combined = text
            if index + 1 < len(strings):
                combined = f"{text} {strings[index + 1]}"
            for source, labels in CONTEXTUAL_DATE_LABELS:
                if not any(label in lowered for label in labels):
                    continue
                parsed = parse_datetime_or_none(combined)
                if parsed:
                    candidates_by_source.setdefault(source, []).append((parsed, combined))
        for source, _ in CONTEXTUAL_DATE_LABELS:
            evidence = self._evidence_from_candidates(source, candidates_by_source.get(source, []))
            if evidence.status != "missing":
                return evidence
        return PublicationDateEvidence()

    def _date_candidates_by_selectors(self, soup: BeautifulSoup, selectors: list[str]) -> list[tuple[datetime, str]]:
        candidates: list[tuple[datetime, str]] = []
        for selector in selectors:
            try:
                elements = soup.select(selector)
            except Exception:
                continue
            for element in elements:
                raw = element.get("datetime") or element.get("content") or element.get_text(" ", strip=True)
                parsed = parse_datetime_or_none(raw)
                if parsed:
                    candidates.append((parsed, f"{selector}={raw}"))
        return candidates

    def _evidence_from_candidates(self, source: str, candidates: list[tuple[datetime, str]]) -> PublicationDateEvidence:
        unique: dict[str, str] = {}
        for value, raw in candidates:
            unique.setdefault(value.date().isoformat(), raw)
        if not unique:
            return PublicationDateEvidence()
        if len(unique) > 1:
            return PublicationDateEvidence(source=source, evidence="; ".join(unique.values()), status="ambiguous")
        date_text, raw = next(iter(unique.items()))
        return PublicationDateEvidence(published_at=datetime.fromisoformat(date_text), source=source, evidence=raw, status="verified")

    def _extract_date_by_selectors(self, soup: BeautifulSoup, selectors: list[str]) -> datetime | None:
        candidates = self._date_candidates_by_selectors(soup, selectors)
        return candidates[0][0] if candidates else None

    def _is_news_article_url(self, section_url: str, url: str, profile: NewsExtractionProfile | None = None) -> bool:
        profile = profile or NewsExtractionProfile()
        parsed = urlparse(url)
        path = parsed.path.strip("/").lower()
        section_path = urlparse(section_url).path.strip("/").lower() if section_url else ""
        listing_paths = {item.strip("/").lower() for item in LISTING_PATHS}
        if not path or path in listing_paths or (section_path and path == section_path):
            return False
        path_parts = {part for part in path.split("/") if part}
        if path_parts.intersection(UTILITY_PATH_PARTS):
            return False
        normalized = url.lower()
        if any(pattern.lower() in normalized for pattern in profile.exclude_url_patterns):
            return False
        return True

    def extract_main_text(self, soup: BeautifulSoup) -> str:
        for selector in MAIN_CONTENT_SELECTORS:
            candidates = []
            for element in soup.select(selector):
                clone = BeautifulSoup(str(element), "lxml")
                for noise in clone.select(", ".join(NOISE_SELECTORS)):
                    noise.decompose()
                text = normalize_text(clone.get_text(" ", strip=True))
                if text:
                    candidates.append(text)
            if candidates:
                return max(candidates, key=len)

        clone = BeautifulSoup(str(soup), "lxml")
        for noise in clone.select(", ".join(NOISE_SELECTORS)):
            noise.decompose()
        return normalize_text(clone.get_text(" ", strip=True))

    def extract_attachments(self, base_url: str, soup: BeautifulSoup) -> list[str]:
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = normalize_crawl_url(base_url, a.get("href"), allowed_file_extensions=ATTACHMENT_EXTENSIONS)
            if not href:
                continue
            if href.lower().split("?")[0].endswith(ATTACHMENT_EXTENSIONS):
                links.append(href)
        return sorted(set(links))

    def _configured_section_urls(self, competitor: CompetitorConfig) -> dict[str, set[str]]:
        sections = competitor.sections
        return {
            "news": set(sections.news),
            "products": set(sections.products),
            "promotions": set(sections.promotions),
            "docs": set(sections.docs),
        }

    def _merge_section_urls(self, configured: dict[str, set[str]], discovered: dict[str, set[str]]) -> dict[str, set[str]]:
        merged: dict[str, set[str]] = {}
        for source_type in ["news", "promotions", "docs", "products"]:
            merged[source_type] = set(configured.get(source_type, set())) | set(discovered.get(source_type, set()))
        return merged

    def _ordered_section_urls(self, urls: set[str], source_type: str) -> list[str]:
        def priority(url: str) -> tuple[int, str]:
            path = urlparse(url).path.lower().rstrip("/")
            if source_type == "products":
                if path.startswith("/new-products/") and path != "/new-products":
                    return (0, url)
                if "/catalog/product/" in path:
                    return (1, url)
                if path == "/new-products":
                    return (2, url)
                if path == "/catalog":
                    return (4, url)
                if path.startswith("/catalog/"):
                    return (5, url)
            return (3, url)

        return sorted(set(urls), key=priority)

    def _first_text(self, soup: BeautifulSoup, selectors: list[str]) -> str:
        for selector in selectors:
            element = soup.select_one(selector)
            if element:
                value = normalize_text(element.get_text(" ", strip=True))
                if value:
                    return value
        return ""

    def _is_listing_url(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()
        return path in LISTING_PATHS or path.rstrip("/") in {item.rstrip("/") for item in LISTING_PATHS}

    def _in_period(self, value: datetime, period: ReportPeriod) -> bool:
        return period.start <= value.replace(tzinfo=None) <= period.end

    def _budget_exhausted(self, competitor_pages_seen: int, page_limit: int | None = None) -> bool:
        limit = page_limit if page_limit is not None else self.settings.max_pages_per_competitor
        if competitor_pages_seen >= limit:
            return True
        if self._total_pages_seen >= self.settings.max_total_pages:
            return True
        return False
