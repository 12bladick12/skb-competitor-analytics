import json
import unittest
from unittest.mock import Mock
from courts.registry import entities
from courts.supreme_public import fetch_supreme


def card(number):
    return f'''<div class="CaseStyle_case_item__test">
      <div class="CaseStyle_case_titleRow__test"><a href="https://kad.arbitr.ru/">{number}</a></div>
      <div class="RowElement_container__test"><div class="CaseStyle_attribute__test">Входящий номер:</div>
      <div class="CaseStyle_case_value__test">{number}</div></div>
      <div>Участники: Третьи лица: АО НПК ТЕКО</div></div>'''


def response(query,numbers,total,last):
    payload={'query':{'keywords':query},'props':{'pageProps':{'initialItemsData':{
        'content':''.join(card(n) for n in numbers),'totalElements':total,'last':last}}}}
    return Mock(status_code=200,text='<script id="__NEXT_DATA__" type="application/json">'+json.dumps(payload)+'</script>')


class SupremeTests(unittest.TestCase):
    def setUp(self):self.entity=entities()[0]

    def test_traverses_pages_and_keeps_name_matches_unconfirmed(self):
        session=Mock();session.get.side_effect=[response(self.entity['inn'],['A','B'],3,False),
                                                response(self.entity['inn'],['C'],3,True)]
        first=fetch_supreme(self.entity,session=session)
        self.assertEqual(first.next_cursor['page'],1)
        second=fetch_supreme(self.entity,first.next_cursor,session)
        self.assertEqual(second.next_cursor['query_index'],1)
        self.assertTrue(second.diagnostics['query_pages_complete'])
        self.assertEqual(second.diagnostics['observed_unique'],3)
        for page in [first,second]:
            page.validate();self.assertEqual(page.status,'partial');self.assertFalse(page.complete)
            self.assertFalse(page.cases);self.assertFalse(page.events)
            self.assertTrue(all(not c['identity_verified'] for c in page.candidates))

    def test_repeated_page_does_not_loop_or_claim_complete(self):
        session=Mock();session.get.return_value=response(self.entity['inn'],['A'],3,False)
        first=fetch_supreme(self.entity,session=session)
        second=fetch_supreme(self.entity,first.next_cursor,session)
        self.assertEqual(second.next_cursor['query_index'],1)
        self.assertTrue(second.next_cursor['unresolved_queries'])
        self.assertFalse(second.diagnostics['query_pages_complete'])

    def test_missing_cards_and_wrong_query_never_mean_no_cases(self):
        session=Mock();session.get.return_value=response(self.entity['inn'],[],3,True)
        result=fetch_supreme(self.entity,session=session)
        self.assertTrue(result.diagnostics['unresolved_queries']);self.assertFalse(result.complete)
        session.get.return_value=response('other company',[],0,True)
        result=fetch_supreme(self.entity,session=session)
        self.assertEqual(result.status,'error');self.assertFalse(result.complete)

    def test_captcha_and_broken_page_preserve_resume_position(self):
        session=Mock();session.get.return_value=Mock(status_code=403,text='Forbidden')
        result=fetch_supreme(self.entity,session=session)
        self.assertEqual(result.status,'access_denied');self.assertFalse(result.candidates)
        session.get.return_value=Mock(status_code=200,text='<html>Maintenance</html>')
        cursor={'query_index':1,'page':2,'seen_ids':['A']}
        result=fetch_supreme(self.entity,cursor,session)
        self.assertEqual(result.status,'error');self.assertEqual(result.next_cursor,cursor)


if __name__=='__main__':unittest.main()
