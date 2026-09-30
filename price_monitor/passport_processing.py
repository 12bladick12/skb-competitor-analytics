"""Public document selection, official fallbacks and idempotent download execution."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin,urlsplit
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup

from .passport_sources import (candidates, audited_candidates, official_pages, official_seeds,
    classify_pdf, valid_url, exact_model_in_text, TECH, REJECT, VERSION)
from .passport_transport import DocumentClient
from .passport_recognition import inspect_pdf
from .transport import FetchError
from .models import utcnow


class Resolver:
    def __init__(self, product, client, repository=None, force=False):
        self.product,self.client=product,client
        self.repository=repository;self.force=force
        self.notes=[]

    def card_candidates(self):
        p=self.product
        details=json.loads(p.get('details_json') or '{}')
        if details.get('document_parser_version')==VERSION: return details.get('documents',[])
        params={'source':p['source'],'url':p['product_url'],'period':utcnow()[:7],'version':VERSION,'now':utcnow()}
        if self.repository and not self.force:
            cached=self.repository.batch('''SELECT candidates_json FROM passport_card_scans WHERE source=%(source)s
                AND url=%(url)s AND period=%(period)s AND version=%(version)s''',params)
            if cached:return json.loads(cached[0]['candidates_json'])
        # Initial migration may need just the document block from an old cached
        # card. This never changes a price or its measurement date.
        response=self.client.fetch(p['product_url'])
        if response.status!=200: return []
        soup=BeautifulSoup(response.body,'html.parser')
        found=candidates(p['source'],soup,response.url,p['manufacturer'])
        if self.repository:
            self.repository.batch('''INSERT INTO passport_card_scans(source,url,period,version,candidates_json,checked_at)
                VALUES(%(source)s,%(url)s,%(period)s,%(version)s,%(json)s,%(now)s)
                ON CONFLICT(source,url,period,version) DO UPDATE SET candidates_json=excluded.candidates_json,checked_at=excluded.checked_at''',
                {**params,'json':json.dumps(found,ensure_ascii=False)})
        return found

    def official_candidates(self):
        p=self.product; brand=p['manufacturer']; article=p['article']
        yield from audited_candidates(brand,article)
        queue=official_pages(brand,article,p['product_url'],p.get('title') or '')+official_seeds(brand)
        seen=set(); budget=24
        while queue and len(seen)<budget:
            url=queue.pop(0)
            if url in seen or not valid_url(url,brand): continue
            seen.add(url)
            try: response=self.client.fetch(url)
            except FetchError as exc:
                self.notes.append(exc.status+': '+url);continue
            if response.status!=200:continue
            body=response.body.decode('utf-8',errors='replace')
            if '<!ENTITY' in body.upper(): continue
            if urlsplit(url).path.endswith('/robots.txt'):
                queue.extend(re.findall(r'(?im)^sitemap:\s*(https://\S+)',body));continue
            if '<sitemapindex' in body or '<urlset' in body:
                try:root=ET.fromstring(body)
                except ET.ParseError:continue
                locs=[n.text for n in root.iter() if n.tag.split('}')[-1]=='loc' and n.text]
                if root.tag.split('}')[-1]=='sitemapindex':queue.extend(locs[:12])
                else:queue.extend(u for u in locs if exact_model_in_text(article,urlsplit(u).path.replace('/',' ').replace('_',' ')))
                continue
            soup=BeautifulSoup(body,'html.parser')
            # Full model match in a page is required before using its generic PDF links.
            applicable=exact_model_in_text(article,soup.get_text(' ',strip=True))
            for a in soup.select('a[href]'):
                target=urljoin(response.url,a['href']); label=a.get_text(' ',strip=True)
                context=label+' '+a.parent.get_text(' ',strip=True)[:500]
                if not valid_url(target,brand) or REJECT.search(label+' '+target):continue
                exact=exact_model_in_text(article,label+' '+target)
                technical=bool(TECH.search(context+' '+target))
                if (applicable or exact) and (technical or re.search(r'\.pdf(?:$|\?)',target,re.I)):
                    yield {'url':target,'name':label,'kind':'passport' if re.search('паспорт|passport',context,re.I) else 'datasheet','source_page':response.url}
                elif exact and target not in seen:queue.append(target)
        if queue:self.notes.append('Официальный поиск ограничен 24 страницами; полнота не подтверждена')


def download_job(passports, files, job, owner, client_factory=DocumentClient):
    product=passports.product(job['rule_id'])
    if not product: raise ValueError('Товар отсутствует')
    payload=json.loads(job['payload_json'])
    if payload.get('input_hash'):
        latest=passports.repo.batch('SELECT details_hash FROM product_index WHERE rule_id=%(id)s',{'id':job['rule_id']})
        if latest and latest[0]['details_hash']!=payload['input_hash']:
            passports.finish(job['id'],owner,'done','Заменено более свежей редакцией карточки');return
    # Origin comes from the configured source, never from a PDF or a scraped link.
    from .sources import SOURCES
    client=client_factory(product['manufacturer'],origin_host=SOURCES[product['source']].host)
    resolver=Resolver(product,client,passports.repo,payload.get('manual',False)); seen=set(); failures=[]; best=None
    def candidate_state(candidate,state,note=''):
        passports.repo.batch('''INSERT INTO passport_candidates(rule_id,url,name,kind,source_page,state,note,checked_at)
            VALUES(%(id)s,%(url)s,%(name)s,%(kind)s,%(page)s,%(state)s,%(note)s,%(now)s)
            ON CONFLICT(rule_id,url) DO UPDATE SET state=excluded.state,note=excluded.note,checked_at=excluded.checked_at''',
            {'id':job['rule_id'],'url':candidate['url'],'name':candidate.get('name',''), 'kind':candidate.get('kind','unknown'),
             'page':candidate.get('source_page',''),'state':state,'note':note[:1000],'now':utcnow()})
    try:
        try: card=resolver.card_candidates()
        except FetchError as exc: card=[];failures.append(exc.status+': '+str(exc))
        # Prefer the current passport even when a publisher removes its link.
        if product.get('source_url') and not any(c['url']==product['source_url'] for c in card):
            card.append({'url':product['source_url'],'name':'Ранее полученный паспорт','kind':'passport'})
        def all_candidates():
            yield from sorted(card,key=lambda c:0 if c.get('kind')=='passport' else 1)
            yield from resolver.official_candidates()
        for candidate in all_candidates():
            url=candidate['url']
            if url in seen:continue
            seen.add(url)
            candidate_state(candidate,'found')
            try:
                validators=url==product.get('source_url') and product.get('current_fingerprint')
                # Reuse a URL already validated this month by another execution.
                shared=passports.repo.batch('''SELECT current_fingerprint,etag,last_modified FROM passport_products
                    WHERE source_url=%(url)s AND substr(checked_at,1,7)=%(month)s AND current_fingerprint<>''
                    AND state IN ('downloaded','review') ORDER BY checked_at DESC LIMIT 1''',
                    {'url':url,'month':json.loads(job['payload_json']).get('period','')})
                if shared:
                    raw=files.get(shared[0]['current_fingerprint']);headers={'ETag':shared[0]['etag'],'Last-Modified':shared[0]['last_modified']};final_url=url
                else:
                    response=client.fetch(url,product.get('etag','') if validators else '',product.get('last_modified','') if validators else '')
                    if response.status in (404,410):
                        reason=f'HTTP {response.status}: {url}';failures.append(reason);candidate_state(candidate,'unavailable',reason);continue
                    if response.status==304:
                        if not validators:raise ValueError('304 без сохранённого файла')
                        raw=files.get(product['current_fingerprint'])
                    else:raw=response.body
                    headers=response.headers;final_url=response.url
                pdf=inspect_pdf(raw)
                classification=classify_pdf(pdf['text'],product['article'],product.get('title') or '')
                if not classification['accepted']:
                    failures.append(classification['reason']+': '+url);candidate_state(candidate,'rejected',classification['reason']);continue
                candidate_state(candidate,'verified' if classification['applicability']=='confirmed' else 'review',classification['reason'])
                score=(classification['applicability']=='confirmed',classification['kind']=='passport',
                       classification.get('revision_text',''),classification['language']=='ru')
                if best is None or score>best[0]:best=(score,raw,pdf,classification,final_url,headers)
                # Inspect every published card candidate to compare known editions.
                # An exact passport avoids a redundant crawl of official fallbacks.
                if best and best[0][:2]==(True,True) and all(c['url'] in seen for c in card):break
            except (FetchError,ValueError,RuntimeError) as exc:
                failures.append((exc.status+': ' if isinstance(exc,FetchError) else '')+str(exc)[:250])
                candidate_state(candidate,'unavailable',failures[-1])
        if best:
            _,raw,pdf,classification,url,headers=best
            fingerprint,key=files.put(raw)
            passports.register(job,owner,fingerprint,key,len(raw),pdf['pages'],classification,url,
                               headers.get('ETag',product.get('etag','') if url==product.get('source_url') else ''),
                               headers.get('Last-Modified',product.get('last_modified','') if url==product.get('source_url') else ''))
        else:
            notes=failures+resolver.notes
            state='unavailable' if notes else 'not_published'
            passports.state(job,owner,state,'; '.join(notes) or 'В карточке и разрешённых официальных источниках паспорт не найден')
            # Network limits are retryable; robots denial and absent publication wait
            # for the next monthly task. No repeating negative search each run.
            if any(n.startswith(('network_error','rate_limited','http_error','robots_unavailable')) for n in notes):
                passports.finish(job['id'],owner,'retry','; '.join(notes),delay=min(86400,300*2**min(job['attempts'],8)))
                return
        passports.finish(job['id'],owner)
    finally:
        job['_metrics']={'requests':getattr(client,'request_count',0),'bytes':getattr(client,'received_bytes',0),'pages':0}
        client.close()
