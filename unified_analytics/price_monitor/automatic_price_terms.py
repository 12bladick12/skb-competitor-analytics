"""Automatic rechecks tied to the exact price observation; never revise old VAT."""
import json
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading

from .models import Rule, utcnow
from .price_terms import terms_for

SCHEMA = '''CREATE TABLE IF NOT EXISTS automatic_price_terms (
 rule_id INTEGER NOT NULL, price_checked_at TEXT NOT NULL, terms_json TEXT NOT NULL,
 checked_at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
 PRIMARY KEY(rule_id,price_checked_at)
);'''

_executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='price-terms')
_jobs={}
_lock=threading.Lock()


def automatic_refresh(repository, rows):
    """One background recheck per database; failures have a one-day cooldown."""
    pending=[]
    now=datetime.now(timezone.utc)
    for row in rows:
        if not row.get('last_price'):continue
        term=current_terms(row)
        if term.get('basis') in ('gross','net') and term.get('rate') is not None:continue
        checked=(row.get('_terms_check') or {}).get('checked_at')
        if checked:
            try:
                if (now-datetime.fromisoformat(checked)).total_seconds()<86400:continue
            except ValueError:pass
        pending.append(dict(row))
    if not pending:return None
    key=str(repository.path) if repository.path else id(repository)
    with _lock:
        job=_jobs.get(key)
        if job and not job.done():return job
        job=_executor.submit(refresh_terms,repository,pending)
        _jobs[key]=job
        return job


def load_terms(repository, rows):
    if not rows:return rows
    ids=list(dict.fromkeys(int(r['rule_id']) for r in rows))
    stored={}
    for offset in range(0,len(ids),200):
        chunk=ids[offset:offset+200];params={f'i{n}':v for n,v in enumerate(chunk)}
        found=repository.batch('SELECT * FROM automatic_price_terms WHERE rule_id IN ('+
            ','.join(f'%(i{n})s' for n in range(len(chunk)))+')',params)
        stored.update({(r['rule_id'],r['price_checked_at']):r for r in found})
    for row in rows:
        record=stored.get((row['rule_id'],str(row.get('price_checked_at') or row.get('checked_at'))))
        if record:
            row['_automatic_price_terms']=json.loads(record['terms_json'])
            row['_terms_check']={k:record[k] for k in ('status','checked_at','detail')}
    return rows


def current_terms(row):
    if row.get('_automatic_price_terms'):
        return terms_for({**row, '_specifications':{'price_terms':row['_automatic_price_terms']}})
    if '_price_snapshot_terms' in row:
        return terms_for({**row, '_specifications':{'price_terms':row['_price_snapshot_terms']},'price_text':row.get('last_price_text')})
    return terms_for(row)


def refresh_terms(repository, rows):
    from .adapters import ADAPTERS
    from .transport import SourceClient, FetchError
    clients={};outcomes=[]
    try:
        for row in rows:
            previous=row.get('_automatic_price_terms') or {}
            terms=previous
            try:
                if row['source'] not in clients:
                    clients[row['source']]=SourceClient(row['source'])
                client=clients[row['source']]
                rule=Rule(row['source'],row['manufacturer'],row['article'],row['product_url'])
                url,status,html=client.fetch(rule.url)
                parsed=ADAPTERS[row['source']].parse(rule,html,url,status)
                if parsed.status!='priced':
                    state,detail='unavailable','Не удалось подтвердить цену карточки: '+parsed.status
                elif parsed.currency!=row.get('last_currency') or Decimal(parsed.price)!=Decimal(str(row.get('last_price') or '0')):
                    state,detail='price_changed','Цена на сайте изменилась; требуется новый сбор цены.'
                else:
                    found=json.loads(parsed.details_json).get('price_terms') or {}
                    if found.get('basis')!='unknown':
                        terms={**found,'verified_price':parsed.price,'verified_currency':parsed.currency}
                        state,detail='confirmed','Условия получены из карточки товара.'
                    else:
                        state,detail='not_found','Условия НДС в доступной карточке не указаны.'
            except FetchError as exc:
                state,detail='unavailable','Не удалось обновить условия: '+exc.status
            except (ValueError,TypeError,KeyError):
                state,detail='unavailable','Формат карточки изменился; требуется проверка распознавания.'
            params={'rule':row['rule_id'],'price_date':str(row.get('price_checked_at') or ''),
                    'terms':json.dumps(terms,ensure_ascii=False),'checked':utcnow(),'status':state,'detail':detail}
            repository.batch('''INSERT INTO automatic_price_terms(rule_id,price_checked_at,terms_json,checked_at,status,detail)
                VALUES(%(rule)s,%(price_date)s,%(terms)s,%(checked)s,%(status)s,%(detail)s)
                ON CONFLICT(rule_id,price_checked_at) DO UPDATE SET terms_json=excluded.terms_json,
                checked_at=excluded.checked_at,status=excluded.status,detail=excluded.detail''',params)
            outcomes.append({'Модель':row['article'],'Статус':state,'Результат':detail})
    finally:
        for client in clients.values():client.close()
    return outcomes
