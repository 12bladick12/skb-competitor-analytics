"""Bounded read-only public route diagnostics; no account/profile or DB access."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
import requests
from courts.parsers import blocked

ROUTES = {
    'kad': 'https://kad.arbitr.ru/',
    'mkad': 'https://mkad.arbitr.ru/',
    'ras': 'https://ras.arbitr.ru/',
    'fssp': 'https://fssp.gov.ru/iss/ip/',
    'fssp_open': 'https://opendata.fssp.gov.ru/',
    'fns': 'https://egrul.nalog.ru/index.html',
    'fns_pb': 'https://pb.nalog.ru/',
    'ksrf': 'https://www.ksrf.ru/Decision/extsearch.php',
    'ksrf_home': 'https://www.ksrf.ru/',
    'fedresurs': 'https://fedresurs.ru/',
    'bankrot': 'https://bankrot.fedresurs.ru/',
    'sudrf': 'https://sudrf.ru/index.php?id=300',
    'sudrf_search': 'https://bsr.sudrf.ru/bigs/portal.html',
    'vsrf': 'https://www.vsrf.ru/lk/practice/claims',
}


def public_url(url):
    """Never record query parameters (including transient access tokens)."""
    p = urlsplit(url)
    return f'{p.scheme}://{p.hostname or ""}{p.path}'


def summary(html):
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script,style'): node.decompose()
    return {
        'title': soup.title.get_text(' ', strip=True) if soup.title else '',
        'text': soup.get_text(' ', strip=True)[:5000],
        'fields': [{k: node.get(k) for k in ('id', 'name', 'type', 'placeholder')}
                   for node in soup.select('input:not([type=hidden]),textarea,select,button')][:60],
        'links': [{'text': a.get_text(' ', strip=True)[:100], 'url': a.get('href')}
                  for a in soup.select('a[href]') if re.search('поиск|решени|акты|данны|картотек|реестр', a.get_text(), re.I)][:30],
    }


def http_probe(name, output):
    started = time.monotonic()
    result = {'source': name, 'transport': 'http', 'url': ROUTES[name]}
    try:
        response = requests.get(ROUTES[name], timeout=(12, 25))
        response.encoding = response.apparent_encoding or 'utf-8'
        result.update(status=response.status_code, final_url=public_url(response.url), **summary(response.text))
        result['access_status'] = blocked(result['text'], response.status_code) or 'page_only'
        (output / (name + '-http.html')).write_text(response.text, encoding='utf-8')
    except requests.RequestException as exc:
        result.update(access_status='transport_error', error=type(exc).__name__)
    result['elapsed_seconds'] = round(time.monotonic() - started, 1)
    return result


def browser_probe(browser, name, output):
    started = time.monotonic()
    result = {'source': name, 'transport': 'browser', 'url': ROUTES[name], 'responses': [], 'failures': []}
    context = browser.new_context(locale='ru-RU')
    page = context.new_page()
    page.on('response', lambda r: result['responses'].append({'url': public_url(r.url), 'status': r.status})
            if r.request.resource_type in ('document', 'script', 'xhr', 'fetch') else None)
    page.on('requestfailed', lambda r: result['failures'].append({'url': public_url(r.url), 'error': r.failure})
            if r.resource_type in ('document', 'script', 'xhr', 'fetch') else None)
    try:
        result['stage'] = 'navigation_commit'
        response = page.goto(ROUTES[name], wait_until='commit', timeout=40000)
        result['status'] = response.status if response else None
        result['stage'] = 'content'
        page.wait_for_timeout(5000)
        page.locator('body').wait_for(timeout=5000)
        result.update(final_url=public_url(page.url), **summary(page.content()))
        result['access_status'] = blocked(page.locator('body').inner_text(timeout=3000), result['status'] or 200) or 'page_only'
        result['visible_fields'] = page.locator('input:not([type=hidden]),textarea,select,button').evaluate_all(
            'els => els.filter(el => el.getClientRects().length).map(el => ({id:el.id,name:el.name,type:el.type,placeholder:el.getAttribute("placeholder")}))')
        result['stage'] = 'page_inspected'
    except Exception as exc:
        result.update(access_status='transport_error', error=type(exc).__name__)
    finally:
        try:
            html = page.content()
            (output / (name + '-browser.html')).write_text(html, encoding='utf-8')
            if 'text' not in result: result.update(**summary(html))
        except Exception: pass
        context.close()
    result['elapsed_seconds'] = round(time.monotonic() - started, 1)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', nargs='+', choices=ROUTES, default=list(ROUTES))
    parser.add_argument('--mode', choices=['http', 'browser'], required=True)
    parser.add_argument('--channel', choices=['msedge', 'chrome'])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    def save(row):
        row['checked_at'] = datetime.now(timezone.utc).isoformat()
        rows.append(row)
        (args.output / (args.mode + '-results.json')).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: row.get(k) for k in ('source', 'transport', 'status', 'stage', 'access_status', 'error', 'title', 'elapsed_seconds')}, ensure_ascii=False), flush=True)
    if args.mode == 'http':
        # Each route once, bounded parallelism; never retries an access denial.
        with ThreadPoolExecutor(max_workers=3) as executor:
            for row in executor.map(lambda name: http_probe(name, args.output), args.sources): save(row)
    else:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as engine:
            options = {'headless': True}
            if args.channel: options['channel'] = args.channel
            browser = engine.chromium.launch(**options)
            try:
                for name in args.sources: save(browser_probe(browser, name, args.output))
            finally: browser.close()


if __name__ == '__main__': main()
