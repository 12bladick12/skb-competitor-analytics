"""Bounded real-data preview when full DB snapshots are unavailable locally."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import base64
import json
import sys
import time
import tomllib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'data/comparison_preview/deps'))

from price_monitor.catalog_storage import CatalogRepository
from price_monitor.library import PRODUCT_SELECT
from price_monitor.models import utcnow
from price_monitor.sources import SOURCES
from price_monitor.scope import VISIBLE

def main():
    settings=tomllib.loads((ROOT/'.streamlit/secrets.toml').read_text(encoding='utf-8-sig'))['database']
    settings={k:v for k,v in settings.items() if k in ('host','port','dbname','user','password','sslmode','sslrootcert')}
    settings['reuse_connections']=False
    repo=CatalogRepository(settings=settings)
    target=ROOT/'data/comparison_preview/competitors.json'
    existing=json.loads(target.read_text(encoding='utf-8')) if target.exists() else {'products':[]}
    by_id={r['rule_id']:r for r in existing['products']}
    ids=set()
    for spec in SOURCES.values():
        for brand in spec.brands:
            # All manufacturers and several common diameters are represented.
            for token in ('08','12','18','30'):
                rows=repo.batch('''SELECT q.id FROM rules q JOIN product_index i ON i.rule_id=q.id
                    WHERE '''+VISIBLE+''' AND q.manufacturer=%(brand)s
                    AND lower(i.title||' '||i.category) LIKE %(family)s
                    AND lower(q.article||' '||i.title) LIKE %(token)s
                    ORDER BY q.id LIMIT 12''',{'brand':brand,'token':'%'+token+'%','family':'%индуктив%'})
                ids.update(row['id'] for row in rows)
            print('Selected',brand,len(ids),flush=True)
    ids.update(r['rule_id'] for r in repo.batch('SELECT rule_id FROM comparison_items'))
    def capture(rid):
        record=repo.batch(PRODUCT_SELECT+' WHERE q.id=%(id)s',{'id':rid})[0]
        # The network drops large TLS responses: transfer original evidence as
        # small ASCII chunks, in separate read-only transactions.
        expression="""encode(convert_to(jsonb_build_object('attributes',d.details_json::jsonb->'attributes',
            'price_terms',d.details_json::jsonb->'price_terms', 'category',d.details_json::jsonb->'category')::text,'UTF8'),'base64')"""
        chunks=[];offset=1
        while True:
            response=repo.batch('SELECT substring('+expression+''' FROM %(offset)s FOR 4000) payload
                FROM product_index i JOIN product_documents d ON d.fingerprint=i.details_hash
                WHERE i.rule_id=%(id)s''',{'id':rid,'offset':offset})
            if not response or not response[0]['payload']:break
            chunk=response[0]['payload'];chunks.append(chunk)
            if len(chunk)<4000:break
            offset+=4000
        record['_specifications']=json.loads(base64.b64decode(''.join(chunks))) if chunks else {}
        return record
    failures=[]
    remaining=sorted(ids-set(by_id))
    print('Capturing',len(remaining),'models',flush=True)
    def save():
        payload={'snapshot_at':utcnow(),'scope':'preview_sample','requested_models':len(ids),
                 'description':'Реальные карточки всех производителей, выборка исполнений 08/12/18/30 и сохранённые сравнения. Последние полученные цены; полная история не выгружена.',
                 'products':list(by_id.values()),'failed_ids':failures}
        target.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    with ThreadPoolExecutor(max_workers=4) as executor:
        work={executor.submit(capture,rid):rid for rid in remaining}
        for future in as_completed(work):
            rid=work[future]
            try:by_id[rid]=future.result()
            except Exception as exc:
                failures.append(rid);print('Read failed',rid,type(exc).__name__,flush=True)
            if (len(by_id)+len(failures))%10==0:
                save();print('Saved',len(by_id),'failed',len(failures),flush=True)
    save()
    print('Complete',len(by_id),'brands',sorted({r['manufacturer'] for r in by_id.values()}),flush=True)

if __name__=='__main__':main()
