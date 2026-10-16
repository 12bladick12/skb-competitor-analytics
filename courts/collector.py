"""Incremental ingestion: files first, then records and cursor in one fenced commit."""
from copy import deepcopy
from datetime import datetime
import os
import time
from cloud.artifacts import upload_objects
from .model import normalize_event, news_event, money, now, digest, participation, DOMAINS
from .registry import entities, fingerprint, validate_entity
from .gate import LOCAL


def record(kind, key, value):
    return {'kind':kind,'key':str(key),'payload':value}


def merge_fact(previous, current):
    """Keep missing fields and contradictory source observations, never erase good data."""
    if not previous:return deepcopy(current)
    merged=deepcopy(previous)
    conflicts=list(merged.get('conflicts',[]))
    for key,value in current.items():
        if value is None:continue
        if key in ('evidence_ids','entity_ids','linked_cases'):
            merged[key]=list(dict.fromkeys([*merged.get(key,[]),*value]))
        elif key=='participants':
            rows={}
            for party in [*merged.get(key,[]),*value]:
                identity=(party.get('inn'),party.get('role'))
                previous=rows.get(identity,{})
                rows[identity]={**previous,**party,
                    'evidence_ids':list(dict.fromkeys([*previous.get('evidence_ids',[]),*party.get('evidence_ids',[])])),
                    'source_urls':list(dict.fromkeys([*previous.get('source_urls',[]),*party.get('source_urls',[]),
                                                     *([party['source_url']] if party.get('source_url') else [])]))}
            merged[key]=list(rows.values())
        elif key in ('detected_at',):
            merged.setdefault(key,value)
        else:
            old=merged.get(key)
            if old is not None and old!=value and key not in ('checked_at','as_of','version','url','source','evidence_ids'):
                conflict={'field':key,'previous':old,'received':value,'source':current.get('source'),
                          'checked_at':current.get('checked_at')}
                if conflict not in conflicts:conflicts.append(conflict)
            merged[key]=deepcopy(value)
    if conflicts:merged['conflicts']=conflicts
    return merged


def ingest(store, drive, job, entity, page, previous_check=None, extra_records=(), persist_check=True):
    page.validate();records=[];cases={}
    known_assets=store.asset_ids(job['import_id']) | set(page.objects)
    def save(kind,key,value):
        old=store.get(job['import_id'],kind,key)
        merged=merge_fact(old,value)
        records.append(record(kind,key,merged));return merged
    for case in page.cases:
        if not participation(entity,case.get('participants',[])):continue
        if not case.get('evidence_ids') or not set(case['evidence_ids'])<=known_assets:
            raise ValueError('Нет подтверждающих документов дела.')
        case=deepcopy(case)
        for field in ('claim_amount','award_amount','costs'):case[field]=money(case.get(field))
        case['checked_at']=page.checked_at
        cases[case['id']]=save('court_case',case['id'],case)
    for candidate in page.candidates:
        save('court_candidate',candidate['id'],{**candidate,'checked_at':page.checked_at,'source':page.source})
    for doc in page.documents:
        if not set(doc.get('evidence_ids',[]))<=known_assets:raise ValueError('Документ не сохранён.')
        save('court_document',doc['id'],{**doc,'checked_at':page.checked_at})
    enforcement={}
    for item in page.enforcement:
        if not participation(entity,item.get('participants',[])) or not item.get('number'):continue
        if not item.get('evidence_ids') or not set(item['evidence_ids'])<=known_assets:
            raise ValueError('Исполнительное производство не имеет доказательства.')
        item=deepcopy(item)
        for field in ('original_amount','balance','fee'):item[field]=money(item.get(field))
        item['entity_ids']=list(dict.fromkeys([*item.get('entity_ids',[]),entity['id']]))
        enforcement[item['id']]=save('court_enforcement',item['id'],{**item,'checked_at':page.checked_at})
    for raw in page.events:
        case_id=raw.get('case_id')
        case=(cases.get(case_id) or store.get(job['import_id'],'court_case',case_id)) if case_id else None
        if raw.get('enforcement_id'):
            proceeding=enforcement.get(raw['enforcement_id']) or store.get(job['import_id'],'court_enforcement',raw['enforcement_id'])
            if proceeding:
                # An IP may have no verified court link. Do not invent a judicial case.
                case={**proceeding,'id':'enforcement:'+proceeding['id']}
        if not case:continue
        event=normalize_event(raw,case,entity,page.checked_at)
        if not event:continue
        if not set(event['evidence_ids'])<=known_assets:raise ValueError('Событие не имеет сохранённого доказательства.')
        # One procedural fact can belong to several monitored entities with different roles.
        event_key=digest([event['id'],entity['id']])
        event=save('court_event',event_key,event)
        news=news_event(event,case,entity)
        if news:
            save('court_news',news['id'],news)
            month=news['published_at'][:7]
            records.append(record('court_period',month,{'key':month}))
    old=previous_check or {}
    check={'entity_id':entity['id'],'brand':entity['brand'],'source':page.source,'domain':page.domain,
           'url':page.url,'status':page.status,'reason':page.reason,'checked_at':page.checked_at,
           'last_success_at':page.checked_at if page.complete else old.get('last_success_at'),
           'complete':page.complete,'all_history':page.history_complete,
           'known_cases_refreshed':page.known_cases_refreshed,'coverage_from':page.coverage_from,'coverage_to':page.coverage_to,
           'pages':old.get('pages',0)+1,'next_cursor':page.next_cursor,'diagnostics':page.diagnostics,
           'evidence_ids':list(page.objects), 'items':len(page.cases)+len(page.enforcement)}
    if persist_check:records.append(record('court_check',entity['id']+':'+page.source,check))
    records.extend(extra_records)
    assets=upload_objects(store,drive,job,page.objects) if page.objects else {}
    store.checkpoint(job,records,assets)
    return check


