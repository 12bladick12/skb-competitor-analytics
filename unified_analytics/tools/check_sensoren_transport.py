"""Bounded live check of the production Sensoren transport; no database writes."""
import argparse
import csv
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from price_monitor.adapters import ADAPTERS
from price_monitor.catalog import discover, seeds
from price_monitor.details import parse_catalog_product
from price_monitor.models import Rule, utcnow
from price_monitor.sensoren_browser import SensorenBrowserClient
from price_monitor.transport import FetchError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--discovery', action='store_true', help='Also inspect one category and product per manufacturer')
    args = parser.parse_args()
    output = ROOT / 'data' / 'sensoren_browser_integration'
    output.mkdir(parents=True, exist_ok=True)
    report = {'started_at': utcnow(), 'production_changed': False, 'products': [], 'navigation': []}
    rows = [r for r in csv.DictReader((ROOT / 'examples/tasks.csv').open(encoding='utf-8-sig'), delimiter=';')
            if r['source'] == 'sensoren'][:6]
    client = SensorenBrowserClient()

    def fetch(url, html=True):
        started = time.monotonic()
        final, status, body = client.fetch_document(url, html_only=html)
        return final, status, body, round(time.monotonic()-started, 2)

    try:
        for row in rows:
            rule = Rule(**{k: row[k] for k in ('source', 'manufacturer', 'article', 'product_url', 'url_template')})
            url, status, body, seconds = fetch(rule.url)
            result = ADAPTERS['sensoren'].parse(rule, body, url, status)
            details = json.loads(result.details_json)
            item = dict(manufacturer=rule.manufacturer, article=rule.article, url=url, http=status,
                        status=result.status, price=result.price, currency=result.currency,
                        attributes=len(details.get('attributes', [])), seconds=seconds)
            report['products'].append(item)
            (output / (rule.manufacturer.replace('+', '_')+'.html')).write_text(body, encoding='utf-8')
            print(json.dumps(item, ensure_ascii=False), flush=True)
            if status != 200 or result.status != 'priced' or not item['attributes']:
                raise RuntimeError('Unexpected sample result: '+rule.manufacturer)
        if args.discovery:
            for row in rows:
                brand = row['manufacturer']
                _, landing = seeds('sensoren', [brand])[0]
                url, status, body, seconds = fetch(landing, False)
                links = discover('sensoren', 'listing', body, url, [brand]) if status == 200 else []
                item = dict(manufacturer=brand, url=url, http=status, links=len(links), seconds=seconds)
                report['navigation'].append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                category = next((u for kind, u in links if kind == 'listing' and '/catalog/' in u), None)
                if not category:
                    continue
                url, status, body, seconds = fetch(category, False)
                links = discover('sensoren', 'listing', body, url, [brand]) if status == 200 else []
                item = dict(manufacturer=brand, url=url, http=status, links=len(links), seconds=seconds,
                            pagination=[u for k, u in links if k == 'listing' and '?' in u][:3])
                report['navigation'].append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                product = next((u for kind, u in links if kind == 'product' and u != row['product_url']), None)
                if product:
                    url, status, body, seconds = fetch(product)
                    parsed, reason = parse_catalog_product('sensoren', body, url, [brand])
                    report['navigation'].append(dict(manufacturer=brand, url=url, http=status,
                        products=len(parsed), reason=reason, seconds=seconds,
                        attributes=[len(json.loads(o.details_json).get('attributes', [])) for r, o in parsed]))
                if item['pagination']:
                    target = item['pagination'][0]
                    client.policy(target)
                    url, status, body, seconds = fetch(target, False)
                    report['navigation'].append(dict(manufacturer=brand, url=url, http=status,
                        links=len(discover('sensoren', 'listing', body, url, [brand])), seconds=seconds))
        report['success'] = True
    except (FetchError, RuntimeError) as exc:
        report['success'] = False
        report['error'] = str(exc)
        if isinstance(exc, FetchError):
            report['error_status'] = exc.status
            report['error_http'] = exc.http_status
        print(json.dumps({'error': report['error']}, ensure_ascii=False), flush=True)
    finally:
        report['metrics'] = dict(client.metrics)
        report['last_browser_state'] = client.last_browser_state
        client.close()
        report['finished_at'] = utcnow()
        (output / 'check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'success': report['success'], 'metrics': report['metrics']}), flush=True)
    return 0 if report['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
