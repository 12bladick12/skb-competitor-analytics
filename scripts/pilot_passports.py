"""Bounded public-source pilot in local SQLite; does not write production prices."""
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.sensoren_local import settings_from_file
from price_monitor.storage import Store
from price_monitor.catalog_schema import document
from price_monitor.passports import Passports
from price_monitor.passport_transport import LocalFiles
from price_monitor.passport_processing import download_job
from price_monitor.models import utcnow
from price_monitor.scope import VISIBLE

SAMPLES=[('megak','МЕГА-К','PS2-36M70-12B11-K'),('sensor','СЕНСОР','ВБИ-Д06-45У-1111-С'),
         ('teko','ТЕКО','CP S254R-3 (PG9)'),('beskonta','BESKONTA','SES-30N101FG'),
         ('sensoren','Autonics','PR30-15DP'),('sensoren','Balluff','BES005N'),
         ('sensoren','Pepperl+Fuchs','NJ4-12GK-SN'),('sensoren','ifm','KI505A'),
         ('sensoren','LANBAO','LR12XBF04DPOY-E2'),('sensoren','SICK','IME12-04BPSZC0S')]


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--settings',default='.streamlit/secrets.toml')
    parser.add_argument('--limit',type=int,default=10);parser.add_argument('--retry',action='store_true');args=parser.parse_args()
    folder=Path('data/passport_pilot');folder.mkdir(exist_ok=True)
    remote=CatalogRepository(settings={**settings_from_file(args.settings),'reuse_connections':False});store=Store(folder/'pilot.sqlite3')
    docs=Passports(store.catalog);files=LocalFiles(folder/'files');report=[]
    for source,brand,article in SAMPLES[:args.limit]:
        rows=remote.batch('''SELECT q.*,i.title,i.category,i.attributes_count FROM rules q
            JOIN product_index i ON i.rule_id=q.id
            WHERE q.source=%(source)s AND q.manufacturer=%(brand)s
            AND '''+VISIBLE+''' AND (q.article=%(article)s OR i.title LIKE %(pattern)s OR q.product_url LIKE %(urlpattern)s)
            ORDER BY CASE WHEN q.article=%(article)s THEN 0 ELSE 1 END,q.id LIMIT 1''',
            {'source':source,'brand':brand,'article':article,'pattern':'%'+article+'%','urlpattern':'%'+article.lower()+'%'})
        if not rows:
            rows=remote.batch('''SELECT q.*,i.title,i.category,i.attributes_count FROM rules q JOIN product_index i ON i.rule_id=q.id
                WHERE q.source=%(source)s AND q.manufacturer=%(brand)s AND '''+VISIBLE+'''
                ORDER BY CASE WHEN lower(i.category) LIKE %(family1)s OR lower(i.category) LIKE %(family2)s THEN 0 ELSE 1 END,q.id LIMIT 1''',
                {'source':source,'brand':brand,'family1':'%индуктив%','family2':'%емкост%'})
        if not rows:
            report.append({'source':source,'brand':brand,'requested':article,'state':'control_not_in_database'});continue
        row=rows[0]
        row['details_json']=json.dumps({'category':row['category'],'attributes':[]})
        fp,canonical,_=document(row['details_json']);p={**row,'now':utcnow(),'fp':fp,'canonical':canonical}
        store.catalog.batch(['''INSERT INTO rules(id,rule_key,source,manufacturer,article,product_url,url_template,created_at)
            VALUES(%(id)s,%(rule_key)s,%(source)s,%(manufacturer)s,%(article)s,%(product_url)s,%(url_template)s,%(created_at)s) ON CONFLICT(id) DO NOTHING''',
            'INSERT INTO product_documents VALUES(%(fp)s,%(canonical)s) ON CONFLICT(fingerprint) DO NOTHING',
            '''INSERT INTO product_index(rule_id,title,category,search_text,details_hash,attributes_count,updated_at)
            VALUES(%(id)s,%(title)s,%(category)s,%(title)s,%(fp)s,%(attributes_count)s,%(now)s) ON CONFLICT(rule_id) DO NOTHING''',
            '''INSERT INTO product_scope(rule_id,manufacturer,state,details_hash,checked_at)
            VALUES(%(id)s,%(manufacturer)s,'confirmed',%(fp)s,%(now)s) ON CONFLICT(rule_id) DO NOTHING'''],p)
        docs.schedule(limit=50)
        if args.retry:
            store.catalog.batch("UPDATE passport_jobs SET not_before=0 WHERE rule_id=%(id)s AND state='retry'",{'id':row['id']})
        job=docs.claim('download','pilot',rule_id=row['id']);started=time.monotonic()
        if job:
            try:download_job(docs,files,job,'pilot')
            except Exception as exc:
                docs.finish(job['id'],'pilot','retry',type(exc).__name__,3600)
        current=docs.product(row['id'])
        report.append({'source':source,'brand':brand,'article':row['article'],'url':row['product_url'],
            'state':current['document_state'],'note':current['document_note'],'fingerprint':current['current_fingerprint'],
            'seconds':round(time.monotonic()-started,1)})
        (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({k:report[-1][k] for k in ('source','brand','state','seconds')},ensure_ascii=True),flush=True)
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Pilot complete',len(report),flush=True)


if __name__=='__main__':main()
