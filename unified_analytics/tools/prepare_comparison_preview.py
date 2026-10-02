"""Import the approved SKB workbook into a specified LOCAL review database."""
from pathlib import Path
import argparse
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def import_prices(destination,workbook,effective_date='2026-10-01',vat_rate='22'):
    from price_monitor.storage import Store
    from price_monitor.own_prices import OwnPrices,read_prices
    from price_monitor.matching import load_catalog
    store=Store(destination)
    prices=OwnPrices(store.catalog)
    _,matcher=load_catalog()
    metadata,rows=read_prices(workbook.read_bytes(),workbook.name,effective_date,matcher,vat_rate)
    inserted=prices.import_rows(metadata,rows)
    actual=prices.repo.batch('SELECT count(*) n FROM own_product_prices WHERE batch_id=%(batch)s',
                            {'batch':metadata['batch_id']})[0]['n']
    if actual!=len(rows):raise RuntimeError('Import verification failed')
    report={**json.loads(metadata['report_json']),'inserted':inserted,'stored_rows':actual,
            'source_file':metadata['source_name'],'source_sha256':metadata['source_hash']}
    (destination.parent/'own_prices_import.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--database',type=Path,default=ROOT/'data/comparison_preview/prices-v4.sqlite3')
    parser.add_argument('--prices',type=Path,required=True)
    parser.add_argument('--effective-date',default='2026-10-01')
    parser.add_argument('--vat-rate',default='22')
    args=parser.parse_args()
    import_prices(args.database.resolve(),args.prices.resolve(),args.effective_date,args.vat_rate)
