"""Public FNS/FSSP website forms using the same unauthenticated HTTP requests."""
import hashlib
import html
import json
import time
from urllib.parse import urljoin, urlsplit
from bs4 import BeautifulSoup
import requests
from .model import Page, digest
from .parsers import blocked
from .browser_access import error_code


def save_evidence(page, text):
    body=text.encode('utf-8');key=hashlib.sha256(body).hexdigest()
    page.objects[key]=body
    return key


def json_evidence(page, value):
    return save_evidence(page,'<!doctype html><meta charset="utf-8"><pre>'+html.escape(
        json.dumps(value,ensure_ascii=False,sort_keys=True))+'</pre>')


def form_request(session, url, selector, fields, allowed_host):
    landing=session.get(url,timeout=(10,25))
    landing.encoding='utf-8'
    problem=blocked(BeautifulSoup(landing.text,'html.parser').get_text(' ',strip=True),landing.status_code)
    if problem:return landing,problem
    form=BeautifulSoup(landing.text,'html.parser').select_one(selector)
    if form is None:raise ValueError('form_missing')
    target=urljoin(url,form.get('action') or url)
    if urlsplit(target).hostname!=allowed_host or urlsplit(target).scheme!='https':
        raise ValueError('form_target_changed')
    values={node['name']:node.get('value','') for node in form.select('input[type=hidden][name]')}
    values.update(fields)
    if form.get('method','get').lower()=='post':
        response=session.post(target,data=values,timeout=(10,30))
    else:
        response=session.get(target,params=values,timeout=(10,30))
    response.encoding='utf-8'
    return response,blocked('',response.status_code)


def fetch_fns(entity, session=None):
    result=Page('fns','identity',entity['id'],'https://egrul.nalog.ru/index.html')
    owned=session is None;session=session or requests.Session();stage='form';started=time.monotonic()
    try:
        response,problem=form_request(session,result.url,'#frmSearch',
            {'query':entity['inn'],'region':''},'egrul.nalog.ru')
        result.diagnostics['http_status']=response.status_code
        if problem:
            result.status=problem;result.reason='ФНС ограничила доступ к публичной форме.'
            save_evidence(result,response.text);return result
        payload=response.json()
        if payload.get('captchaRequired') not in (None,False,'false',0):
            result.status='challenge';result.reason='ФНС запросила CAPTCHA.'
            json_evidence(result,{'captchaRequired':True});return result
        token=payload.get('t')
        if not isinstance(token,str) or not token.isalnum():raise ValueError('search_token_missing')
        stage='search_response'
        for attempt in range(5):
            time.sleep(1)
            response=session.get('https://egrul.nalog.ru/search-result/'+token,timeout=(10,25))
            result.diagnostics['search_http_status']=response.status_code
            problem=blocked('',response.status_code)
            if problem:
                result.status=problem;result.reason='Не получен результат поиска ФНС.'
                save_evidence(result,response.text);return result
            payload=response.json()
            if payload.get('status')!='wait':break
        rows=payload.get('rows')
        if not isinstance(rows,list):raise ValueError('search_result_not_ready')
        # Download/session tokens are not identity facts and never enter candidates.
        clean=[{k:v for k,v in row.items() if k in ('i','o','n','c','a','p','r','e','k','rn','cnt','pg','tot')}
               for row in rows]
        proof=json_evidence(result,{'query_inn':entity['inn'],'rows':clean})
        matched=[row for row in clean if row.get('i')==entity['inn'] and row.get('o')==entity['ogrn']]
        result.diagnostics.update(stage='search_observed',reported_rows=len(clean),identity_matches=len(matched))
        for row in clean:
            exact=row in matched
            result.candidates.append({'id':digest(['fns',entity['id'],row.get('o'),row.get('i')]),
                'entity_id':entity['id'],'url':result.url,'title':row.get('n') or row.get('c') or 'Запись ФНС',
                'type':'registry_identity','identity_verified':exact,'registry_fields':row,
                'reason':('ИНН и ОГРН совпали. История названий и связь с брендом требуют отдельной проверки.'
                          if exact else 'ИНН/ОГРН не совпали с реестром наблюдения.'),'evidence_ids':[proof]})
        result.reason=('ФНС: ИНН и ОГРН подтверждены текущей поисковой записью; история юрлиц не проверена.'
                       if matched else 'Точная запись ФНС не подтверждена; отсутствие организации не установлено.')
    except Exception as exc:
        result.status='error';result.reason=f'ФНС: этап {stage}, {error_code(exc)}'
        result.diagnostics.update(stage=stage,error=error_code(exc))
    finally:
        result.diagnostics['transport']='public_http_form'
        result.diagnostics['elapsed_seconds']=round(time.monotonic()-started,1)
        if owned:session.close()
    return result


def fetch_fssp(entity, session=None):
    result=Page('fssp','enforcement',entity['id'],'https://fssp.gov.ru/iss/ip/')
    owned=session is None;session=session or requests.Session();started=time.monotonic()
    try:
        response,problem=form_request(session,result.url,'#ip_form',
            {'is[variant]':'5','is[inn]':entity['inn']},'is-go.fssp.gov.ru')
        result.diagnostics['http_status']=response.status_code
        body=response.text
        if not problem:
            # The site's JSONP response is parsed as JSON, never evaluated as JS.
            wrapped=body.strip()
            if wrapped.startswith('(') and wrapped.endswith(');'):wrapped=wrapped[1:-2]
            payload=json.loads(wrapped)
            body=payload.get('data','')
            problem=blocked(BeautifulSoup(body,'html.parser').get_text(' ',strip=True))
        save_evidence(result,body)
        result.status=problem or 'partial'
        result.reason=('ФССП запросила CAPTCHA; результат поиска не получен.' if problem=='challenge' else
                       'Ответ публичной формы ФССП получен; полнота и участники не подтверждены.' if not problem else
                       'ФССП ограничила доступ к публичной форме.')
        result.diagnostics['stage']='search_response'
    except Exception as exc:
        result.status='error';result.reason='ФССП: публичная форма, '+error_code(exc)
        result.diagnostics.update(stage='form',error=error_code(exc))
    finally:
        result.diagnostics['transport']='public_http_form'
        result.diagnostics['elapsed_seconds']=round(time.monotonic()-started,1)
        if owned:session.close()
    return result
