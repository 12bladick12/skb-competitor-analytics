from __future__ import annotations

from pathlib import Path

import yaml

from .models import AppSettings, CompetitorConfig, SiteActivityConfig


def load_competitors(path: Path) -> list[CompetitorConfig]:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return [CompetitorConfig(**item) for item in raw.get("competitors", [])]


def load_settings(path: Path, root_dir: Path) -> AppSettings:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    paths = raw.get("paths", {})
    http = raw.get("http", {})
    telegram = raw.get("telegram", {})
    report = raw.get("report", {})

    def p(key: str, default: str) -> Path:
        value = Path(paths.get(key, default))
        return value if value.is_absolute() else root_dir / value

    return AppSettings(
        root_dir=root_dir,
        data_dir=p("data_dir", "data"),
        raw_dir=p("raw_dir", "data/raw"),
        processed_dir=p("processed_dir", "data/processed"),
        snapshots_dir=p("snapshots_dir", "data/snapshots"),
        reports_dir=p("reports_dir", "reports"),
        logs_dir=p("logs_dir", "logs"),
        database=p("database", "data/competitor_monitor.db"),
        user_agent=http.get("user_agent", "SKB-Induction-CompetitorMonitor/1.0"),
        timeout_seconds=int(http.get("timeout_seconds", 25)),
        request_delay_seconds=float(http.get("request_delay_seconds", 1.5)),
        max_pages_per_section=int(http.get("max_pages_per_section", 30)),
        max_pages_per_competitor=int(http.get("max_pages_per_competitor", 40)),
        max_total_pages=int(http.get("max_total_pages", 120)),
        max_discovered_sections_per_type=int(http.get("max_discovered_sections_per_type", 5)),
        use_playwright_fallback=bool(http.get("use_playwright_fallback", True)),
        use_system_proxy=bool(http.get("use_system_proxy", False)),
        proxy_url=str(http.get("proxy_url", "")),
        proxy_mode=http.get('proxy_mode', 'legacy'),
        allow_direct_fallback=bool(http.get('allow_direct_fallback', True)),
        max_archive_pages=int(http.get('max_archive_pages', 30)),
        telegram_max_pages=int(telegram.get('max_pages', 100)),
        translation_enabled=bool(report.get('translation_enabled', True)),
        telegram_use_telethon=bool(telegram.get("use_telethon", False)),
        telegram_api_id=str(telegram.get("api_id", "")),
        telegram_api_hash=str(telegram.get("api_hash", "")),
        telegram_session_name=str(telegram.get("session_name", "teko_monitor")),
        telegram_web_timeout_seconds=int(telegram.get("web_timeout_seconds", 8)),
        report_primary_color=str(report.get("primary_color", "7A1F2B")),
        report_table_fill=str(report.get("table_fill", "F3E6E9")),
        report_muted_color=str(report.get("muted_color", "666666")),
        report_font_name=str(report.get("font_name", "Arial")),
    )


def load_site_activity_config(path: Path) -> SiteActivityConfig:
    """Load only explicitly curated watches; never infer watches from old crawl rows."""
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if raw.get('registry'):
        from .models import SiteActivityTarget
        from .verified_monitor import sources_for
        competitors = load_competitors(path.parent / raw['registry'])
        return SiteActivityConfig(targets=[SiteActivityTarget(
            competitor_code=c.code, source_type=s['kind'], url=s['url'], title=c.name,
            crawl_mode=s.get('crawl_mode',c.crawl_mode)) for c in competitors for s in sources_for(c)])
    return SiteActivityConfig(**raw.get("site_activity", raw))
