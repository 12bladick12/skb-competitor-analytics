"""Ordinary public browser sessions. Unsupported searches are explicitly incomplete."""
import hashlib
import time
from urllib.parse import urlsplit
from .model import Page, digest
from .parsers import blocked, total, sudact_links, sudact_document

CATALOG={
    'kad':('arbitral','https://kad.arbitr.ru/'),
    'ras':('arbitral','https://ras.arbitr.ru/'),
    'sudrf':('general','https://sudrf.ru/index.php?id=300'),
    'magistrates':('magistrate','https://sudrf.ru/index.php?id=300'),
    'vsrf':('supreme','https://www.vsrf.ru/lk/practice/claims'),
    'ksrf':('constitutional','https://www.ksrf.ru/Decision/extsearch.php'),
    'fssp':('enforcement','https://fssp.gov.ru/iss/ip/'),
    'fedresurs':('bankruptcy','https://fedresurs.ru/'),
    'fns':('identity','https://egrul.nalog.ru/'),
    'sudact_arbitral':('arbitral','https://sudact.ru/arbitral/'),
    'sudact_regular':('general','https://sudact.ru/regular/'),
    'sudact_magistrate':('magistrate','https://sudact.ru/magistrate/')}


class PublicBrowser:
    def __init__(self, channel=None, delay=2):
        self.channel=channel;self.delay=delay;self.blocked_hosts={};self.context=None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.engine=sync_playwright().start()
        try:
            options={'headless':True}
            if self.channel:options['channel']=self.channel
            self.browser=self.engine.chromium.launch(**options)
            self.context=self.browser.new_context(locale='ru-RU')
        except Exception:
            self.engine.stop();raise
        return self

    def __exit__(self,*_):
        if self.context:self.context.close()
        self.browser.close();self.engine.stop()

    def fetch(self, source, entity, cursor=None):
        domain,url=CATALOG[source]
        host=urlsplit(url).hostname
        result=Page(source,domain,entity['id'],url)
        if host in self.blocked_hosts:
            result.status=self.blocked_hosts[host];result.reason='Повторные запросы остановлены после ограничения доступа.'
            return result
        page=self.context.new_page()
        page.set_default_timeout(10000);page.set_default_navigation_timeout(25000)
        try:
            time.sleep(self.delay)
            if source.startswith('sudact_'):
                return self._sudact(page,source,entity,cursor or {})
            response=page.goto(url,wait_until='domcontentloaded')
            page.wait_for_timeout(1000)
            body=page.locator('body').inner_text()
            problem=blocked(body,response.status if response else 500)
            if not problem:
                if source=='vsrf':
                    page.get_by_placeholder('ФИО (наименование)',exact=True).fill(entity['names'][-1])
                    page.get_by_text('Найти',exact=True).click()
                    page.get_by_text('Результаты поиска',exact=True).wait_for(timeout=20000)
                    page.wait_for_timeout(1000)
                elif source=='kad':
                    close=page.locator('.js-promo_notification-popup-close')
                    if close.is_visible():close.click()
                    field=page.locator('#sug-participants textarea');field.fill(entity['inn']);field.press('Tab')
                    with page.expect_response(lambda r:'/Kad/SearchInstances' in r.url,timeout=20000) as waiting:
                        page.locator('#b-form-submit').click()
                    problem=blocked('',waiting.value.status)
                elif source=='fssp':
                    page.locator('label[for=r5]').click();page.locator('#input10').fill(entity['inn'])
                    page.locator('#btn-sbm').click();page.wait_for_timeout(3500)
            body=page.locator('body').inner_text()
            problem=problem or blocked(body)
            result.status=problem or 'partial'
            result.reason=('Источник ограничил автоматический доступ.' if problem else
                           'Доступ проверен; полнота поиска и карточек не подтверждена.')
            if problem in ('challenge','access_denied','rate_limited'):self.blocked_hosts[host]=problem
            html=page.content().encode();proof=hashlib.sha256(html).hexdigest();result.objects[proof]=html
            # Search observations stay candidates until identity AND role are verified.
            if source in ('vsrf','kad') and not problem:
                result.candidates.append({'id':digest([source,entity['id'],body]),'entity_id':entity['id'],
                    'url':page.url,'title':'Результат поиска '+entity['name'],'text':body,
                    'reported_total':total(body,supreme=True) if source=='vsrf' else None,
                    'reason':'Требуется проверка карточек, ролей и всех страниц.','evidence_ids':[proof]})
            return result
        except Exception as exc:
            result.status='error';result.reason='Не удалось завершить поиск: '+type(exc).__name__
            try:
                html=page.content().encode();proof=hashlib.sha256(html).hexdigest();result.objects[proof]=html
                problem=blocked(page.locator('body').inner_text())
                if problem:
                    result.status=problem;self.blocked_hosts[host]=problem
            except Exception:pass
            return result
        finally:page.close()

    def _sudact(self,page,source,entity,cursor):
        domain,base=CATALOG[source];section=source.removeprefix('sudact_')
        queries=list(dict.fromkeys([entity['inn'],*entity['names']]))
        index=cursor.get('query_index',0)
        result=Page(source,domain,entity['id'],base)
        pending=cursor.get('pending',[])
        target=pending[0]['url'] if pending else cursor.get('page_url') or base
        parsed=urlsplit(target)
        if parsed.scheme!='https' or parsed.hostname!='sudact.ru' or not parsed.path.startswith('/'+section+'/'):
            raise ValueError('Недопустимый адрес продолжения поиска.')
        response=page.goto(target,wait_until='domcontentloaded')
        body=page.locator('body').inner_text()
        problem=blocked(body,response.status if response else 500)
        if not problem and not pending and not cursor.get('page_url'):
            page.locator('#id_'+section+'-txt').fill(queries[index])
            with page.expect_navigation(wait_until='domcontentloaded'):
                page.locator('input[type=submit]').click()
        if not problem and not pending:
            page.wait_for_function("!document.body.innerText.includes('Идет поиск')",timeout=35000)
        body=page.locator('body').inner_text();problem=problem or blocked(body)
        html=page.content();proof=hashlib.sha256(html.encode()).hexdigest()
        result.objects[proof]=html.encode();result.url=page.url
        if problem:
            result.status=problem;result.reason='Источник ограничил автоматический доступ.'
            self.blocked_hosts['sudact.ru']=problem;return result
        if pending:
            result=sudact_document(html,target,entity,domain)
            remaining=pending[1:];after=cursor.get('after_url')
        else:
            remaining,after=sudact_links(html,page.url,section)
            count=total(body)
            if count is None or (count>0 and not remaining):
                result.reason='Результаты не распознаны или список документов отсутствует.'
                result.next_cursor=cursor or {'query_index':index};return result
        if remaining:
            result.next_cursor={'query_index':index,'pending':remaining,'after_url':after}
        elif after:
            result.next_cursor={'query_index':index,'page_url':after}
        elif index+1<len(queries):
            result.next_cursor={'query_index':index+1}
        result.reason=result.reason or 'Обход опубликованных копий; полнота первичных реестров не подтверждена.'
        return result
