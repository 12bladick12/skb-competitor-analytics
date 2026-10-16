"""Server-rendered public Supreme Court search pages; no API account required.

Names and published roles are discovery evidence. Without verified identifiers,
these records remain candidates and never become confirmed competitor cases.
"""
import json
import time
from urllib.parse import urlencode
from bs4 import BeautifulSoup
import requests
from .browser_access import error_code
from .model import Page, digest, safe_url
from .parsers import blocked
from .public_forms import save_evidence

BASE='https://www.vsrf.ru/lk/practice/claims'


def fetch_supreme(entity,cursor=None,session=None):
    cursor=cursor or {};index=int(cursor.get('query_index',0));number=int(cursor.get('page',0))
    queries=list(dict.fromkeys([entity['inn'],*entity['names']]))
    if index<0 or index>=len(queries) or number<0:raise ValueError('Invalid search cursor')
    query=queries[index]
    params={'registerDateExact':'false','considerationDateExact':'false','numberExact':'true',
            'keywords':query,'showSubscriptions':'false','page':number}
    result=Page('vsrf_http','supreme',entity['id'],BASE+'?'+urlencode(params))
    owned=session is None;session=session or requests.Session();started=time.monotonic()
    gaps=list(cursor.get('unresolved_queries',[]))
    result.diagnostics={'stage':'http_search','transport':'public_search_page','query_index':index,'page':number}
    try:
        response=session.get(result.url,timeout=(10,25));response.encoding='utf-8'
        result.diagnostics['http_status']=response.status_code
        proof=save_evidence(result,response.text)
        soup=BeautifulSoup(response.text,'html.parser')
        problem=blocked(soup.get_text(' ',strip=True),response.status_code,response.text)
        if problem:
            result.status=problem;result.reason='ВС РФ: поисковая страница недоступна.';return result
        result.diagnostics['stage']='parse_results'
        data=json.loads(soup.select_one('script#__NEXT_DATA__').get_text())
        if data.get('query',{}).get('keywords')!=query:raise ValueError('Search query not acknowledged')
        items=data['props']['pageProps']['initialItemsData']
        total=items['totalElements'];last=items['last']
        if type(total) is not int or total<0 or type(last) is not bool:raise ValueError('Invalid result metadata')
        fragment=BeautifulSoup(items['content'],'html.parser')
        cards=fragment.select('[class*="CaseStyle_case_item__"]')
        for card in cards:
            fields={}
            for row in card.select('[class*="RowElement_container__"]'):
                label=row.select_one('[class*="attribute__"]');value=row.select_one('[class*="case_value__"]')
                if label and value:fields[label.get_text(' ',strip=True).rstrip(':')]=value.get_text(' ',strip=True)
            title=card.select_one('[class*="case_titleRow__"] a')
            title=title.get_text(' ',strip=True) if title else fields.get('Входящий номер','')
            if not title or not fields.get('Входящий номер'):raise ValueError('Unrecognized case card')
            links=list(dict.fromkeys(a['href'] for a in card.select('a[href]') if safe_url(a['href'])))
            key=digest(['vsrf_http',entity['id'],fields['Входящий номер'],
                        fields.get('Дата поступления'),fields.get('Номер дела 1-й инстанции')])
            result.candidates.append({'id':key,'entity_id':entity['id'],'url':result.url,
                'title':title,'type':'court_search_match','identity_verified':False,
                'published_fields':fields,'related_urls':links,'text':card.get_text(' ',strip=True),
                'reason':'Совпадение поиска по названию/ИНН. Принадлежность организации и роль требуют проверки.',
                'evidence_ids':[proof]})
        ids=[c['id'] for c in result.candidates];signature=digest(sorted(ids))
        seen_pages=list(cursor.get('seen_pages',[]));seen_ids=list(dict.fromkeys([*cursor.get('seen_ids',[]),*ids]))
        issue=None
        if signature in seen_pages:issue='Повторилась страница выдачи.'
        elif total and not cards:issue='Счётчик положительный, список отсутствует.'
        elif not last and not cards:issue='Пагинация не продвигается.'
        elif last and len(seen_ids)!=total:issue='Количество карточек не совпало со счётчиком выдачи.'
        result.diagnostics.update(stage='search_observed',reported_total=total,observed_cards=len(cards),
                                  observed_unique=len(seen_ids),last_page=last,query_pages_complete=last and not issue)
        if issue:gaps.append({'query_index':index,'page':number,'reason':issue,'evidence_id':proof})
        if not issue and not last:
            result.next_cursor={'query_index':index,'page':number+1,'seen_pages':[*seen_pages,signature],'seen_ids':seen_ids}
        elif index+1<len(queries):result.next_cursor={'query_index':index+1,'page':0}
        if gaps:
            result.diagnostics['unresolved_queries']=gaps
            if result.next_cursor is not None:result.next_cursor['unresolved_queries']=gaps
        result.reason=issue or 'Получены опубликованные карточки ВС РФ; идентификация и полнота истории не подтверждены.'
    except Exception as exc:
        result.status='error';result.reason='ВС РФ: публичная страница поиска, '+error_code(exc)
        result.next_cursor=cursor
        result.diagnostics['error']=error_code(exc)
    finally:
        result.diagnostics['elapsed_seconds']=round(time.monotonic()-started,1)
        if owned:session.close()
    return result
