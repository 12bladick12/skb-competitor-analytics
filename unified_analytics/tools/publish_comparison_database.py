"""Validate, then explicitly import the reviewed local SKB prices into Supabase.

Never exports credentials or prices to GitHub. No existing product/history writes.
"""
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
import argparse
import hashlib
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
EXPECTED_HASH = '3730c0dc4b7655dd2427270fba3c7cd47ad7c9cf18bad655a40562f5cab64fc9'


def import_bounded(settings, metadata, rows):
    """Resume short transactions; the deployed completeness gate hides partial data."""
    from price_monitor.catalog_storage import CatalogRepository
    repo=CatalogRepository(settings=settings)
    fields=tuple(metadata)
    repo.batch('INSERT INTO own_price_imports ('+','.join(fields)+') VALUES ('+
        ','.join('%('+key+')s' for key in fields)+') ON CONFLICT(batch_id) DO NOTHING',metadata)
    before=repo.batch('SELECT count(*) n FROM own_product_prices WHERE batch_id=%(batch)s',{'batch':metadata['batch_id']})[0]['n']
    columns=('batch_id','article','source_model','catalog_id','catalog_model','net_price','gross_price','currency','unit','source_sheet','source_row')
    for offset in range(0,len(rows),10):
        subset=rows[offset:offset+10]
        params={};tuples=[]
        for number,row in enumerate(subset):
            params.update({key+str(number):row[key] for key in columns})
            tuples.append('('+','.join('%('+key+str(number)+')s' for key in columns)+')')
        repo.batch('INSERT INTO own_product_prices ('+','.join(columns)+') VALUES '+
            ','.join(tuples)+' ON CONFLICT(batch_id,article) DO NOTHING',params)
        if offset%250==0:print('Price rows transferred:',offset+len(subset),flush=True)
    return before<len(rows)


def local_prices(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        imports = [dict(row) for row in connection.execute('SELECT * FROM own_price_imports WHERE source_hash=?', (EXPECTED_HASH,))]
        if len(imports) != 1:
            raise ValueError('The approved source batch is not unique')
        metadata = imports[0]
        rows = [dict(row) for row in connection.execute('SELECT * FROM own_product_prices WHERE batch_id=? ORDER BY article', (metadata['batch_id'],))]
    assert metadata['effective_date'] == '2026-10-01' and metadata['vat_rate'] == '22'
    assert len(rows) == metadata['row_count'] == 3749
    assert sum(bool(row['catalog_id']) for row in rows) == metadata['matched_count'] == 1043
    assert len({row['article'] for row in rows}) == len(rows)
    assert metadata['batch_id'] == hashlib.sha256(f"{EXPECTED_HASH}|2026-10-01|22".encode()).hexdigest()
    for row in rows:
        net, gross = Decimal(row['net_price']), Decimal(row['gross_price'])
        assert net > 0 and gross == (net * Decimal('1.22')).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    return metadata, rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--settings', type=Path, default=ROOT / '.streamlit/secrets.toml')
    parser.add_argument('--source', type=Path, default=ROOT / 'data/comparison_preview/prices-v4.sqlite3')
    args = parser.parse_args()
    metadata, rows = local_prices(args.source)
    from price_monitor.sensoren_local import settings_from_file
    from price_monitor.catalog_storage import CatalogRepository
    from price_monitor.own_prices import OwnPrices
    from price_monitor.automatic_price_terms import SCHEMA
    from price_monitor.postgres import schema_sql
    from price_monitor.models import utcnow
    repo = CatalogRepository(settings={**settings_from_file(args.settings), 'reuse_connections': False})
    tables = repo.batch("SELECT tablename FROM pg_tables WHERE schemaname='price_monitor' AND tablename IN ('own_price_imports','own_product_prices','automatic_price_terms')")
    report = {'checked_at': utcnow(), 'source_hash': EXPECTED_HASH, 'rows': len(rows),
              'matched': metadata['matched_count'], 'tables_before': [row['tablename'] for row in tables], 'applied': False}
    report['product_counts'] = repo.batch('SELECT (SELECT count(*) FROM rules) products,(SELECT count(*) FROM observations) observations')[0]
    prices = OwnPrices(repo)
    if 'own_price_imports' in report['tables_before']:
        report['existing_batches'] = len(prices.imports())
    if args.apply:
        print('Creating additive price tables...',flush=True)
        prices.initialize()
        print('Creating quote terms table...',flush=True)
        repo.batch([statement for statement in schema_sql(SCHEMA).split(';') if statement.strip()])
        print('Importing approved price batch...',flush=True)
        report['inserted'] = import_bounded(repo.settings, metadata, rows)
        print('Verifying every price row by checksum...',flush=True)
        columns=('batch_id','article','source_model','catalog_id','catalog_model','net_price','gross_price','currency','unit','source_sheet','source_row')
        digests=sorted(hashlib.md5('\x1f'.join(str(row[key]) for key in columns).encode()).hexdigest() for row in rows)
        expected=hashlib.md5('|'.join(digests).encode()).hexdigest()
        expression='md5(concat_ws(chr(31),'+','.join(columns)+'))'
        actual=repo.batch('SELECT count(*) rows,md5(string_agg('+expression+",'|' ORDER BY "+expression+')) checksum FROM own_product_prices WHERE batch_id=%(batch)s',{'batch':metadata['batch_id']})[0]
        if actual['rows'] != len(rows) or actual['checksum'] != expected:
            raise RuntimeError('Cloud rows differ from the approved local source')
        report['applied'] = True
        report['verified_rows'] = actual['rows']
        report['verified_checksum'] = actual['checksum']
        report['tables_after'] = [r['tablename'] for r in repo.batch("SELECT tablename FROM pg_tables WHERE schemaname='price_monitor' AND tablename IN ('own_price_imports','own_product_prices','automatic_price_terms')")]
    destination = ROOT / 'analysis/search_release_2026_10_01'
    destination.mkdir(parents=True, exist_ok=True)
    (destination / ('database_applied.json' if args.apply else 'database_check.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Connection errors may contain private endpoints; don't echo them.
        print('Database check/import failed:', type(exc).__name__, 'SQLSTATE:', getattr(exc,'sqlstate',None), file=sys.stderr)
        import tomllib
        private=tomllib.loads((ROOT/'.streamlit/secrets.toml').read_text(encoding='utf-8-sig')).get('database',{})
        message=str(exc)
        for value in private.values():
            if isinstance(value,str) and value:message=message.replace(value,'[redacted]')
        print(message[:500],file=sys.stderr)
        sys.exit(1)
