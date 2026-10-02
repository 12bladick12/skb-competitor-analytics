"""Portable collection for public mentions and court cases, with explicit coverage."""
from datetime import datetime, timezone
import hashlib
from html import escape
import json
from pathlib import Path
from urllib.parse import urlsplit

from .intelligence_registry import ENTITIES
from .intelligence_web import PublicWeb, SourceUnavailable
from .intelligence_mentions import BraveSearch, ReadingAgent, canonical, extract_article, identity, excerpts, own_site, queries
from .intelligence_courts import card_links, parse_card, update_case, validate_observation


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def key_for(*values):
    return hashlib.sha256('\n'.join(values).encode('utf-8')).hexdigest()


def collect_intelligence(library, period, snapshots, progress=lambda *a: None, config=None,
                         *, web=None, search=None, seeds=(), case_imports=()):
    config = config or {}
    web = web or PublicWeb()
    search = search or BraveSearch(config.get('search_api_key'))
    agent = ReadingAgent(config)
    snapshots = Path(snapshots); snapshots.mkdir(parents=True, exist_ok=True)
    previous_mentions = library.get('intel_mention', {})
    previous_cases = library.get('intel_case', {})
    mentions, cases, checks, events, objects = {}, {}, [], [], {}
    start, end = period.start.date().isoformat(), period.end.date().isoformat()
    cap = min(max(int(config.get('max_articles_per_company', 8)), 1), 25)

    def save(body, suffix='.html'):
        digest = hashlib.sha256(body).hexdigest()
        objects[digest] = body
        path = snapshots / (digest + suffix)
        path.write_bytes(body)
        return digest, str(path.resolve())

    def check(code, kind, url, status, reason='', items=0):
        value = dict(competitor_code=code, competitor_name=ENTITIES[code]['name'], kind=kind,
                     url=url, status=status, reason=reason, items=items, checked_at=utcnow(),
                     period_start=start, period_end=end, scope='configured_queries_and_known_records')
        checks.append(value)

    try:
        for code, entity in ENTITIES.items():
            progress('Поиск внешних упоминаний', entity['name'])
            candidates = [dict(x) for x in seeds if x.get('competitor_code') == code]
            # A previously discovered URL can still be rechecked without a search key.
            candidates += [dict(url=m['url'], title=m.get('title', ''), discovery='known_url')
                           for m in previous_mentions.values() if m['competitor_code'] == code
                           and (not m.get('published_at') or start <= m['published_at'][:10] <= end)]
            for query in queries(entity):
                try:
                    discovered = search.search(query, start, end)
                    candidates += discovered
                    check(code, 'mentions', 'https://api.search.brave.com/', 'success',
                          'Проверен запрос: ' + query + '; до 10 кандидатов, не весь интернет', len(discovered))
                except SourceUnavailable as exc:
                    check(code, 'mentions', 'https://api.search.brave.com/', 'partial', exc.reason)
                    break
            unique = {}
            for candidate in candidates:
                target = canonical(candidate['url'])
                if not own_site(target, entity):
                    unique.setdefault(target, candidate)
            if len(unique) > cap:
                check(code, 'mentions', '', 'partial', 'article_budget_reached')
            for url, lead in list(unique.items())[:cap]:
                ident = key_for(code, url)
                previous = previous_mentions.get(ident)
                progress('Проверка публикации', url)
                try:
                    page = web.fetch(url)
                    article = extract_article(page)
                    raw_id, raw_path = save(page.body, '.pdf' if article['format'] == 'pdf' else '.html')
                    proof_id, proof_path = raw_id, raw_path
                    if article['format'] == 'pdf':
                        envelope = ('<!doctype html><meta charset="utf-8"><h1>Текст PDF</h1><p>'
                            + escape(page.url) + '</p><p>SHA-256: ' + raw_id + '</p><pre>'
                            + escape(article['text']) + '</pre>').encode('utf-8')
                        proof_id, proof_path = save(envelope)
                    match = identity(article['text'], entity)
                    date = article['published_at']
                    in_period = bool(date and start <= date <= end)
                    status = ('confirmed' if match != 'ambiguous' and date and date <= datetime.now(timezone.utc).date().isoformat() and len(article['text']) >= 100
                              and article['title'] and not own_site(page.url, entity) else 'needs_review')
                    if own_site(page.url, entity):
                        status = 'official_site'
                    content_hash = key_for(code, article['text'])
                    duplicate = next((m['id'] for m in [*previous_mentions.values(), *mentions.values()]
                        if m['id'] != ident and m.get('content_hash') == content_hash and m.get('status') == 'confirmed'), None)
                    reading = agent.read(article['text'], entity) if status == 'confirmed' and not duplicate else {'state': 'not_run'}
                    summary = ' '.join(reading.get('excerpts', []))[:1800] or excerpts(article['text'], entity)
                    record = dict(id=ident, competitor_code=code, competitor_name=entity['name'],
                        url=url, publisher_url=page.url, title=article['title'], description=summary,
                        original_text=article['text'], published_at=date, date_evidence=article['date_evidence'],
                        detected_at=(previous or {}).get('detected_at', utcnow()), checked_at=utcnow(),
                        identity_basis=match, status='duplicate' if duplicate else status, duplicate_of=duplicate,
                        content_hash=content_hash, evidence_ids=[proof_id], raw_asset_id=raw_id,
                        in_requested_period=in_period,
                        format=article['format'], pages=article['pages'], organizations=reading.get('organizations', []),
                        agent=reading, discovery=lead.get('discovery', 'provided_url'), access_state='success',
                        geography=lead.get('geography','Не определена'))
                    mentions[ident] = record
                    if record['status'] == 'confirmed':
                        events.append(dict(competitor_code=code, competitor_name=entity['name'], kind='mentions',
                            url=url, title=article['title'], original_text=article['text'], summary=summary,
                            published_at=date + 'T00:00:00', detected_at=record['detected_at'], date_basis='publisher_date',
                            evidence=dict(official_url=page.url, source_url='https://api.search.brave.com/', article_url=page.url,
                                          date_evidence=article['date_evidence'], snapshots=[proof_path],
                                          publisher_kind='external', identity_basis=match, raw_sha256=raw_id)))
                    check(code, 'mentions', url, 'success' if record['status'] in ('confirmed', 'duplicate', 'outside_period', 'official_site') else 'partial',
                          record['status'] if in_period else 'outside_period' if date else 'date_unconfirmed',
                          int(record['status'] == 'confirmed' and in_period))
                except SourceUnavailable as exc:
                    check(code, 'mentions', url, 'error', exc.reason)
                    if previous:
                        mentions[ident] = {**previous, 'access_state': 'unavailable', 'last_attempt_at': utcnow()}
            progress('Поиск судебных дел по ИНН', entity['inn'])
            links, search_ok = [], False
            try:
                result = web.court_search(entity['inn'])
                save(result.body)
                links = card_links(result.text)
                search_ok = True
                check(code, 'litigation', 'https://kad.arbitr.ru/', 'partial',
                      'Первая страница по ИНН; более ранние дела проверяются по сохранённым карточкам. '
                      'Полнота всех страниц не подтверждена.', len(links))
            except SourceUnavailable as exc:
                check(code, 'litigation', 'https://kad.arbitr.ru/', 'error', exc.reason)
            known = {c['url']: c for c in previous_cases.values() if c['competitor_code'] == code}
            # After an explicit source block, do not retry other endpoints on that host.
            if search_ok:
                links = list(dict.fromkeys([*known, *links]))
                limit = min(max(int(config.get('max_cases_per_company', 30)), 1), 100)
                if len(links) > limit:
                    check(code, 'litigation', 'https://kad.arbitr.ru/', 'partial', 'case_budget_reached')
                for url in links[:limit]:
                    ident = key_for(code, url)
                    previous = previous_cases.get(ident)
                    try:
                        page = web.fetch(url)
                        observation = parse_card(page, entity)
                        proof_id, _ = save(page.body)
                        observation.update(competitor_code=code, competitor_name=entity['name'], evidence_ids=[proof_id])
                        # A machine parse must not silently overwrite reviewed details.
                        if previous and previous.get('reviewer'):
                            if previous.get('raw_asset_id') == proof_id:
                                cases[ident] = {**previous, 'checked_at': utcnow(), 'access_state': 'success'}
                            else:
                                cases[ident] = {**previous, 'needs_review': True, 'new_evidence_ids': [proof_id],
                                                'last_attempt_at': utcnow(), 'access_state': 'changed'}
                        else:
                            observation.update(raw_asset_id=proof_id, id=ident)
                            cases[ident] = update_case(previous, observation, utcnow())
                        check(code, 'litigation', url, 'partial' if cases[ident].get('needs_review') else 'success',
                              'card_requires_review' if cases[ident].get('needs_review') else '', 1)
                    except SourceUnavailable as exc:
                        check(code, 'litigation', url, 'error', exc.reason)
                        if previous:
                            cases[ident] = {**previous, 'access_state': 'unavailable', 'last_attempt_at': utcnow()}
            for url, old in known.items():
                ident = key_for(code, url)
                if ident not in cases:
                    cases[ident] = {**old, 'access_state': 'not_checked', 'last_attempt_at': utcnow()}
        for imported in case_imports:
            observation = validate_observation(imported)
            code = next((k for k, e in ENTITIES.items() if e['inn'] == observation['competitor_inn']), None)
            if code is None:
                raise ValueError('Юридическое лицо не включено в мониторинг')
            # The supplied raw official page is mandatory; a JSON claim alone is not evidence.
            try:
                page = web.fetch(observation['url'])
            except SourceUnavailable as exc:
                check(code, 'litigation', observation['url'], 'error', 'review_not_applied:' + exc.reason)
                continue
            soup_text = normalized_page_text(page.text)
            if observation['original_text'] not in soup_text:
                check(code, 'litigation', observation['url'], 'error', 'review_evidence_mismatch')
                continue
            proof_id, proof_path = save(page.body)
            ident = key_for(code, observation['url'])
            observation = {**observation, 'id': ident, 'competitor_code': code,
                'competitor_name': ENTITIES[code]['name'], 'needs_review': False,
                'evidence_ids': [proof_id], 'raw_asset_id': proof_id}
            cases[ident] = update_case(previous_cases.get(ident), observation, utcnow())
            for event in observation.get('events', []):
                token = key_for(observation['case_number'], event['date'], event['description'])
                # Each event has a stable distinct URL; article_url retains the real court link.
                events.append(dict(competitor_code=code, competitor_name=ENTITIES[code]['name'], kind='litigation',
                    url=observation['url'] + '?event=' + token, title=observation['case_number'] + ': ' + event['description'],
                    original_text=event['evidence'], summary=event['description'], published_at=event['date'] + 'T00:00:00',
                    detected_at=utcnow(), date_basis='court_document', evidence=dict(official_url=observation['url'],
                    source_url='https://kad.arbitr.ru/', article_url=observation['url'], date_evidence=event['date'], snapshots=[proof_path])))
        if not agent.model:
            model_status = 'not_configured'
        else:
            model_status = agent.error or 'configured'
        records = ([dict(kind='intel_mention', key=k, payload=v) for k, v in mentions.items()]
                   + [dict(kind='intel_case', key=k, payload=v) for k, v in cases.items()])
        run_key = utcnow()
        for i, check_row in enumerate(checks):
            check_id = key_for(run_key, str(i), check_row['competitor_code'], check_row['url'])
            records.append(dict(kind='intel_check', key=check_id, payload={**check_row, 'id': check_id}))
        return dict(records=records, objects=objects, events=events, checks=checks,
                    status='partial' if any(c['status'] != 'success' for c in checks) else 'success',
                    model_status=model_status, model_calls=agent.calls)
    finally:
        web.close()
        search.close()


def normalized_page_text(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.select('script,style,nav,footer'):
        tag.decompose()
    from .intelligence_mentions import normalized
    return normalized(soup.get_text(' ', strip=True))