def collect(store, drive, job, browser, alive=lambda:None, *, sources=None, max_pages=25, seconds=2400):
    from .sources import CATALOG
    started=now();deadline=time.monotonic()+seconds
    registry=list(store.all(job['import_id'],'court_entity')) or entities()
    for entity in registry:validate_entity(entity)
    store.checkpoint(job,[record('court_entity',e['id'],e) for e in registry],{})
    checks={};completed=0
    sources=list(sources or CATALOG)
    # Round-robin pages and durable next pair prevent a large first-company archive
    # or slow primary source from starving later companies on free runner limits.
    pairs=[(source,entity,None) for source in sources for entity in registry]
    weekly=datetime.now(LOCAL).weekday()==6
    by_id={e['id']:e for e in registry}
    known_cases={c['id']:c for c in store.all(job['import_id'],'court_case')}
    for doc in store.all(job['import_id'],'court_document'):
        case=known_cases.get(doc.get('case_id'),{})
        if not weekly and case.get('status') in ('closed','completed'):continue
        if doc.get('source') not in sources or not doc['source'].startswith('sudact_'):continue
        for entity_id in doc.get('entity_ids',[]):
            if entity_id in by_id:pairs.append((doc['source'],by_id[entity_id],doc))
    progress=store.get(job['import_id'],'court_progress','scan') or {}
    index=int(progress.get('next_pair',0))%len(pairs)
    visits={};done=set()
    while len(done)<len(pairs) and time.monotonic()<deadline:
        position=index;index=(index+1)%len(pairs)
        if position in done:continue
        source,entity,document=pairs[position];alive()
        key=entity['id']+':'+source
        old=store.get(job['import_id'],'court_check',key) or {}
        cursor=old.get('next_cursor')
        store.pulse(job,'Судебные дела: проверка источников',source+' · '+entity['name'],completed)
        if document:
            cursor={'query_index':len(entity['names'])+1,'pending':[{'url':document['url']}],'after_url':None}
        page=browser.fetch(source,entity,cursor)
        failed=page.status in ('error','challenge','access_denied','rate_limited')
        if failed:page.next_cursor=cursor
        check=ingest(store,drive,job,entity,page,old,
                     [record('court_progress','scan',{'next_pair':index,'registry':fingerprint(registry)})],
                     persist_check=document is None)
        if document is None:checks[key]=check
        completed+=1;alive()
        visits[position]=visits.get(position,0)+1
        if document or page.next_cursor is None or failed or visits[position]>=max_pages:done.add(position)
    # A diagnostic/partial source can never produce a successful daily run.
    required={(e['id'],d) for e in registry for d in DOMAINS}
    checks=list(checks.values())
    covered={(c['entity_id'],c['domain']) for c in checks if c['status']=='success'
             and c['complete'] and c['all_history'] and c['known_cases_refreshed']}
    success=required<=covered
    run={'id':job['id'],'started_at':started,'finished_at':now(),'registry':fingerprint(registry),
         'environment':'github_actions' if os.getenv('GITHUB_ACTIONS')=='true' else 'local',
         'automatic':os.getenv('GITHUB_EVENT_NAME')=='schedule','status':'success' if success else 'partial','checks':checks}
    store.publish(job,[record('court_run',job['id'],run)],{},status=run['status'],result_id=job['id'])
    return run
