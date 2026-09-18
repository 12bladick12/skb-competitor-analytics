from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from .models import CrawlResult
from .utils import normalize_text, utcnow_naive
from .content_extraction import telegram_content

LOCAL = ZoneInfo('Asia/Yekaterinburg')


def post_time(raw: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if value.tzinfo is None:
            return None
        return value.astimezone(LOCAL).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def collect_web(crawler, competitor, period, resume_url=''):
    channel = competitor.telegram.url.rstrip('/').rsplit('/', 1)[-1]
    current = f'https://t.me/s/{channel}'
    results, visited, post_ids = [], set(), set()
    crawler.coverage = {'status': 'partial', 'reason': 'page_limit', 'cursor': current, 'pages': 0}
    for _ in range(crawler.settings.telegram_max_pages):
        if current in visited:
            crawler.coverage.update(reason='pagination_loop', cursor=current)
            break
        visited.add(current)
        html, status = '', ''
        for candidate in crawler._web_candidates(current):
            html, _, status = crawler.fetch(candidate, timeout_seconds=crawler.settings.telegram_web_timeout_seconds)
            if html:
                _, _, candidate_soup = crawler.parse_html(html)
                if candidate_soup.select('.tgme_widget_message'):
                    current = candidate
                    break
                html = ''
        if not html:
            crawler.coverage.update(status='partial' if results else 'error', reason=status or 'no_channel_messages', cursor=current)
            break
        _, _, soup = crawler.parse_html(html)
        crawler.coverage['pages'] += 1
        times = []
        invalid = False
        for message in soup.select('.tgme_widget_message'):
            ident = str(message.get('data-post') or '')
            if not ident.casefold().startswith(channel.casefold() + '/') or not ident.rsplit('/', 1)[-1].isdigit():
                invalid = True
                continue
            stamp = message.select_one('time[datetime]')
            raw = stamp.get('datetime') if stamp else ''
            published = post_time(raw)
            if published is None:
                invalid = True
                continue
            times.append(published)
            if not period.start <= published <= period.end or ident in post_ids:
                continue
            post_ids.add(ident)
            body = message.select_one('.tgme_widget_message_text')
            title,text,title_basis,full_text = telegram_content(body,ident.rsplit('/',1)[-1])
            # Media-only posts are real publications; describe only observable
            # media, never infer what is shown in an image/video.
            if not full_text:
                if message.select_one('.tgme_widget_message_video_player, .tgme_widget_message_photo_wrap'):
                    text = 'Публикация с медиа без текстового описания.'
                else:
                    invalid = True
                    continue
            url = f'https://t.me/{ident}'
            snapshot = str(crawler.save_snapshot(competitor.code, 'telegram', url, str(message)))
            results.append(CrawlResult(
                competitor_code=competitor.code, competitor_name=competitor.name,
                source_type='telegram', url=url, title=title, text=text,full_text=full_text,title_basis=title_basis,
                html=str(message), published_at=published, published_at_source='telegram_timestamp',
                published_at_evidence=str(raw), publication_date_status='verified',
                discovered_at=utcnow_naive(), checked_at=utcnow_naive(), status='ok',
                local_snapshot_path=snapshot,
            ))
        older = soup.select_one('a.tme_messages_more[href], a[data-before][href]')
        if older is None:
            older = next((a for a in soup.select('a[href]') if 'before=' in a['href']), None)
        # Always traverse the current archive contiguously: a saved cursor can
        # skip posts published between the previous attempt and this one.
        next_url = urljoin(current, older['href']) if older else ''
        resume_url = ''
        if invalid:
            crawler.coverage.update(reason='unparseable_messages', cursor=current)
            break
        if (times and min(times) < period.start) or not next_url:
            crawler.coverage.update(status='success', reason='', cursor='')
            break
        if urlparse(next_url).hostname not in {'t.me', 'telegram.me'}:
            crawler.coverage.update(reason='invalid_pagination_host', cursor=current)
            break
        current = next_url
        crawler.coverage['cursor'] = current
        crawler.sleep()
    return results
