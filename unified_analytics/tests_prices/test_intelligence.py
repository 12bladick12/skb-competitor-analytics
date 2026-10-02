from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.intelligence import collect_intelligence, key_for
from src.intelligence_registry import ENTITIES
from src.intelligence_mentions import extract_article, identity, canonical, BraveSearch, ReadingAgent
from src.intelligence_web import Page, PublicWeb, SourceUnavailable, public_url
from src.intelligence_courts import card_links, parse_card, update_case, is_current, validate_observation
from web.facts import month

URL = 'https://publisher.example/news/teko'
BODY = 'НПК ТЕКО представила оптические датчики для промышленной автоматизации. В материале описаны выбор и монтаж датчиков на производственной линии.'
HTML = '<html><head><meta property="article:published_time" content="2026-09-10T10:00:00+03:00"></head><body><article><h1>Новая серия ТЕКО</h1><p>'+BODY+'</p></article></body></html>'
CARD_URL='https://kad.arbitr.ru/Card/11111111-2222-3333-4444-555555555555'


def page(text=HTML, url=URL):
    return Page(url,text.encode('utf-8'),'text/html',text,200)


class FakeSearch:
    def search(self,*args):
        raise SourceUnavailable('search_api_not_configured')
    def close(self): pass


class FakeWeb:
    def fetch(self,url): return page(url=url)
    def court_search(self,*args): raise SourceUnavailable('access_restricted_http_451',451)
    def close(self): pass


