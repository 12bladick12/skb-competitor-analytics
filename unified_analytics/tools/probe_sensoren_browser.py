"""Bounded, read-only comparison of HTTP and ordinary Chromium on Sensoren.

Does not alter the production queue, user profiles or database. Server cookies
belong to a fresh anonymous context; no cookie values are manually injected.
Every browser request obeys the current robots policy and approved host.
"""
from pathlib import Path
import argparse
import csv
import json
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from price_monitor.transport import SourceClient,FetchError,challenge
from price_monitor.details import parse_catalog_product
from price_monitor.models import utcnow
from price_monitor.sources import validate_url
from price_monitor.transport import check_public_host

ROOT=Path(__file__).resolve().parents[1]
URL='https://sensoren.ru/product/datchik_potoka_ifm_electronic_si5000/'

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-http',action='store_true',help='Explicitly enable the optional six-card HTTP session experiment')
    args=parser.parse_args()
    out=ROOT/'data'/'sensoren_options_20260929'
    out.mkdir(parents=True,exist_ok=True)
    report={'checked_at':utcnow(),'url':URL,'browser':'ordinary Chromium, clean context',
            'cookies_transferred':False,'production_changed':False}
    client=SourceClient('sensoren',delay=3)
    try:
        client.policy(URL)
        policy=client.policies['sensoren.ru']
        started=time.monotonic()
        try:
            url,status,body=client.fetch(URL)
            results,reason=parse_catalog_product('sensoren',body,url,['ifm'])
            report['http']={'status':status,'challenge':challenge(body),'products':len(results),'reason':reason}
        except FetchError as exc:
            report['http']={'status':exc.http_status,'outcome':exc.status,'reason':str(exc)}
        report['http']['seconds']=round(time.monotonic()-started,2)
        print(json.dumps({'http':report['http']},ensure_ascii=False),flush=True)
        client.wait(3)
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=True)
            context=browser.new_context(locale='ru-RU',service_workers='block',accept_downloads=False)
            page=context.new_page()
            documents=[];denied=[];request_count=0
            started=time.monotonic()
            def route_request(route):
                nonlocal request_count
                request=route.request;parts=urlsplit(request.url)
                request_count+=1
                reason=''
                if parts.scheme!='https' or parts.hostname!='sensoren.ru' or parts.port not in (None,443):reason='outside approved origin'
                elif request.method!='GET':reason='read-only probe'
                elif not policy.allows(request.url):reason='robots.txt'
                elif request_count>80 or time.monotonic()-started>25:reason='probe budget'
                if reason:
                    if request.is_navigation_request():denied.append({'url':request.url,'reason':reason})
                    route.abort();return
                route.continue_()
            def response_received(response):
                if response.request.is_navigation_request():
                    documents.append({'url':response.url,'status':response.status})
            context.route('**/*',route_request)
            page.on('response',response_received)
            error=''
            try:
                page.goto(URL,wait_until='domcontentloaded',timeout=20000)
                page.locator('.product-info').wait_for(timeout=5000)
            except Exception as exc:error=type(exc).__name__
            initial={'documents':list(documents),'blocked_navigations':list(denied),
                     'error':error,'cookie_names':[c['name'] for c in context.cookies()]}
            # Inspect one additional permitted canonical navigation in the same
            # anonymous browser session. Do not replay a disallowed query URL,
            # inject cookies, solve a CAPTCHA or modify the browser fingerprint.
            if denied and any(c['name']=='RCPC' for c in context.cookies()):
                page.wait_for_timeout(3000)
                try:
                    page.goto(URL,wait_until='domcontentloaded',timeout=15000)
                    page.locator('.product-info').wait_for(timeout=3000)
                    error=''
                except Exception as exc:error=type(exc).__name__
            report['chromium_initial']=initial
            body=page.content()
            final_url=page.url
            results,reason=parse_catalog_product('sensoren',body,URL,['ifm'])
            report['chromium']={'documents':list(documents),'blocked_navigations':list(denied),
                'final_url':final_url,'error':error,'products':len(results),'reason':reason,
                'challenge':challenge(body),'cookie_names':[c['name'] for c in context.cookies()],
                'seconds':round(time.monotonic()-started,2),'request_count':request_count,
                'observations':[{'article':r.article,'status':o.status,'price':o.price,
                    'attributes':len(json.loads(o.details_json).get('attributes',[]))} for r,o in results]}
            if results:(out/'browser_product.html').write_text(body,encoding='utf-8')
            page.screenshot(path=str(out/'browser_probe.png'))
            report['session_http']=[]
            if results and args.session_http:
                examples=list(csv.DictReader((ROOT/'examples/tasks.csv').open(encoding='utf-8-sig'),delimiter=';'))
                # Keep this diagnostic bounded even if the example file grows.
                for rule in [r for r in examples if r['source']=='sensoren'][:6]:
                    target=rule['product_url']
                    validate_url('sensoren',target)
                    check_public_host(urlsplit(target).hostname)
                    if not policy.allows(target):continue
                    page.wait_for_timeout(3000)
                    request_started=time.monotonic()
                    response=context.request.get(target,max_redirects=0,timeout=20000)
                    content=response.text()
                    status=initial_status=response.status
                    response.dispose()
                    transport='session_http';browser_documents=[]
                    if status==503 and 'document.cookie' in content and 'RCPC=' in content:
                        # The user explicitly authorized this six-card session
                        # experiment. Use ordinary JS in the existing browser;
                        # disallowed navigation remains blocked by route_request.
                        transport='browser';before=len(documents)
                        for attempt in range(2):
                            page.wait_for_timeout(3000)
                            started=time.monotonic();request_count=0
                            try:
                                page.goto(target,wait_until='domcontentloaded',timeout=15000)
                                page.locator('.product-info').wait_for(timeout=3000)
                            except Exception:pass
                            content=page.content()
                            browser_documents=documents[before:]
                            status=next((d['status'] for d in reversed(browser_documents) if d['url']==target),503)
                            if page.url==target and status==200 and not challenge(content):break
                            if not any(d['reason']=='robots.txt' and d['url'].startswith(target+'?attempt=') for d in denied):break
                    parsed,reason=parse_catalog_product('sensoren',content,target,[rule['manufacturer']]) if status==200 and not challenge(content) else ([], 'unavailable')
                    item={'manufacturer':rule['manufacturer'],'article':rule['article'],
                        'initial_http':initial_status,'http':status,'transport':transport,'browser_documents':browser_documents,
                        'challenge':challenge(content),'products':len(parsed),
                        'reason':reason,'seconds':round(time.monotonic()-request_started,2),
                        'observations':[{'article':r.article,'status':o.status,'price':o.price,
                            'attributes':len(json.loads(o.details_json).get('attributes',[]))} for r,o in parsed]}
                    report['session_http'].append(item)
                    print(json.dumps({'session_http':item},ensure_ascii=False),flush=True)
                    if item['challenge'] or item['http'] in (401,403,429,503):break
            browser.close()
        (out/'probe.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report['chromium'],ensure_ascii=False),flush=True)
    finally:client.close()

if __name__=='__main__':main()
