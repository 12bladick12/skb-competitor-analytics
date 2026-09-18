from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl


class TelegramConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    web_url: str = ""


class CompetitorSections(BaseModel):
    news: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    promotions: list[str] = Field(default_factory=list)
    docs: list[str] = Field(default_factory=list)


class NewsExtractionProfile(BaseModel):
    """Site-specific safeguards for collecting news articles."""

    article_selectors: list[str] = Field(default_factory=list)
    date_selectors: list[str] = Field(default_factory=list)
    listing_date_selectors: list[str] = Field(default_factory=list)
    exclude_url_patterns: list[str] = Field(default_factory=list)


class CompetitorConfig(BaseModel):
    name: str
    code: str
    base_url: str = ""
    sections: CompetitorSections = Field(default_factory=CompetitorSections)
    discover_sections: bool = True
    crawl_mode: Literal["http", "playwright"] = "http"
    news_profile: NewsExtractionProfile = Field(default_factory=NewsExtractionProfile)
    telegram: TelegramConfig | None = None
    monitor_sources: list[dict[str, Any]] = Field(default_factory=list)


class AppSettings(BaseModel):
    root_dir: Path
    data_dir: Path
    raw_dir: Path
    processed_dir: Path
    snapshots_dir: Path
    reports_dir: Path
    logs_dir: Path
    database: Path
    user_agent: str
    timeout_seconds: int = 25
    request_delay_seconds: float = 1.5
    max_pages_per_section: int = 30
    max_pages_per_competitor: int = 40
    max_total_pages: int = 120
    max_discovered_sections_per_type: int = 5
    use_playwright_fallback: bool = True
    use_system_proxy: bool = False
    proxy_url: str = ""
    proxy_mode: Literal['legacy', 'system', 'explicit', 'direct'] = 'legacy'
    allow_direct_fallback: bool = True
    max_archive_pages: int = 30
    telegram_max_pages: int = 100
    translation_enabled: bool = True
    telegram_use_telethon: bool = False
    telegram_api_id: str = ""
    telegram_api_hash: str = ""
    telegram_session_name: str = "teko_monitor"
    telegram_web_timeout_seconds: int = 8
    report_primary_color: str = "7A1F2B"
    report_table_fill: str = "F3E6E9"
    report_muted_color: str = "666666"
    report_font_name: str = "Arial"


class CrawlResult(BaseModel):
    competitor_code: str
    competitor_name: str
    source_type: str
    url: str
    title: str = ""
    text: str = ""
    full_text: str = ""
    title_basis: str = ""
    html: str = ""
    published_at: datetime | None = None
    published_at_source: str = "missing"
    published_at_evidence: str = ""
    publication_date_status: Literal["verified", "ambiguous", "missing"] = "missing"
    content_kind: Literal["article", "listing", "utility", "unknown"] = "article"
    discovered_at: datetime
    checked_at: datetime
    status: str = "ok"
    local_snapshot_path: str = ""
    attachment_links: list[str] = Field(default_factory=list)
    product_mentions: list[str] = Field(default_factory=list)


class ProductCandidate(BaseModel):
    competitor_code: str
    competitor_name: str
    name: str
    marking: str = ""
    product_type: str = ""
    url: str
    product_url: str = ""
    site_added_at: datetime | None = None
    site_added_evidence_url: str = ""
    evidence_type: str = "unknown_date"
    first_seen_at: datetime
    last_seen_at: datetime
    characteristics: dict[str, Any] = Field(default_factory=dict)
    evidence_page_id: int | None = None
    status: str = "active"


class ClassificationResult(BaseModel):
    category: str
    confidence: float
    matched_keywords: list[str] = Field(default_factory=list)
    short_summary: str = ""


class ReportPeriod(BaseModel):
    year: int
    month: int | None = None
    start: datetime
    end: datetime
    label: str
    granularity: str = "month"


class SiteActivityTarget(BaseModel):
    """One explicitly approved part of a competitor site to check locally."""

    competitor_code: str
    source_type: Literal["news", "products", "promotions", "docs"]
    url: str
    title: str = ""
    include_filters: list[str] = Field(default_factory=list)
    subtractive_selectors: list[str] = Field(default_factory=list)
    content_keywords: list[str] = Field(default_factory=list)
    crawl_mode: Literal["http", "playwright"] = "http"


class SiteActivityConfig(BaseModel):
    """Configuration for direct, local site snapshot checks."""

    targets: list[SiteActivityTarget] = Field(default_factory=list)
