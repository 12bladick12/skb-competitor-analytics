"""Read-only timing and completeness check of the full production search catalog."""
from pathlib import Path
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    from price_monitor.sensoren_local import settings_from_file
    from price_monitor.catalog_storage import CatalogRepository
    from price_monitor.library import Library
    from price_monitor.comparison_groups import ComparisonIndex
    from price_monitor.catalog_search import CatalogSearch
    repo=CatalogRepository(settings={**settings_from_file(ROOT/'.streamlit/secrets.toml'),'reuse_connections':False})
    begin=time.monotonic()
    library=Library(repo)
    expected=library.summary()['products']
    print('Visible catalog rows:',expected,flush=True)
    batch=repo.batch
    def measured(sql,params=None):
        start=time.monotonic()
        result=batch(sql,params)
        print('Read batch:',len(result),'rows;',round(time.monotonic()-start,2),'seconds',flush=True)
        return result
    repo.batch=measured
    rows=library.comparison_products()
    read_seconds=time.monotonic()-begin
    assert len(rows)==expected and len({row['rule_id'] for row in rows})==expected
    index=ComparisonIndex(rows,[],{})
    search=CatalogSearch(index.records)
    report={'rows':len(rows),'expected_rows':expected,'manufacturers':len({row['manufacturer'] for row in rows}),
            'read_seconds':round(read_seconds,2),'total_seconds':round(time.monotonic()-begin,2),
            'exact_example_found':bool(search.search('LR18XBF08DPOY-E2'))}
    (ROOT/'analysis/search_release_2026_10_01/catalog_check.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Catalog check failed:',type(exc).__name__,file=sys.stderr)
        sys.exit(1)
