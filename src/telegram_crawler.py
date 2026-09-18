from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .crawler_base import CrawlerBase
from .logger import logger
from .models import AppSettings, CompetitorConfig, CrawlResult, ReportPeriod
from .utils import join_url, normalize_text, parse_datetime_or_none, utcnow_naive


class TelegramCrawler(CrawlerBase):
    def __init__(self, settings: AppSettings):
        super().__init__(settings)

    def crawl(self, competitor: CompetitorConfig, period: ReportPeriod, resume_url: str = '') -> list[CrawlResult]:
        from .telegram_history import collect_web
        self.coverage = {'status': 'not_configured', 'reason': '', 'cursor': ''}
        if not competitor.telegram or not competitor.telegram.enabled:
            return []
        results = collect_web(self, competitor, period, resume_url)
        if self.coverage['status'] == 'success' or not self.settings.telegram_use_telethon:
            return results
        try:
            fallback = self.crawl_with_telethon(competitor, period)
            if getattr(self, 'api_complete', False):
                self.coverage.update(status='success', reason='', cursor='')
                return list({r.url: r for r in [*results, *fallback]}.values())
        except Exception as exc:
            logger.warning('Telegram API fallback unavailable: {}', type(exc).__name__)
            self.coverage['reason'] += '; api_unavailable:' + type(exc).__name__
        return results

    def crawl_web(self, competitor: CompetitorConfig, period: ReportPeriod) -> list[CrawlResult]:
        from .telegram_history import collect_web
        return collect_web(self, competitor, period)

    @staticmethod
    def _web_candidates(web_url: str) -> list[str]:
        candidates = [web_url]
        if "//t.me/" in web_url:
            candidates.append(web_url.replace("//t.me/", "//telegram.me/"))
        elif "//telegram.me/" in web_url:
            candidates.append(web_url.replace("//telegram.me/", "//t.me/"))
        return list(dict.fromkeys(candidates))

    def crawl_with_telethon(self, competitor: CompetitorConfig, period: ReportPeriod) -> list[CrawlResult]:
        self.api_complete = False
        if not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
            logger.warning("Telethon is enabled but api_id/api_hash are empty")
            return []
        try:
            from telethon.sync import TelegramClient
        except Exception as exc:
            logger.warning("Telethon import failed: {}", exc)
            return []

        channel = self._channel_name(competitor.telegram.url)
        results: list[CrawlResult] = []
        proxy = self._telethon_proxy()
        session_path = Path(self.settings.telegram_session_name)
        if not session_path.is_absolute():
            session_path = self.settings.root_dir / session_path
        client = TelegramClient(
            str(session_path),
            int(self.settings.telegram_api_id),
            self.settings.telegram_api_hash,
            proxy=proxy,
            timeout=10,
            request_retries=2,
            connection_retries=2,
        )
        try:
            client.connect()
            if not client.is_user_authorized():
                raise RuntimeError(
                    f"Telegram session is not authorized: {session_path.name}. "
                    "Authorize it once before running unattended collection."
                )
            from datetime import timezone
            from .telegram_history import LOCAL
            from telethon.tl.types import Channel
            entity = client.get_entity(channel)
            if not isinstance(entity, Channel) or not entity.broadcast:
                raise RuntimeError('Configured Telegram source is not a broadcast channel')
            for msg in client.iter_messages(entity, offset_date=period.end.replace(tzinfo=LOCAL).astimezone(timezone.utc)):
                from .telegram_history import LOCAL
                published = msg.date.astimezone(LOCAL).replace(tzinfo=None)
                if published < period.start:
                    break
                if not msg.message:
                    continue
                url = f"https://t.me/{channel}/{msg.id}"
                from html import escape
                from .content_extraction import telegram_content
                message_html='<div class="tgme_widget_message_text">'+escape(msg.message).replace('\n','<br/>')+'</div>'
                title,text,title_basis,full_text=telegram_content(BeautifulSoup(message_html,'lxml'),str(msg.id))
                results.append(
                    CrawlResult(
                        competitor_code=competitor.code,
                        competitor_name=competitor.name,
                        source_type="telegram",
                        url=url,
                        title=title,
                        text=text,full_text=full_text,title_basis=title_basis,
                        html=message_html,
                        published_at=published,
                        published_at_source='telegram_api',
                        published_at_evidence=msg.date.isoformat(),
                        publication_date_status='verified',
                        local_snapshot_path=str(self.save_snapshot(competitor.code, 'telegram', url, message_html)),
                        discovered_at=utcnow_naive(),
                        checked_at=utcnow_naive(),
                        status="ok_telethon",
                    )
                )
        finally:
            client.disconnect()
        self.api_complete = True
        return results

    def _channel_name(self, url: str) -> str:
        match = re.search(r"t\.me/([^/?#]+)", url)
        return match.group(1) if match else url.strip("@")

    def _telethon_proxy(self):
        from .network import telegram_proxy
        return telegram_proxy(self.settings)

