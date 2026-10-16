"""Ordinary public browser sessions. Unsupported searches are explicitly incomplete."""
import hashlib
import time
from urllib.parse import urlsplit
from .model import Page, digest
from .parsers import blocked, total, sudact_links, sudact_document
from .browser_access import route_optional, error_code, navigate, capture

CATALOG={
    'kad':('arbitral','https://kad.arbitr.ru/'),
    'mkad':('arbitral','https://mkad.arbitr.ru/'),
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
            self.context.route('**/*',route_optional)
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
        if source in ('fns','fssp'):
            from .public_forms import fetch_fns,fetch_fssp
            time.sleep(self.delay)
            result=(fetch_fns if source=='fns' else fetch_fssp)(entity)
            if result.status in ('challenge','access_denied','rate_limited'):self.blocked_hosts[host]=result.status
            return result
        page=self.context.new_page()
        page.set_default_timeout(15000);page.set_default_navigation_timeout(40000)
        stage='navigation';started=time.monotonic()
        result.diagnostics={'stage':stage}
        def script_error(error):
            errors=result.diagnostics.setdefault('script_errors',[])
            if len(errors)<10:errors.append('WASM_TRAP' if str(error).strip()=='unreachable' else type(error).__name__)
        page.on('pageerror',script_error)
        try:
            time.sleep(self.delay)
            if source.startswith('sudact_'):
                return self._sudact(page,source,entity,cursor or {})
            response=navigate(page,url)
            result.diagnostics['http_status']=response.status if response else None
            page.wait_for_timeout(1000)
            body=page.locator('body').inner_text()
            problem=blocked(body,response.status if response else 500)
            if not problem:
                stage='form_ready'
                if source=='vsrf':
                    page.get_by_placeholder('ФИО (наименование)',exact=True).fill(entity['names'][-1])
                    stage='search_response';page.get_by_text('Найти',exact=True).click()
                    page.get_by_text('Результаты поиска',exact=True).wait_for(timeout=20000)
                    page.wait_for_timeout(1000)
                elif source in ('kad','ras'):
                    page.wait_for_function('window.jQuery && window.jQuery.isReady',timeout=25000)
                    close=page.locator('.js-promo_notification-popup-close')
                    page.add_locator_handler(close,lambda control:control.click())
                    if close.is_visible():close.click()
                    field=page.locator('#sug-participants textarea');field.fill(entity['inn']);field.press('Tab')
                    stage='search_response'
                    endpoint='/Kad/SearchInstances' if source=='kad' else '/Ras/Search'
                    with page.expect_response(lambda r:endpoint.lower() in r.url.lower(),timeout=45000) as waiting:
                        page.get_by_role('button',name='Найти',exact=True).click()
                    problem=blocked('',waiting.value.status)
                    result.diagnostics['search_http_status']=waiting.value.status
                    page.wait_for_timeout(1500)
                elif source=='mkad':
                    page.get_by_placeholder('Название или ИНН',exact=True).fill(entity['inn'])
                    stage='search_response'
                    with page.expect_navigation(wait_until='commit',timeout=45000):
                        page.locator('input[type=submit]').click()
                    page.wait_for_timeout(2000)
            body=page.locator('body').inner_text()
            problem=problem or blocked(body)
            result.status=problem or 'partial'
            result.reason=('Источник ограничил автоматический доступ.' if problem else
                           'Доступ проверен; полнота поиска и карточек не подтверждена.')
            if problem in ('challenge','access_denied','rate_limited'):self.blocked_hosts[host]=problem
            proof=capture(page,result)
            result.diagnostics['stage']='search_observed' if stage=='search_response' else 'page_only'
            if source=='mkad' and not problem and not page.locator('a[href*="/Card/"]').count():
                result.status='error';result.reason='Мобильная форма не вернула распознанный список дел; отсутствие дел не установлено.'
                result.diagnostics['stage']='results_not_recognized'
                return result
            # Search observations stay candidates until identity AND role are verified.
            if source in ('vsrf','kad','ras','mkad') and not problem and stage=='search_response':
                result.candidates.append({'id':digest([source,entity['id'],body]),'entity_id':entity['id'],
                    'url':page.url,'title':'Результат поиска '+entity['name'],'text':body,
                    'reported_total':total(body,supreme=True) if source=='vsrf' else None,
                    'reason':'Требуется проверка карточек, ролей и всех страниц.','evidence_ids':[proof]})
            return result
        except Exception as exc:
            result.status='error';result.reason=f'Не удалось завершить этап {stage}: {error_code(exc)}'
            result.diagnostics.update(stage=stage,error=error_code(exc))
            try:
                capture(page,result)
                problem=blocked(page.locator('body').inner_text())
                if problem:
                    result.status=problem;self.blocked_hosts[host]=problem
            except Exception:pass
            return result
        finally:
            result.diagnostics['elapsed_seconds']=round(time.monotonic()-started,1)
            page.close()

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
        response=navigate(page,target)
        body=page.locator('body').inner_text()
        problem=blocked(body,response.status if response else 500)
        if not problem and not pending and not cursor.get('page_url'):
            page.locator('#id_'+section+'-txt').fill(queries[index])
            with page.expect_navigation(wait_until='commit'):
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
