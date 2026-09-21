"""Shared collection, proof validation and recovery used by every entry point."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .briefs import article_body, brief
from .evidence_store import EvidenceStore, canonical_url, now
from .logger import logger
from .site_crawler import SiteCrawler, PublicationDateEvidence
from .telegram_crawler import TelegramCrawler
from .telegram_history import post_time
from .utils import normalize_text


def sources_for(competitor):
    if competitor.monitor_sources:
        return competitor.monitor_sources
    return [dict(kind=kind, url=url, mode='articles', crawl_mode=competitor.crawl_mode,
                 official_evidence_url=competitor.base_url)
            for kind in ('news', 'products', 'promotions') for url in getattr(competitor.sections, kind)]


def next_pages(soup, current):
    found = []
    for a in soup.select('a[href]'):
        href = urljoin(current, a['href'])
        if urlsplit(href).netloc != urlsplit(current).netloc:
            continue
        path = urlsplit(href).path
        if (re.search(r'(?:[?&](?:page|pageIndex|pagen_\d+)=\d+|/page/\d+/?)', href, re.I)
                or 'next' in (a.get('rel') or [])):
            if path.rstrip('/') == urlsplit(current).path.rstrip('/') or '/page/' in path:
                found.append(href)
    return list(dict.fromkeys(found))


def is_product_announcement(title, text=''):
    subject = (title + ' ' + text[:200]).casefold()
    product = r'(?:датчик\w*|преобразовател\w*|кнопк\w*|серия|серии|sensor\w*|encoder\w*|controller\w*|series|flow meter\w*|code.read\w*)'
    return bool(
        re.search(r'(?:\bnew\b|\bupgraded\b|\bнов(?:ый|ая|ые|ую|ых)\b)\s+(?:[\w-]+\s+){0,4}'+product,subject)
        or re.search(r'\b(?:launch(?:es|ed)?|introduc(?:es|ed|ing)|unveils)\s+(?:[\w-]+\s+){0,6}'+product,subject)
        or re.search(r'\b(?:portfolio|lineup|series|range)\b.{0,45}\b(?:expanded|additional|upgraded|added)\b',subject)
        or re.search(r'(?:расшир\w*|обнов\w*|пополн\w*)\s+(?:[\w-]+\s+){0,2}(?:линейк\w*|ассортимент\w*|сери\w*)',subject)
        or (re.search(r'\bновинки\b',subject) and re.search(product,subject)))


def announced_month(value):
    months=['январ','феврал','март','апрел','ма[йя]','июн','июл','август','сентябр','октябр','ноябр','декабр']
    found=[]
    for month,word in enumerate(months,1):
        for match in re.finditer(r'\b'+word+r'[а-я]*\s+(20\d{2})\b',value,re.I):
            found.append((datetime(int(match[1]),month,1),match[0]))
    return found[0] if len({x[0] for x in found})==1 else (None,'')


class VerifiedMonitor:
    def __init__(self, storage, settings, competitors):
        self.storage, self.settings = storage, settings
        self.competitors = competitors
        self.store = EvidenceStore(storage)
        self.crawler = SiteCrawler(settings)
        self.telegram = TelegramCrawler(settings)
        self.fetched = {}
        self.historical_listings = {}

    def close(self):
        self.crawler.close()
        self.telegram.close()

    def snapshot(self, code, url, html):
        digest = hashlib.sha256((url + '\n' + html).encode()).hexdigest()
        path = self.settings.snapshots_dir / code / 'evidence' / (digest + '.html')
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(html, encoding='utf-8')
        return str(path.resolve())

    def fetch(self, url, mode='http'):
        key = (canonical_url(url), mode)
        if key not in self.fetched:
            self.crawler.sleep()
            html, code, status = self.crawler.fetch(url, crawl_mode=mode)
            final = self.crawler.last_url or url
            if html and urlsplit(final).netloc != urlsplit(url).netloc:
                html, status = '', 'error: unexpected_redirect_host'
            self.fetched[key] = (html, code, status)
        return self.fetched[key]

    def cards(self, soup, source, competitor):
        selector = source.get('card_selector')
        if not selector:
            return self.crawler.extract_listing_items(source['url'], soup, competitor.news_profile)
        result, seen = [], set()
        for card in soup.select(selector):
            link = card if card.name == 'a' and card.get('href') else card.select_one(source.get('link_selector', 'h3 a[href], h2 a[href], a[href]'))
            if link is None:
                continue
            url = urljoin(source['url'], link['href'])
            if url in seen or urlsplit(url).netloc != urlsplit(source['url']).netloc or url == canonical_url(source['url']):
                continue
            title_node = card.select_one(source.get('title_selector', '.news-products-title,.products-view-name,h2,h3,h4,.title'))
            title = normalize_text((title_node or link).get_text(' ', strip=True))
            if len(title) < 4:
                continue
            local = BeautifulSoup(str(card), 'lxml')
            date = self.crawler.extract_listing_publication_date(local, competitor.news_profile)
            summary_node = card.select_one('.item-detail,.news-products-text,.products-view-description,p,.description')
            result.append(dict(url=url, title=title, summary=normalize_text(summary_node.get_text(' ', strip=True)) if summary_node else '',
                               published_at=date.published_at, published_at_source=date.source,
                               published_at_evidence=date.evidence, publication_date_status=date.status,
                               card_text=normalize_text(card.get_text(' ', strip=True))))
            seen.add(url)
        return result

    def article(self, competitor, source, url, html, listing=None, listing_path='', archived=False):
        _, _, soup = self.crawler.parse_html(html)
        heading = soup.select_one(source.get('heading_selector', 'h1'))
        title = normalize_text(heading.get_text(' ', strip=True)) if heading else ''
        generic = {'news', 'новости', 'press releases', 'sick | sensor intelligence', 'company news | autonics global'}
        if not title or title.casefold() in generic:
            title = str((listing or {}).get('title') or '')
        text = article_body(soup, source.get('body_selectors', []), title=title)
        evidence = self.crawler.extract_publication_date(soup, competitor.news_profile)
        fallback = listing or {}
        published = evidence.published_at
        date_evidence = evidence.evidence
        basis = 'publication_date'
        reason = ''
        if evidence.status == 'ambiguous':
            reason = 'conflicting_publication_dates'
        elif evidence.status != 'verified':
            if fallback.get('publication_date_status') == 'verified' and listing_path:
                published = fallback.get('published_at')
                date_evidence = str(fallback.get('published_at_evidence') or '')
            else:
                reason = 'publication_date_missing'
        elif fallback.get('publication_date_status') == 'verified' and fallback.get('published_at') and published.date() != fallback['published_at'].date():
            reason = 'article_listing_date_conflict'
        if reason=='publication_date_missing' and source['kind']=='products' and listing_path:
            month, raw_month=announced_month(str(fallback.get('title',''))+' '+str(fallback.get('summary','')))
            if month:
                published,date_evidence,basis,reason=month,raw_month,'announcement_month',''
        if not text or len(text) < 40 or not title or title.casefold() in generic:
            reason = 'article_content_missing'
        if self.crawler.invalid_content(html):
            reason = 'access_block'
        if fallback.get('publication_date_status')=='ambiguous':
            reason = 'conflicting_listing_dates'
        if published and published > datetime.now():
            reason = 'future_publication_date'
        if urlsplit(url).netloc not in {urlsplit(competitor.base_url).netloc,urlsplit(source['url']).netloc}:
            reason = 'unapproved_source_host'
        kind = source['kind']
        if kind == 'news' and is_product_announcement(title, text):
            kind = 'products'
        if kind == 'promotions':
            # A news card mentioning a sales department is not a promotion.
            if not source.get('curated_offers') and not re.search(r'скидк|распродаж|спецпредлож|\bdiscount\b|special offer|price reduction', title + ' ' + text[:500], re.I):
                reason = 'promotion_not_supported'
            kind = 'products'
        path = self.snapshot(competitor.code, url, html)
        item = dict(competitor_code=competitor.code, competitor_name=competitor.name, kind=kind,
                    url=url, title=title, summary=brief(text), original_text=text,
                    published_at=published.isoformat() if published else None, detected_at=now(), date_basis=basis,
                    status='rejected' if reason else 'confirmed', reason=reason,
                    evidence=dict(official_url=source.get('official_evidence_url', competitor.base_url),
                                  source_url=source['url'], article_url=url, date_evidence=date_evidence,
                                  snapshots=[path] + ([listing_path] if listing_path else []), archived=archived))
        ident, added = self.store.record(item)
        return ident, added, reason

    def collect_source(self, run_id, competitor, source, period):
        root = source['url']
        mode = source.get('crawl_mode', competitor.crawl_mode)
        queue, pending, seen, cards, proofs = [root], [], set(), {}, []
        completed, resume_context = set(), {}
        previous = self.store.conn.execute('''SELECT s.cursor FROM source_checks s JOIN collection_runs r ON r.id=s.run_id
            WHERE s.competitor_code=? AND s.url=? AND r.period_start=? ORDER BY s.id DESC LIMIT 1''',
            (competitor.code, source['url'], period.start.isoformat())).fetchone()
        if previous and previous[0]:
            try:
                resume = json.loads(previous[0]); queue.extend(resume.get('listings', [])); pending.extend(resume.get('articles', []))
                # Listings can shift as new publications arrive. Revisit them
                # on every pass; only pending article work is reusable.
                completed=set()
                # A catalogue comparison needs one complete current pass;
                # never assemble a supposedly full baseline from partial runs.
                if source.get('mode') in {'catalogue','announcements'}:
                    completed.clear()
                completed.discard(canonical_url(root))
                for key,value in resume.get('article_context',{}).items():
                    card,path=value
                    if card.get('published_at'):
                        card['published_at']=datetime.fromisoformat(card['published_at'])
                    resume_context[key]=(card,path)
            except (ValueError, TypeError):
                pass
        failures, reasons, count, requests_used = [], [], 0, 0
        last_soup = None
        while queue and len(seen) < self.settings.max_archive_pages:
            url = queue.pop(0)
            if canonical_url(url) in seen or canonical_url(url) in completed:
                continue
            seen.add(canonical_url(url))
            html, _, status = self.fetch(url, mode)
            if not html:
                reasons.append(status); failures.append(url)
                continue
            _, _, soup = self.crawler.parse_html(html)
            last_soup = soup
            path = self.snapshot(competitor.code, url, html)
            proofs.append(path)
            page_source={**source,'url':url}
            page_items = self.inline_cards(soup,page_source) if source.get('mode')=='inline' else self.cards(soup, page_source, competitor)
            categories = [urljoin(url,a['href']) for a in soup.select(source['category_selector'])] if source.get('category_selector') else []
            categories = [u for u in categories if re.search(r'/productnews/\d+/?$',urlsplit(u).path)]
            queue.extend(u for u in categories if canonical_url(u) not in seen|completed and u not in queue)
            if not page_items:
                if categories:
                    completed.add(canonical_url(url))
                    continue
                reasons.append('no_recognized_cards'); failures.append(url)
                continue
            for card in page_items:
                cards.setdefault(card['url'], (card, path))
            completed.add(canonical_url(url))
            queue.extend(p for p in next_pages(soup, url) if canonical_url(p) not in seen|completed and p not in queue)
            dates = [x.get('published_at') for x in page_items]
            if dates and all(dates) and dates == sorted(dates, reverse=True) and max(dates) < period.start:
                queue = []
        if queue:
            reasons.append('archive_page_limit')
        if source.get('mode')=='inline':
            for url,(card,path) in cards.items():
                published=card['published_at']
                if not published:
                    reasons.append('publication_date_missing')
                    continue
                if not period.start<=published<=period.end:
                    continue
                _,fresh=self.store.record(dict(competitor_code=competitor.code,competitor_name=competitor.name,
                    kind='products',url=url,title=card['title'],summary=brief(card['summary']),original_text=card['summary'],
                    published_at=published.isoformat(),date_basis='publication_date',
                    evidence=dict(official_url=competitor.base_url,source_url=source['url'],article_url=card['source_url'],
                                  date_evidence=card['published_at_evidence'],snapshots=[path])))
                count+=int(fresh)
        elif source.get('mode') == 'catalogue':
            if not reasons and last_soup is not None:
                count, reason = self.product_changes(competitor, source, cards, proofs, last_soup)
                if reason:
                    reasons.append(reason)
                    pending.append(url)
        else:
            candidates = list(dict.fromkeys([*pending, *cards]))
            pending = []
            for index, url in enumerate(candidates):
                card, listing_path = cards.get(url, resume_context.get(url, ({}, '')))
                date = card.get('published_at')
                if date and not period.start <= date <= period.end:
                    continue
                if requests_used >= self.settings.max_pages_per_competitor:
                    pending.extend(candidates[index:]); reasons.append('article_page_limit'); break
                requests_used += 1
                html, _, status = self.fetch(url, mode)
                if not html:
                    pending.append(url); reasons.append(status); continue
                _, added, reason = self.article(competitor, source, url, html, card, listing_path)
                count += int(added and not reason)
                if reason:
                    reasons.append(reason)
            if source.get('mode')=='announcements' and not failures and not queue and cards:
                _,baseline_reason=self.product_changes(competitor,source,cards,proofs,last_soup)
                if baseline_reason:
                    reasons.append(baseline_reason)
        context={url:cards.get(url,resume_context.get(url,({},''))) for url in pending}
        cursor = json.dumps({'listings': list(dict.fromkeys(failures + queue)), 'articles': pending,
                             'completed_listings':sorted(completed),'article_context':context},
                            default=lambda value:value.isoformat()) if failures or queue or pending else ''
        status = 'partial' if reasons and cards else 'error' if reasons else 'success'
        self.store.check(run_id, competitor.code, source['kind'], source['url'], status,
                         '; '.join(dict.fromkeys(reasons))[:1500], cursor, len(seen), count,
                         source.get('official_evidence_url', competitor.base_url))
        logger.info('{} {}: {}, cards={}, added={}', competitor.code, source['kind'], status, len(cards), count)

    def inline_cards(self,soup,source):
        from .utils import parse_datetime_or_none
        result=[]
        for card in soup.select(source['card_selector']):
            ident=card.select_one('input.snCls[value]')
            title=card.select_one('.item-title')
            date=card.select_one('.item-top .col:nth-child(3)')
            body=card.select_one('.answer')
            if ident is None or title is None or date is None or body is None:
                continue
            raw_date=normalize_text(date.get_text(' ',strip=True))
            text=normalize_text(body.get_text(' ',strip=True))
            # Preserve the publisher's post ID independently of the archive page.
            url=source['url'].split('?',1)[0]+'?announcement_id='+ident['value']
            result.append(dict(url=url,title=normalize_text(title.get_text(' ',strip=True)),summary=text,
                published_at=parse_datetime_or_none(raw_date),published_at_evidence=raw_date,source_url=source['url']))
        return result

    def product_changes(self, competitor, source, cards, proofs, soup):
        current = {url: {'title': item['title'], 'description': item.get('summary', '')} for url, (item, _) in cards.items()}
        total_selector = source.get('total_selector')
        if total_selector:
            total_node = soup.select_one(total_selector)
            total = re.search(r'\d+', total_node.get_text() if total_node else '')
            if not total or int(total[0]) != len(current):
                return 0, 'incomplete_product_listing'
        old = self.store.conn.execute('SELECT * FROM product_baselines WHERE competitor_code=? AND url=?', (competitor.code, source['url'])).fetchone()
        added = 0
        if old:
            before = json.loads(old['cards_json'])
            old_proofs = json.loads(old['evidence_path']) if old['evidence_path'].startswith('[') else [old['evidence_path']]
            if not old_proofs or any(not Path(p).is_file() for p in old_proofs):
                return 0, 'baseline_evidence_missing'
            for url, card in current.items():
                if url in before and card == before[url]:
                    continue
                html, _, _ = self.fetch(url, source.get('crawl_mode', competitor.crawl_mode))
                if not html:
                    return added, 'product_detail_unavailable'
                detail = self.snapshot(competitor.code, url, html)
                action = 'Добавлена карточка' if url not in before else 'Изменена карточка'
                content = f"{action}: {card['title']}. {card['description']}"
                key = url + ('&' if '?' in url else '?') + 'observed_change=' + hashlib.sha256(json.dumps(card,sort_keys=True).encode()).hexdigest()[:16]
                _, fresh = self.store.record(dict(competitor_code=competitor.code, competitor_name=competitor.name,
                    kind='products', url=key, title=card['title'], summary=brief(content), original_text=content,
                    published_at=None, detected_at=now(), date_basis='observed_change',
                    evidence=dict(official_url=competitor.base_url, source_url=source['url'], article_url=url,
                                  date_evidence=f"Между {old['observed_at']} UTC и {now()} UTC",
                                  snapshots=[*old_proofs, *proofs, detail], before=before.get(url), after=card)))
                added += int(fresh)
        self.store.conn.execute('INSERT OR REPLACE INTO product_baselines VALUES(?,?,?,?,?)',
                                (competitor.code, source['url'], now(), json.dumps(current,ensure_ascii=False), json.dumps(proofs)))
        self.store.conn.commit()
        return added, ''

    def run(self, period, progress=None):
        run_id = self.store.start(period)
        completed = 0
        for competitor in self.competitors:
            for source in sources_for(competitor):
                if progress:
                    progress('Сбор источников', source['url'], completed, run_id)
                try:
                    self.collect_source(run_id, competitor, source, period)
                except Exception as exc:
                    logger.exception('Source failed {} {}', competitor.code, source['url'])
                    self.store.check(run_id, competitor.code, source['kind'], source['url'], 'error', type(exc).__name__ + ': ' + str(exc)[:300])
                completed += 1
            if competitor.telegram and competitor.telegram.enabled:
                if progress:
                    progress('Сбор Telegram', competitor.telegram.url, completed, run_id)
                try:
                    previous=self.store.conn.execute('''SELECT s.cursor FROM source_checks s JOIN collection_runs r ON r.id=s.run_id
                        WHERE s.competitor_code=? AND s.kind='telegram' AND r.period_start=? ORDER BY s.id DESC LIMIT 1''',
                        (competitor.code,period.start.isoformat())).fetchone()
                    results = self.telegram.crawl(competitor, period,previous[0] if previous else '')
                    for result in results:
                        self.store.record(dict(competitor_code=competitor.code, competitor_name=competitor.name, kind='telegram',
                            url=result.url, title=result.title, summary=brief(result.text), original_text=result.text,
                            published_at=result.published_at.isoformat(), detected_at=now(), date_basis='telegram_timestamp',
                            evidence=dict(official_url=competitor.base_url, source_url=competitor.telegram.url,
                                          date_evidence=result.published_at_evidence, snapshots=[result.local_snapshot_path],
                                          title_basis=result.title_basis,full_message=result.full_text)))
                    coverage = self.telegram.coverage
                    self.store.check(run_id, competitor.code, 'telegram', competitor.telegram.url, coverage['status'],
                                     coverage.get('reason',''), coverage.get('cursor',''), coverage.get('pages',0), len(results), competitor.base_url)
                except Exception as exc:
                    self.store.check(run_id, competitor.code, 'telegram', competitor.telegram.url, 'error', type(exc).__name__)
                completed += 1
        if progress:
            progress('Проверка материалов и ссылок', '', completed, run_id)
        self.revalidate_saved_events(period)
        self.check_event_links(run_id, period)
        # Coverage reports publications in the requested period, not just newly
        # inserted rows. A repeated successful run must not turn existing news
        # into the misleading "checked, no publications" state.
        events=self.store.events(period,[c.code for c in self.competitors])
        counts={}
        for event in events:
            source=json.loads(event['evidence_json']).get('source_url','')
            key=(event['competitor_code'],canonical_url(source))
            counts[key]=counts.get(key,0)+1
        for check in self.store.conn.execute('SELECT id,competitor_code,url FROM source_checks WHERE run_id=?',(run_id,)).fetchall():
            self.store.conn.execute('UPDATE source_checks SET items=? WHERE id=?',
                (counts.get((check['competitor_code'],canonical_url(check['url'])),0),check['id']))
        self.store.conn.commit()
        return dict(run_id=run_id, status=self.store.finish(run_id))

    def check_event_links(self, run_id, period):
        for event in self.store.events(period,[c.code for c in self.competitors]):
            evidence=json.loads(event['evidence_json'])
            url=evidence.get('article_url') or event['url']
            html,code,status=self.fetch(url,'http')
            self.store.conn.execute('INSERT OR REPLACE INTO event_link_checks VALUES(?,?,?,?,?,?)',
                (run_id,event['id'],url,now(),'success' if html else 'unavailable',f'HTTP {code}: {status}'[:500]))
        self.store.conn.commit()

    def revalidate_saved_events(self, period):
        competitors={c.code:c for c in self.competitors}
        fields=('competitor_code','competitor_name','kind','url','title','original_text','published_at','detected_at','date_basis','status','reason')
        for event in self.store.events(period,list(competitors)):
            item={key:event[key] for key in fields}
            item['evidence']=json.loads(event['evidence_json'])
            source=next((s for s in sources_for(competitors[event['competitor_code']]) if s['url']==item['evidence'].get('source_url')),None)
            paths=item['evidence'].get('snapshots',[])
            if paths and Path(paths[0]).is_file():
                html=Path(paths[0]).read_text(encoding='utf-8',errors='replace')
                _,_,soup=self.crawler.parse_html(html)
                if item['kind']=='telegram':
                    body=soup.select_one('.tgme_widget_message_text')
                    if body is not None:
                        from .content_extraction import telegram_content
                        title,text,basis,full=telegram_content(body,item['url'].rsplit('/',1)[-1])
                        item.update(title=title,original_text=text)
                        item['evidence'].update(title_basis=basis,full_message=full,extraction_version=2)
                elif source and item['date_basis'] not in {'observed_change'} and source.get('mode')!='inline':
                    heading=soup.select_one(source.get('heading_selector','h1'))
                    title=normalize_text(heading.get_text(' ',strip=True)) if heading else item['title']
                    if title.casefold() in {'news','новости','press releases','company news | autonics global'}:
                        title=item['title']
                    text=article_body(soup,source.get('body_selectors',[]),title=title)
                    if len(text)>=40:
                        item.update(title=title,original_text=text)
                        item['evidence']['extraction_version']=2
                    else:
                        item.update(status='rejected',reason='article_content_missing')
            item['summary']=brief(item['original_text'])
            ident,_=self.store.record(item)
            if self.store.conn.execute('SELECT status FROM verified_events WHERE id=?',(ident,)).fetchone()[0]!='confirmed':
                continue
            if source and source['kind']=='news' and item['kind'] in {'news','products'}:
                item['kind']='products' if is_product_announcement(item['title'],item['original_text']) else 'news'
            if item['kind']!=event['kind']:
                self.store.record(item)

    def audit_legacy(self):
        """Reparse evidence, never copy the previous verified flag as proof."""
        competitors = {c.code:c for c in self.competitors}
        rows = self.storage.fetchall('SELECT p.*,c.code FROM pages p JOIN competitors c ON c.id=p.competitor_id ORDER BY p.checked_at,p.id')
        counts = {'confirmed':0, 'rejected':0}
        for row in rows:
            if row['code'] not in competitors:
                continue
            competitor = competitors[row['code']]
            raw_path = Path(row['local_snapshot_path'] or '')
            if not raw_path.is_file():
                # Old installations can have stored absolute paths elsewhere.
                candidates = list((self.settings.snapshots_dir / competitor.code).glob('**/' + raw_path.name)) if raw_path.name else []
                raw_path = candidates[0] if candidates else raw_path
            reason, event_id = 'missing_snapshot', None
            if raw_path.is_file() and raw_path.suffix == '.html':
                html = raw_path.read_text(encoding='utf-8', errors='replace')
                if row['source_type'] == 'news' and row['content_kind'] == 'article':
                    source = next((s for s in sources_for(competitor) if s['kind']=='news'), None)
                    if source:
                        card, listing_path = self.historical_card(competitor,source,row['url'])
                        card = card or {'title':row['title']}
                        event_id, _, reason = self.article(competitor, source, row['url'], html, card, listing_path, archived=True)
                elif row['source_type'] == 'telegram':
                    soup = BeautifulSoup(html, 'lxml'); stamp = soup.select_one('time[datetime]')
                    message = soup.select_one('.tgme_widget_message')
                    text_node = soup.select_one('.tgme_widget_message_text')
                    published = post_time(stamp['datetime']) if stamp else None
                    expected = competitor.telegram.url.rstrip('/').rsplit('/',1)[-1] if competitor.telegram else ''
                    ident = message.get('data-post','') if message else ''
                    if published and expected and ident.startswith(expected+'/') and text_node:
                        from .content_extraction import telegram_content
                        title,text,title_basis,full=telegram_content(text_node,ident.rsplit('/',1)[-1])
                        event_id, _ = self.store.record(dict(competitor_code=competitor.code,competitor_name=competitor.name,kind='telegram',
                            url='https://t.me/'+ident,title=title,summary=brief(text),original_text=text,
                            published_at=published.isoformat(),detected_at=now(),date_basis='telegram_timestamp',
                            evidence=dict(official_url=competitor.base_url,source_url=competitor.telegram.url,
                                          date_evidence=stamp['datetime'],snapshots=[str(raw_path.resolve())],archived=True,
                                          title_basis=title_basis,full_message=full)))
                        reason = ''
                    else:
                        reason = 'telegram_timestamp_or_channel_unverified'
                else:
                    reason = 'legacy_observation_not_publication'
            status = 'rejected' if reason else 'confirmed'
            self.store.audit('pages',row['id'],status,reason,event_id);counts[status]+=1
        for row in self.storage.fetchall('SELECT e.*,c.code FROM site_change_events e JOIN site_watches w ON w.id=e.site_watch_id JOIN competitors c ON c.id=w.competitor_id'):
            reason,event_id='Text diff is insufficient to confirm a business event',None
            competitor=competitors.get(row['code'])
            if competitor and row['article_url']:
                source=next((s for s in sources_for(competitor) if s['kind']=='news'),None)
                if source and urlsplit(row['article_url']).netloc==urlsplit(source['url']).netloc:
                    html,_,_=self.fetch(row['article_url'],source.get('crawl_mode',competitor.crawl_mode))
                    if html:
                        card,listing_path=self.historical_card(competitor,source,row['article_url'])
                        event_id,_,reason=self.article(competitor,source,row['article_url'],html,card,listing_path)
            self.store.audit('site_change_events',row['id'],'rejected' if reason else 'confirmed',reason,event_id)
        for row in self.storage.fetchall('SELECT id,url FROM telegram_posts'):
            found=self.store.conn.execute("SELECT id FROM verified_events WHERE url=? AND kind='telegram' AND status='confirmed'",(canonical_url(row['url']),)).fetchone()
            self.store.audit('telegram_posts',row['id'],'confirmed' if found else 'rejected',
                             '' if found else 'Original Telegram timestamp not recovered',found[0] if found else None)
        for entity in ('products','changes'):
            for row in self.storage.fetchall('SELECT id FROM '+entity):
                self.store.audit(entity,row['id'],'rejected','Legacy heuristic candidate; only independently proven publications or structured changes are reportable')
        return counts

    def historical_card(self,competitor,source,url):
        key=(competitor.code,source['url'])
        if key not in self.historical_listings:
            from .news_audit import NewsDateAuditor
            mapping,hashes={},set()
            for path in NewsDateAuditor._listing_snapshot_paths(self.settings.snapshots_dir/competitor.code,competitor):
                html=path.read_text(encoding='utf-8',errors='replace')
                digest=hashlib.sha256(html.encode()).hexdigest()
                if digest in hashes:
                    continue
                hashes.add(digest)
                _,_,soup=self.crawler.parse_html(html)
                for card in self.cards(soup,source,competitor):
                    if card.get('publication_date_status')!='verified':
                        continue
                    card_url=canonical_url(card['url'])
                    previous=mapping.get(card_url)
                    if previous and previous[0]['published_at']!=card['published_at']:
                        previous[0]['publication_date_status']='ambiguous'
                    elif not previous:
                        mapping[card_url]=(card,str(path.resolve()))
            self.historical_listings[key]=mapping
        return self.historical_listings[key].get(canonical_url(url),({},''))
