import json
import unittest
import sys
import os
import time
from unittest.mock import Mock, patch
from courts.browser_access import optional_resource, error_code, navigate, wait_for_search_end, wait_for_react_input
from courts.public_forms import fetch_fns, fetch_fssp
from courts.registry import entities
from courts.sources import PublicBrowser
from courts.parsers import blocked
from courts.browser_worker import BoundedBrowser


def isolated_test_worker(connection,options):
    if os.name!='nt':os.setsid()
    from courts.model import Page
    try:
        while True:
            request=connection.recv()
            if request is None:return
            source,entity,cursor=request
            if cursor and cursor.get('hang'):time.sleep(60)
            if cursor and cursor.get('crash'):os._exit(7)
            result=Page(source,'arbitral',entity['id'],'https://kad.arbitr.ru/')
            result.status='challenge' if cursor and cursor.get('challenge') else 'partial'
            connection.send(result)
    finally:connection.close()


def response(text='', payload=None, status=200):
    r=Mock(status_code=status,text=text)
    r.json.return_value=payload
    return r


class AccessTests(unittest.TestCase):
    def setUp(self):self.entity=entities()[0]

    def test_optional_filter_never_blocks_primary_or_challenge_resources(self):
        self.assertTrue(optional_resource('https://vk.com/js/api/openapi.js?169'))
        self.assertTrue(optional_resource('https://mc.yandex.ru/metrika/tag.js'))
        for url in ['https://kad.arbitr.ru/Wasm/api/v1/wasm.js','https://fedresurs.ru/__qrator/validate',
                    'https://fssp.gov.ru/bdip/js/general.js','https://egrul.nalog.ru/js/captcha.js',
                    'https://mc.yandex.ru.attacker.invalid/script.js']:
            self.assertFalse(optional_resource(url))

    def test_empty_200_pravo_challenge_is_not_a_missing_form_or_no_cases(self):
        html='<html><head><script src="/Content/Static/js/pravocaptcha.2026.js"></script></head><body></body></html>'
        self.assertEqual(blocked('',200,html),'challenge')
        self.assertEqual(blocked('Дело А76-1/2026',200,html),'')
        self.assertEqual(blocked('',200,'<script src="/normal.js"></script>'),'')

    def test_navigation_accepts_committed_document_without_waiting_for_trackers(self):
        page=Mock();navigate(page,'https://kad.arbitr.ru/')
        self.assertEqual(page.goto.call_args.kwargs['wait_until'],'commit')
        page.locator.return_value.wait_for.assert_called_once_with(state='attached',timeout=10000)
        self.assertEqual(error_code(Exception('secret=abc net::ERR_CONNECTION_RESET https://host/token')),'ERR_CONNECTION_RESET')

    def test_search_wait_survives_missing_body_then_loading_document(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as engine:
            options={'headless':True}
            if sys.platform=='win32':options['channel']='msedge'
            browser=engine.chromium.launch(**options)
            try:
                page=browser.new_page();page.set_content('<html><body>Previous document</body></html>')
                page.evaluate('''() => {
                    document.body.remove();
                    setTimeout(() => {
                        const body=document.createElement('body');body.innerText='Идет поиск';
                        document.documentElement.appendChild(body);
                        setTimeout(() => body.innerText='Найдено 2 документа',100);
                    },100);
                }''')
                wait_for_search_end(page)
                self.assertEqual(page.locator('body').inner_text(),'Найдено 2 документа')
                page.set_content('<input placeholder="Company">')
                page.evaluate('''() => setTimeout(() => {
                    const input=document.querySelector('input');input.value='';
                    input.__reactProps$test={};
                },200)''')
                wait_for_react_input(page,'Company')
                page.get_by_placeholder('Company').fill('TEKO')
                self.assertEqual(page.get_by_placeholder('Company').input_value(),'TEKO')
            finally:browser.close()

    def test_public_search_dismisses_popup_once_and_reaches_search_endpoint(self):
        html='''<html><head><meta charset="utf-8"></head><body><script>window.jQuery={isReady:true};</script>
            <div id="popup" style="position:fixed;inset:0;background:white;z-index:99">
              <button class="js-promo_notification-popup-close"
                onclick="document.getElementById('popup').remove()">Закрыть</button></div>
            <h2>Участник дела</h2><div id="sug-participants"><textarea></textarea></div>
            <button onclick="fetch('/Kad/SearchInstances',{method:'POST'})">Найти</button>
            </body></html>'''
        options={'delay':0}
        if sys.platform=='win32':options['channel']='msedge'
        with PublicBrowser(**options) as browser:
            browser.context.route('https://kad.arbitr.ru/**',lambda route:
                route.fulfill(status=200,content_type='text/html',body=html))
            result=browser.fetch('kad',self.entity)
            self.assertEqual(result.status,'partial',result.diagnostics)
            self.assertEqual(result.diagnostics['stage'],'search_observed')
            self.assertEqual(result.diagnostics['search_http_status'],200)

    @patch('courts.public_forms.time.sleep')
    def test_fns_matches_both_identifiers_and_removes_transient_tokens(self,_):
        session=Mock()
        session.post.return_value=response(payload={'t':'ABCD','captchaRequired':False})
        session.get.side_effect=[response('<form id="frmSearch" action="." method="post"></form>'),
            response(payload={'status':'wait'}),response(payload={'rows':[
                {'i':self.entity['inn'],'o':self.entity['ogrn'],'n':self.entity['name'],'t':'DOWNLOAD_SECRET'},
                {'i':self.entity['inn'],'o':'OTHER','n':'Wrong entity'}]})]
        result=fetch_fns(self.entity,session);result.validate()
        self.assertEqual([c['identity_verified'] for c in result.candidates],[True,False])
        self.assertFalse(result.complete);self.assertEqual(result.status,'partial')
        self.assertNotIn('DOWNLOAD_SECRET',json.dumps(result.candidates))
        self.assertTrue(all(b'DOWNLOAD_SECRET' not in data for data in result.objects.values()))

    @patch('courts.public_forms.time.sleep')
    def test_fns_challenge_does_not_poll_results_or_claim_zero(self,_):
        session=Mock();session.get.return_value=response('<form id="frmSearch" action="." method="post"></form>')
        session.post.return_value=response(payload={'t':'ABCD','captchaRequired':True})
        result=fetch_fns(self.entity,session)
        self.assertEqual(result.status,'challenge');self.assertFalse(result.complete)
        self.assertEqual(session.get.call_count,1);self.assertFalse(result.candidates)

    def test_changed_form_target_is_not_followed(self):
        session=Mock();session.get.return_value=response('<form id="frmSearch" action="https://other.invalid/" method="post"></form>')
        result=fetch_fns(self.entity,session)
        self.assertEqual(result.status,'error');session.post.assert_not_called()

    def test_fssp_jsonp_captcha_is_not_an_empty_search(self):
        session=Mock();session.get.side_effect=[response('<form id="ip_form" action="https://is-go.fssp.gov.ru/ajax_search" method="get"></form>'),
            response('('+json.dumps({'data':'<h2>Введите код с картинки:</h2>'})+');')]
        result=fetch_fssp(self.entity,session);result.validate()
        self.assertEqual(result.status,'challenge');self.assertFalse(result.complete)
        self.assertFalse(result.enforcement)

    @patch('courts.sources.time.sleep')
    @patch('courts.public_forms.fetch_fssp')
    def test_challenge_stops_subsequent_company_requests(self,fetch,_):
        from courts.model import Page
        fetch.return_value=Page('fssp','enforcement',self.entity['id'],'https://fssp.gov.ru/iss/ip/',status='challenge')
        browser=PublicBrowser(delay=0)
        first=browser.fetch('fssp',self.entity);second=browser.fetch('fssp',entities()[1])
        self.assertEqual(first.status,second.status);self.assertEqual(fetch.call_count,1)

    def test_hung_worker_is_stopped_and_another_source_can_continue(self):
        with BoundedBrowser(timeout=5,worker_target=isolated_test_worker) as browser:
            self.assertEqual(browser.fetch('kad',self.entity).status,'partial')
            process=browser.process;browser.timeout=.3;started=time.monotonic()
            cursor={'hang':True,'query_index':2}
            result=browser.fetch('kad',self.entity,cursor)
            self.assertLess(time.monotonic()-started,12)
            self.assertEqual(result.diagnostics['error'],'SOURCE_DEADLINE')
            self.assertEqual(result.next_cursor,cursor);self.assertFalse(result.complete)
            self.assertIsNone(browser.process)
            browser.timeout=5
            self.assertEqual(browser.fetch('ras',self.entity).status,'partial')
            self.assertIsNot(browser.process,process)

    def test_crashed_worker_preserves_cursor_and_access_denials_survive_restart(self):
        with BoundedBrowser(timeout=5,worker_target=isolated_test_worker) as browser:
            self.assertEqual(browser.fetch('kad',self.entity,{'challenge':True}).status,'challenge')
            cursor={'crash':True,'query_index':2}
            result=browser.fetch('ras',self.entity,cursor)
            self.assertEqual(result.status,'error');self.assertEqual(result.next_cursor,cursor)
            self.assertEqual(browser.fetch('kad',self.entity).status,'challenge')
            self.assertIsNone(browser.process)
            self.assertEqual(browser.fetch('ras',self.entity).status,'partial')

    @patch('courts.sources.wait_for_search_end')
    @patch('courts.sources.navigate')
    def test_inconsistent_search_advances_alias_and_retains_gap_to_end_of_pass(self,navigate,_):
        page=Mock();page.url='https://sudact.ru/regular/'
        page.locator.return_value.inner_text.return_value='Найден 1 документ. Список отсутствует.'
        page.content.return_value='<html><body>Найден 1 документ. Список отсутствует.</body></html>'
        page.expect_navigation.return_value.__enter__=Mock()
        page.expect_navigation.return_value.__exit__=Mock()
        navigate.return_value.status=200
        browser=PublicBrowser(delay=0)
        first=browser._sudact(page,'sudact_regular',self.entity,{})
        self.assertEqual(first.next_cursor['query_index'],1)
        self.assertEqual(len(first.next_cursor['unresolved_queries']),1)
        page.locator.return_value.inner_text.return_value='Документы не найдены'
        page.content.return_value='<html><body>Документы не найдены</body></html>'
        cursor=first.next_cursor
        for _ in range(len(self.entity['names'])+1):
            result=browser._sudact(page,'sudact_regular',self.entity,cursor)
            self.assertEqual(result.status,'partial');self.assertFalse(result.complete)
            self.assertEqual(len(result.diagnostics['unresolved_queries']),1)
            cursor=result.next_cursor
            if cursor is None:break
        self.assertIsNone(cursor)


if __name__=='__main__':unittest.main()
