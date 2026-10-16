import json
import unittest
from unittest.mock import Mock, patch
from courts.browser_access import optional_resource, error_code, navigate
from courts.public_forms import fetch_fns, fetch_fssp
from courts.registry import entities
from courts.sources import PublicBrowser


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

    def test_navigation_accepts_committed_document_without_waiting_for_trackers(self):
        page=Mock();navigate(page,'https://kad.arbitr.ru/')
        self.assertEqual(page.goto.call_args.kwargs['wait_until'],'commit')
        page.locator.return_value.wait_for.assert_called_once_with(state='attached',timeout=10000)
        self.assertEqual(error_code(Exception('secret=abc net::ERR_CONNECTION_RESET https://host/token')),'ERR_CONNECTION_RESET')

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


if __name__=='__main__':unittest.main()