class MentionTests(unittest.TestCase):
    def test_generic_sensor_or_other_teko_is_not_confirmed(self):
        self.assertEqual(identity('Новый сенсор камеры телефона',ENTITIES['sensor']),'ambiguous')
        self.assertEqual(identity('ООО ТЕКО выпускает датчики пожарной сигнализации',ENTITIES['teko']),'ambiguous')
        self.assertNotEqual(identity(BODY,ENTITIES['teko']),'ambiguous')
        self.assertEqual(identity('ИНН 7448197440',ENTITIES['beskonta']),'exact_inn')

    def test_domains_must_be_whole_names(self):
        self.assertEqual(identity('fake-sensor-com.ru.evil.org',ENTITIES['sensor']),'ambiguous')

    def test_article_date_comes_from_publisher(self):
        value=extract_article(page())
        self.assertEqual(value['published_at'],'2026-09-10')
        self.assertIn('page_metadata',value['date_evidence'])

    def test_conflicting_dates_are_not_resolved_arbitrarily(self):
        html=HTML.replace('</head>','<meta itemprop="datePublished" content="2026-09-11"></head>')
        self.assertIsNone(extract_article(page(html))['published_at'])

    def test_missing_date_is_not_detection_date(self):
        self.assertIsNone(extract_article(page('<article><h1>ТЕКО</h1>'+BODY+'</article>'))['published_at'])

    def test_pdf_creation_date_is_not_a_publication_date(self):
        from pypdf import PdfWriter
        writer=PdfWriter(); writer.add_blank_page(width=200,height=200)
        writer.add_metadata({'/CreationDate':'D:20260910000000'})
        stream=BytesIO(); writer.write(stream)
        value=extract_article(Page('https://publisher.example/journal.pdf',stream.getvalue(),'application/pdf','',200))
        self.assertEqual(value['format'],'pdf')
        self.assertEqual(len(value['pages']),1)
        self.assertIsNone(value['published_at'])

    def test_canonical_removes_only_tracking_parameters(self):
        self.assertEqual(canonical(URL+'?id=2&utm_source=a#x'),URL+'?id=2')

    def test_model_cannot_introduce_unsupported_organization(self):
        agent=ReadingAgent({'openai_api_key':'test-key','model':'test'})
        response=Mock(status_code=200)
        response.json.return_value={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps({'excerpts':[BODY],'organizations':[{'name':'Invented','quote':BODY}]})}]}]}
        with patch('src.intelligence_mentions.requests.post',return_value=response):
            self.assertEqual(agent.read(BODY,ENTITIES['teko'])['state'],'error')

    def test_search_key_missing_never_scrapes_search_site(self):
        with patch.dict('os.environ',{'BRAVE_SEARCH_API_KEY':''}):
            search=BraveSearch(session=Mock())
            with self.assertRaises(SourceUnavailable):search.search('x','2026-01-01','2026-10-01')
            search.session.get.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_private_and_redirect_targets_rejected(self):
        with patch('src.intelligence_web.socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(SourceUnavailable):public_url('https://example.com/internal')
        with self.assertRaises(SourceUnavailable):public_url('https://user:pass@example.com/')

    def test_robots_allowed_page_does_not_keep_initial_deny_marker(self):
        web=PublicWeb(session=Mock())
        with patch.object(web,'_request',return_value=page('User-agent: *\nAllow: /','https://publisher.example/robots.txt')):
            web._policy(URL)
            self.assertTrue(web.robots['https://publisher.example'].can_fetch('*',URL))

    def test_robots_denied_page_is_not_requested(self):
        web=PublicWeb(session=Mock())
        with patch.object(web,'_request',return_value=page('User-agent: *\nDisallow: /news/')) as request:
            with self.assertRaisesRegex(SourceUnavailable,'robots_denied'):web._policy(URL)
            self.assertEqual(request.call_count,1)


class CourtTests(unittest.TestCase):
    def test_unrecognized_search_not_zero_cases(self):
        with self.assertRaises(SourceUnavailable):card_links('<h1>Access denied</h1>')

    def test_only_official_card_links(self):
        self.assertEqual(card_links('<a href="'+CARD_URL+'">А76-123/2026</a>'),[CARD_URL])
        with self.assertRaises(SourceUnavailable):card_links('<a href="https://evil.example/Card/11111111-2222-3333-4444-555555555555">дело</a>')

    def test_legal_identity_requires_exact_inn(self):
        html='<h1>А76-123/2026</h1><p>ТЕКО участвует в деле</p>'
        with self.assertRaisesRegex(SourceUnavailable,'inn_unconfirmed'):parse_card(page(html,CARD_URL),ENTITIES['teko'])

    def test_historical_decision_does_not_prove_current_case_closed(self):
        html='<h1>А76-123/2026</h1><p>АО НПК ТЕКО ИНН 7453019186. Решение от 10.09.2026.</p>'
        value=parse_card(page(html,CARD_URL),ENTITIES['teko'])
        self.assertEqual(value['stage'],'unknown')
        self.assertIsNone(value['claimed_amount'])

    def test_failures_and_old_observations_are_not_current(self):
        value=dict(stage='in_progress',needs_review=False,access_state='success',checked_at='2026-09-30T12:00:00+00:00')
        now=datetime(2026,10,1,tzinfo=timezone.utc)
        self.assertTrue(is_current(value,now))
        self.assertIsNone(is_current({**value,'access_state':'unavailable'},now))
        self.assertIsNone(is_current({**value,'checked_at':'2026-08-01T00:00:00+00:00'},now))

    def test_recheck_does_not_create_revision_for_timestamp_only(self):
        value={'case_number':'А76-123/2026','stage':'unknown'}
        first=update_case(None,value,'2026-09-01T00:00:00+00:00')
        second=update_case(first,value,'2026-09-02T00:00:00+00:00')
        self.assertEqual(len(second['history']),1)
        changed=update_case(second,{**value,'stage':'closed'},'2026-09-03T00:00:00+00:00')
        self.assertEqual(len(changed['history']),2)

    def test_unreviewed_import_rejected(self):
        with self.assertRaises(ValueError):validate_observation({'url':CARD_URL,'case_number':'А76-123/2026',
            'competitor_inn':'7453019186','original_text':'А76-123/2026; АО НПК ТЕКО; ИНН 7453019186; рассматривается'})


class IntegrationTests(unittest.TestCase):
    def test_collection_keeps_search_gap_and_case_block_separate_from_news(self):
        with tempfile.TemporaryDirectory() as folder:
            result=collect_intelligence({},month('2026-09')[1],Path(folder),web=FakeWeb(),search=FakeSearch(),
                seeds=[{'competitor_code':'teko','url':URL}])
            self.assertEqual(len(result['events']),1)
            self.assertEqual(result['events'][0]['kind'],'mentions')
            self.assertEqual(result['status'],'partial')
            self.assertEqual(sum(c['kind']=='litigation' and c['status']=='error' for c in result['checks']),4)
            self.assertFalse(any(r['kind']=='intel_case' for r in result['records']))

    def test_existing_case_survives_source_failure(self):
        old={'id':'old','competitor_code':'teko','url':CARD_URL,'stage':'in_progress','history':[{'version':'v1'}]}
        with tempfile.TemporaryDirectory() as folder:
            result=collect_intelligence({'intel_case':{'old':old}},month('2026-09')[1],Path(folder),web=FakeWeb(),search=FakeSearch())
        case=next(r['payload'] for r in result['records'] if r['kind']=='intel_case')
        self.assertEqual(case['history'],old['history'])
        self.assertEqual(case['stage'],'in_progress')
        self.assertEqual(case['access_state'],'not_checked')

    def test_duplicate_content_grouped_without_second_news_event(self):
        with tempfile.TemporaryDirectory() as folder:
            result=collect_intelligence({},month('2026-09')[1],Path(folder),web=FakeWeb(),search=FakeSearch(),
                seeds=[{'competitor_code':'teko','url':URL},{'competitor_code':'teko','url':URL+'-copy'}])
        self.assertEqual(len(result['events']),1)
        self.assertEqual(sum(r['payload'].get('status')=='duplicate' for r in result['records']),1)

    def test_cloud_roundtrip_adds_event_period_and_portable_proof(self):
        from cloud.collection import collect
        from cloud.library import VISIBLE_KINDS
        library={kind:{} for kind in VISIBLE_KINDS}
        def factory(lib,period,snapshots,progress,config,**kwargs):
            return collect_intelligence(lib,period,snapshots,web=FakeWeb(),search=FakeSearch(),
                seeds=[{'competitor_code':'teko','url':URL}])
        with tempfile.TemporaryDirectory() as folder:
            records,objects,result=collect(library,'2026-09',Path(folder),Mock(),lambda *a,**k:None,
                intelligence_only=True,intelligence_factory=factory)
        event=next(r['payload'] for r in records if r['kind']=='event')
        self.assertEqual(event['kind'],'mentions')
        self.assertTrue(all(i in objects for i in event['evidence_ids']))
        period=next(r['payload'] for r in records if r['kind']=='period')
        self.assertIn(event['id'],period['event_ids'])
        self.assertTrue(any(c['kind']=='litigation' and c['status']=='error' for c in period['checks']))
        self.assertEqual(result['status'],'partial')

    def test_changing_report_period_does_not_revoke_old_confirmed_article(self):
        with tempfile.TemporaryDirectory() as folder:
            first=collect_intelligence({},month('2026-09')[1],Path(folder),web=FakeWeb(),search=FakeSearch(),
                seeds=[{'competitor_code':'teko','url':URL}])
            saved={r['key']:r['payload'] for r in first['records'] if r['kind']=='intel_mention'}
            second=collect_intelligence({'intel_mention':saved},month('2026-10')[1],Path(folder),web=FakeWeb(),search=FakeSearch(),
                seeds=[{'competitor_code':'teko','url':URL}])
        row=next(r['payload'] for r in second['records'] if r['kind']=='intel_mention')
        self.assertEqual(row['status'],'confirmed')
        self.assertFalse(row['in_requested_period'])
        self.assertEqual(row['published_at'],'2026-09-10')

    def test_export_includes_new_sections(self):
        from cloud.exports import render
        from docx import Document
        import hashlib
        raw=HTML.encode('utf-8'); digest=hashlib.sha256(raw).hexdigest()
        event={'id':1,'kind':'mentions','competitor_code':'teko','competitor_name':'ТЕКО',
               'url':URL,'date_label':'10.09.2026','evidence_ids':[digest],'provenance':{'article_url':URL}}
        snapshot={'draft_id':'test','revision':1,'period':'2026-09','conclusions':'',
                  'items':[{'title':'Проверка','description':BODY,'source':event}],
                  'counts':{'mentions':1},'checks':[],'incomplete':False}
        word,_=render(snapshot,{digest:raw},{})
        doc=Document(BytesIO(word))
        text='\n'.join(p.text for p in doc.paragraphs)
        self.assertIn('Внешние упоминания',text)
        self.assertIn('Судебные события',text)
        self.assertIn(BODY,text)


if __name__=='__main__':unittest.main()
