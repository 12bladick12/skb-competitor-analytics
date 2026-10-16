"""Merge released judicial facts at read time; never rewrite a news period or report."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from .gate import released, stale


def project(library, news, entities, checks):
    if not released(library.get('court_release',{}).get('public')):
        return library
    result=deepcopy(library)
    for entity in entities:
        result['competitor'].setdefault(entity['brand'],{'code':entity['brand'],'name':entity['brand_name'],
                                                      'base_url':entity['membership_sources'][0]})
    current=datetime.now(timezone(timedelta(hours=5))).strftime('%Y-%m')
    for event in news:
        if not event.get('published_at') or event.get('status')!='confirmed':continue
        result['event'][str(event['id'])]=event
        event_date=event['published_at'][:10]
        month=event_date[:7]
        result['period'].setdefault(month,{'key':month,'event_ids':[],'checks':[],'links':[]})
        for key,period in result['period'].items():
            belongs=(key==month if '__' not in key else key.split('__')[0]<=event_date<=key.split('__')[1])
            if belongs and event['id'] not in period['event_ids']:period['event_ids'].append(event['id'])
    # Current availability belongs to current reporting only, not every past month.
    if current in result['period']:
        for check in checks:
            result['period'][current]['checks'].append({**check,'competitor_code':check['brand'],
                'kind':'enforcement' if check['domain']=='enforcement' else 'court',
                'status':'success' if check['complete'] and not stale(check) else 'partial',
                'reason':check['reason'],'items':None if not check['complete'] else check['items']})
    for period in result['period'].values():
        period['event_ids'].sort(key=lambda i:(result['event'].get(str(i),{}).get('published_at') or '',str(i)),reverse=True)
    return result


def load_released(repository, library):
    if not released(library.get('court_release',{}).get('public')):return library
    from .store import CourtStore
    store=CourtStore({},repository=repository)
    return project(library,list(store.all(library['id'],'court_news')),
                   list(store.all(library['id'],'court_entity')),list(store.all(library['id'],'court_check')))
