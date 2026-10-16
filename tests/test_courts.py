from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
import hashlib
import unittest
from unittest.mock import Mock, patch
from zipfile import ZipFile

from courts.registry import entities, validate_entity, fingerprint
from courts.model import Page, participation, normalize_event, news_event, money, DOMAINS
from courts.parsers import sudact_document, sudact_links, total, blocked
from courts.gate import release_reasons, stale, released
from courts.collector import ingest, merge_fact
from courts.projection import project
from courts.runner import daily_key, schedule
from courts.service import CourtService


URL='https://sudact.ru/arbitral/doc/ABC123/'
HTML='''<h1>Постановление от 11 июня 2024 г. по делу № А76-27310/2021</h1>
<div class="b-justice">Восемнадцатый арбитражный апелляционный суд</div>
<h3>Истцы:</h3>АО НПК ТЕКО (ИНН: 7453019186)<br>
<h3>Ответчики:</h3>ООО ГлавАвтоматика (ИНН: 7447132031)<br>
<h3>Судьи дела:</h3>Судья А.<h3>Последние документы по делу:</h3>'''


class MemoryStore:
    def __init__(self):self.data={};self.assets=set();self.commits=0
    def get(self,ident,kind,key):return deepcopy(self.data.get((kind,key)))
    def asset_ids(self,ident):return self.assets.copy()
    def checkpoint(self,job,records,assets):
        self.commits+=1;self.assets.update(assets)
        for r in records:self.data[(r['kind'],r['key'])]=deepcopy(r['payload'])
    def all(self,ident,kind):return [deepcopy(v) for (k,_),v in self.data.items() if k==kind]
    def pulse(self,*a,**kw):pass
    def publish(self,job,records,assets,**kwargs):self.checkpoint(job,records,assets)


class CourtTests(unittest.TestCase):
    def setUp(self):self.entity=entities()[0]

    def test_registry_identifiers_and_membership_have_evidence(self):
        for e in entities():self.assertEqual(validate_entity(e),e)
        invalid=deepcopy(self.entity);invalid['inn']='7453019180'
        with self.assertRaises(ValueError):validate_entity(invalid)

    def test_registry_fingerprint_ignores_order_and_refresh_time(self):
        before=entities();after=list(reversed(deepcopy(before)))
        after[0]['fns_verified_at']='2026-10-16T01:00:00Z'
        self.assertEqual(fingerprint(before),fingerprint(after))
        after[0]['names'].append('Подтверждённое прежнее название')
        self.assertNotEqual(fingerprint(before),fingerprint(after))

    def test_collection_resumes_each_company_cursor_and_does_not_claim_complete(self):
        from courts.collector import collect
        store=MemoryStore();seen=[]
        class Browser:
            def fetch(self,source,entity,cursor):
                seen.append((entity['id'],cursor))
                return Page(source,'arbitral',entity['id'],URL,next_cursor={'page':(cursor or {}).get('page',0)+1})
        run=collect(store,None,{'id':'one','import_id':'x'},Browser(),sources=['sudact_arbitral'],max_pages=1)
        self.assertEqual(len(seen),5);self.assertEqual(run['status'],'partial')
        seen.clear()
        collect(store,None,{'id':'two','import_id':'x'},Browser(),sources=['sudact_arbitral'],max_pages=1)
        self.assertTrue(all(cursor=={'page':1} for _,cursor in seen))

    def test_structured_participation_not_mention_or_similar_name(self):
        for participants in ([{'name':'АО НПК ТЕКО','role':'plaintiff','role_evidence':'Истцы'}],
                             [{'inn':self.entity['inn'],'role':'supplier','identity_evidence':'ИНН','role_evidence':'Поставщики'}],
                             [{'inn':'0000000000','role':'plaintiff','identity_evidence':'ИНН','role_evidence':'Истцы'}]):
            self.assertFalse(participation(self.entity,participants))

    def test_sudact_roles_and_unknown_outcomes(self):
        result=sudact_document(HTML,URL,self.entity)
        self.assertEqual(len(result.cases),1)
        case=result.cases[0]
        self.assertEqual(case['participants'][0]['role'],'plaintiff')
        self.assertIsNone(case['status']);self.assertIsNone(case['award_amount'])
        self.assertFalse(result.complete)
        event=normalize_event(result.events[0],case,self.entity,'2026-10-16T00:00:00+00:00')
        self.assertEqual(news_event(event,case,self.entity)['published_at'],'2024-06-11')

    def test_supplier_in_body_is_only_candidate(self):
        html=HTML.replace('<h3>Истцы:</h3>','<p>Поставщики:').replace('7453019186','4028062967')
        result=sudact_document(html,URL,entities()[1])
        self.assertFalse(result.cases);self.assertEqual(len(result.candidates),1)

    def test_unknown_and_future_dates_do_not_become_current_news(self):
        p=sudact_document(HTML,URL,self.entity);case=p.cases[0];raw=p.events[0]
        raw['occurred_on']=None
        event=normalize_event(raw,case,self.entity,'2026-10-16T00:00:00+00:00')
        self.assertIsNone(news_event(event,case,self.entity))
        raw['occurred_on']='2026-11-01'
        self.assertIsNone(normalize_event(raw,case,self.entity,'2026-10-16T00:00:00+00:00'))

    def test_next_page_is_retained_and_document_links_are_deduplicated(self):
        html='<a href="/arbitral/doc/X/?q=x">Акт</a><a href="/arbitral/doc/X/">Акт</a><a href="?page=2">следующая</a><a href="https://evil.invalid/arbitral/doc/Y/">Другой</a>'
        docs,next_url=sudact_links(html,'https://sudact.ru/arbitral/','arbitral')
        self.assertEqual(len(docs),1);self.assertTrue(next_url.endswith('?page=2'))

    def test_search_counts_never_consume_case_number_next_line(self):
        self.assertEqual(total('Найдено: 1\n309-ЭС23-20007',True),1)
        self.assertEqual(total('Найдено 1 234 документа'),1234)
        self.assertIsNone(total('Идет поиск'))
        self.assertEqual(blocked('Введите код с картинки'),'challenge')
        self.assertEqual(blocked('',451),'access_denied')

    def test_success_requires_complete_pagination(self):
        with self.assertRaises(ValueError):Page('kad','arbitral','1',URL,status='success').validate()
        with self.assertRaises(ValueError):Page('kad','arbitral','1',URL,status='success',complete=True,next_cursor={'page':2}).validate()

    def test_amounts_do_not_default_to_zero_or_accept_nonfinite(self):
        self.assertIsNone(money(None));self.assertEqual(money({'amount':'0','currency':'RUB'})['amount'],'0')
        for raw in ({'amount':'NaN','currency':'RUB'},{'amount':'-1','currency':'RUB'},{'amount':'123'}):
            with self.assertRaises(ValueError):money(raw)

    def test_repeated_runs_keep_first_detection_and_several_events_same_case_url(self):
        store=MemoryStore();job={'import_id':'x'}
        with patch('courts.collector.upload_objects',side_effect=lambda s,d,j,o:{k:{} for k in o}):
            p=sudact_document(HTML,URL,self.entity);p.checked_at='2026-10-16T00:00:00+00:00'
            ingest(store,None,job,self.entity,p)
            first=[v for (k,_),v in store.data.items() if k=='court_event'][0]
            p.checked_at='2026-10-17T00:00:00+00:00';ingest(store,None,job,self.entity,p)
            events=[v for (k,_),v in store.data.items() if k=='court_event']
            self.assertEqual(len(events),1);self.assertEqual(events[0]['detected_at'],first['detected_at'])
            p.events[0]['canonical_event_id']='another-event';p.events[0]['title']='Поступление жалобы'
            ingest(store,None,job,self.entity,p)
            self.assertEqual(len([k for k in store.data if k[0]=='court_event']),2)

    def test_failed_storage_does_not_commit_cursor(self):
        store=MemoryStore();p=sudact_document(HTML,URL,self.entity)
        with patch('courts.collector.upload_objects',side_effect=RuntimeError('Drive failure')):
            with self.assertRaises(RuntimeError):ingest(store,None,{'import_id':'x'},self.entity,p)
        self.assertEqual(store.commits,0)

    def test_failed_source_keeps_data_and_last_success(self):
        store=MemoryStore();store.data[('court_case','existing')]={'id':'existing'}
        p=Page('kad','arbitral',self.entity['id'],URL,status='challenge')
        check=ingest(store,None,{'import_id':'x'},self.entity,p,{'last_success_at':'2026-10-15T00:00:00+00:00'})
        self.assertIn(('court_case','existing'),store.data)
        self.assertEqual(check['last_success_at'],'2026-10-15T00:00:00+00:00');self.assertFalse(check['complete'])

    def test_enforcement_events_do_not_require_an_invented_court_case(self):
        store=MemoryStore();raw=b'public enforcement proof';proof=hashlib.sha256(raw).hexdigest()
        party={'inn':self.entity['inn'],'role':'debtor','role_evidence':'Должник','identity_evidence':'ИНН'}
        p=Page('fssp','enforcement',self.entity['id'],URL,objects={proof:raw},
            enforcement=[{'id':'ip1','number':'123/26/74000-ИП','participants':[party],'evidence_ids':[proof],
                          'original_amount':{'amount':'100','currency':'RUB'},'balance':None}],
            events=[{'enforcement_id':'ip1','type':'opened','title':'Возбуждено исполнительное производство',
                     'occurred_on':'2026-10-01','evidence_ids':[proof],'url':URL}])
        with patch('courts.collector.upload_objects',return_value={proof:{}}):ingest(store,None,{'import_id':'x'},self.entity,p)
        self.assertFalse(store.all('x','court_case'))
        self.assertIsNone(store.all('x','court_enforcement')[0]['balance'])
        self.assertEqual(store.all('x','court_news')[0]['kind'],'enforcement')

    def test_conflicts_and_missing_fields_preserve_evidence(self):
        result=merge_fact({'status':'open','claim_amount':{'amount':'100','currency':'RUB'},'evidence_ids':['a']},
                          {'status':'closed','claim_amount':None,'evidence_ids':['b'],'source':'copy'})
        self.assertEqual(result['claim_amount']['amount'],'100');self.assertEqual(result['evidence_ids'],['a','b'])
        self.assertEqual(result['conflicts'][0]['field'],'status')

    def test_internal_data_does_not_enter_news_even_with_admin_preview(self):
        library={'court_release':{},'event':{},'period':{},'competitor':{}}
        self.assertIs(project(library,[{'id':'x'}],[],[]),library)
        self.assertFalse(released({'enabled':True}))

    def test_historical_backfill_and_range_projection_preserve_original_period(self):
        p=sudact_document(HTML,URL,self.entity)
        event=news_event(normalize_event(p.events[0],p.cases[0],self.entity,'2026-10-16T00:00:00+00:00'),p.cases[0],self.entity)
        library={'court_release':{'public':{'enabled':True,'certified_at':'2026-10-16','registry':'r','accepted_days':list(range(14))}},
                 'event':{},'period':{'2026-10':{'event_ids':[],'checks':[]},'2024-06-10__2024-06-12':{'event_ids':[],'checks':[]}},'competitor':{}}
        result=project(library,[event],[self.entity],[])
        self.assertFalse(result['period']['2026-10']['event_ids'])
        self.assertEqual(result['period']['2024-06']['event_ids'],[event['id']])
        self.assertEqual(result['period']['2024-06-10__2024-06-12']['event_ids'],[event['id']])
        self.assertFalse(library['event'])

    def test_gate_requires_fourteen_days_all_entities_domains_and_runner(self):
        es=entities()
        for e in es:e.update(fns_verified_at='2026-10-01',history_verified=True)
        at=datetime(2026,10,16,12,tzinfo=timezone.utc);fp=fingerprint(es)
        checks=[{'entity_id':e['id'],'domain':d,'status':'success','complete':True,
                 'all_history':True,'known_cases_refreshed':True} for e in es for d in DOMAINS]
        runs=[{'registry':fp,'environment':'github_actions','automatic':True,'checks':checks,'status':'success',
               'started_at':(at-timedelta(days=i)).isoformat(),'finished_at':(at-timedelta(days=i)).isoformat()} for i in range(1,15)]
        control={'registry':fp,'passed':True,'evidence_ids':['a'*64]}
        self.assertFalse(release_reasons(es,runs,control,at))
        self.assertTrue(release_reasons(es,runs[:-1],control,at))
        runs[0]['environment']='local';self.assertTrue(release_reasons(es,runs,control,at))
        runs[0]['environment']='github_actions';checks.pop()
        self.assertTrue(release_reasons(es,runs,control,at))

    def test_freshness_uses_success_not_last_attempt_and_local_schedule_date(self):
        at=datetime(2026,10,16,12,tzinfo=timezone.utc)
        self.assertTrue(stale({'checked_at':at.isoformat(),'last_success_at':(at-timedelta(hours=37)).isoformat()},at))
        self.assertFalse(stale({'last_success_at':(at-timedelta(hours=35)).isoformat()},at))
        self.assertEqual(daily_key(datetime(2026,10,15,22,tzinfo=timezone.utc)),'2026-10-16')

    def test_reader_cannot_query_unreleased_archive(self):
        store=Mock();store.query.return_value=[{'import_id':'x'}];store.get.return_value=None
        service=CourtService(lambda:{},lambda:{},store_factory=lambda _:store)
        with patch('courts.service.current_access',return_value=Mock(allowed=True,role='reader')):
            with self.assertRaises(PermissionError):service.overview('x')

    def test_court_facts_export_with_existing_editor_contract(self):
        from cloud.draft_rules import create_payload, save_payload, snapshot
        from cloud.exports import render
        p=sudact_document(HTML,URL,self.entity)
        event=news_event(normalize_event(p.events[0],p.cases[0],self.entity,'2026-10-16T00:00:00+00:00'),p.cases[0],self.entity)
        library={'event':{event['id']:event},'period':{'2024-06':{'event_ids':[event['id']],'checks':[]}}}
        draft={'id':'draft','revision':1,'period':'2024-06',**create_payload(library,'2024-06')}
        saved=save_payload(draft,library,'',[{'event_id':event['id'],'included':True,'title':event['title'],'description':event['description']}])
        draft.update(saved);frozen=snapshot(draft,library)
        word,bundle=render(frozen,p.objects,{'teko':{'name':'ТЕКО'}})
        with ZipFile(BytesIO(word)) as z:self.assertIn('Судебные дела',z.read('word/document.xml').decode())
        with ZipFile(BytesIO(bundle)) as z:self.assertIn(event['id'],z.read('manifest.json').decode())


if __name__=='__main__':unittest.main()
