"""Durable sitemap/catalog traversal. Only explicit public navigation is followed."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit, unquote
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup

from .details import BRAND_PATTERNS, parse_catalog_product
from .models import utcnow
from .sources import SOURCES, validate_url
from .transport import FetchError, SourceClient

SEEDS = {
    'sensoren': [('sitemap','https://sensoren.ru/sitemap.xml'),('listing','https://sensoren.ru/catalog/')],
    'beskonta': [('sitemap','https://beskonta.ru/sitemap.xml'),('listing','https://beskonta.ru/catalog/all/')],
    'megak': [('sitemap','https://mega-k.com/sitemap.xml'),('listing','https://mega-k.com/catalog')],
    'teko': [('sitemap','https://teko-com.ru/sitemap/sitemap.xml'),('listing','https://teko-com.ru/catalog/')],
    'sensor': [('sitemap','https://sensor-com.ru/sitemap/main.xml'),('listing','https://sensor-com.ru/')],
}
# Brand names observed in Sensoren's public manufacturer navigation. Known
# unselected brands can be excluded before fetching. Unknown URLs are inspected.
OTHER_SENSOR_BRANDS = (
    'autrol','banner','baumer','carlo_gavazzi','contrinex','cyndar','datalogic','datasensor',
    'delta_electronics','ege','ema_electronic','endress_hauser','festo','innocont','innolevel',
    'innovert','keyence','leuze','lovato','mega_k','micro_detectors','microsonic','temposonics',
    'neftim','nivelco','omron','raventek','schmersal','sensopart','sentec','sentinel','siemens',
    'skb_induktsiya','teko','telco','telemecanique','turck','vega','watts','wenglor','wika','yaskawa',
)


def page_id(run_id, source, url):
    return hashlib.sha256(f'{run_id}\n{source}\n{url}'.encode()).hexdigest()


def canonical_url(source, base, href):
    try:
        url=urljoin(base,href)
        p=urlsplit(url)
        if p.hostname not in SOURCES[source].hosts:return None
        # Canonical TLS navigation, preserving only real pagination parameters.
        if p.scheme not in ('http','https') or p.username or p.password or p.port not in (None,80,443):return None
        pairs=[]
        for key,value in parse_qsl(p.query,keep_blank_values=True):
            if key.lower().startswith('utm_') or key.lower() in ('ysclid','gclid','yclid'):continue
            if re.fullmatch(r'PAGEN_\d+|page|p',key,re.I) and value.isdigit() and int(value)>0:
                pairs.append((key,value))
            else:return None
        url=urlunsplit(('https',SOURCES[source].host,p.path or '/',urlencode(sorted(pairs)),''))
        validate_url(source,url,product=False)
        return url
    except (ValueError,TypeError):return None


def product_url(source, url):
    try:validate_url(source,url);return True
    except ValueError:return False


def selected_sensoren_url(url, brands):
    path=unquote(urlsplit(url).path).lower()
    for brand,pattern in BRAND_PATTERNS.items():
        if re.search(r'(?:^|[/_-])'+pattern+r'(?:[_/-]|$)',path,re.I):
            return brand in brands
    if any(re.search(r'(?:^|[/_-])'+re.escape(brand)+r'(?:[_/-]|$)',path) for brand in OTHER_SENSOR_BRANDS):
        return False
    return True


def classify(source, url, brands):
    path=urlsplit(url).path
    if re.search(r'sitemap[^/]*\.xml(?:\.gz)?$',path,re.I) or (source=='sensor' and path.startswith('/sitemap/') and path.endswith('.xml')):
        return 'sitemap'
    if product_url(source,url):
        return 'product' if source!='sensoren' or selected_sensoren_url(url,brands) else None
    prefixes=('/categories/','/catalog') if source=='megak' else ('/catalog/',)
    if any(path.startswith(prefix) for prefix in prefixes):return 'listing'
    return None


def discover(source, kind, body, base, brands):
    found={}
    if kind=='sitemap':
        if '<!DOCTYPE' in body.upper() or '<!ENTITY' in body.upper():
            raise ValueError('Вместо карты сайта получен документ другого типа')
        root=ET.fromstring(body)
        root_name=root.tag.split('}')[-1]
        if root_name not in ('sitemapindex','urlset'):
            raise ValueError('Не распознан формат карты сайта')
        links=[n.text for n in root.iter() if n.tag.split('}')[-1]=='loc' and n.text]
        for href in links:
            url=canonical_url(source,base,href)
            if not url:continue
            category='sitemap' if root_name=='sitemapindex' else classify(source,url,brands)
            if category:found[url]=category
    else:
        soup=BeautifulSoup(body,'html.parser')
        for a in soup.select('a[href]'):
            url=canonical_url(source,base,a['href'])
            if not url:continue
            category=classify(source,url,brands)
            if category:found[url]=category
    return [(kind,url) for url,kind in found.items() if url!=base]


def process_catalog(repository, run_id, source, owner, shutdown, client_factory=SourceClient):
    from .cloud import CancellationProbe
    info=repository.source(run_id,source)
    if not info or info['state'] not in ('pending','running'):return
    brands=json.loads(info['brands_json'])
    if not repository.start(run_id,source,owner):return
    repository.add_pages(run_id,source,SEEDS[source],owner)
    cancelled=CancellationProbe(lambda:repository.cancelled(run_id,source,owner),shutdown,ttl=3)
    client=client_factory(source,cancelled=cancelled)
    failures=0
    try:
        while not shutdown.is_set():
            if cancelled():return
            page=repository.claim(run_id,source,owner)
            if not page:
                repository.finish_source(run_id,source,owner)
                return
            try:
                url,code,body=client.fetch_document(page['url'],html_only=page['kind']=='product')
                if code!=200:
                    repository.finish_page(page['id'],owner,'failed',f'HTTP {code}',code)
                    continue
                if page['kind']=='product':
                    results,detail=parse_catalog_product(source,body,url,brands)
                    if detail=='outside_scope':
                        repository.finish_page(page['id'],owner,'skipped','Другой производитель',code)
                    elif results:
                        repository.record_products(run_id,source,page['id'],results,owner)
                    else:
                        repository.finish_page(page['id'],owner,'failed',detail,code)
                else:
                    links=discover(source,page['kind'],body,url,brands)
                    repository.add_pages(run_id,source,links,owner)
                    repository.finish_page(page['id'],owner,'done','',code)
                failures=0
            except FetchError as exc:
                if shutdown.is_set() or exc.status=='cancelled':return
                repository.finish_page(page['id'],owner,'failed',str(exc),exc.http_status)
                failures=failures+1 if exc.status in ('network_error','http_error') else 0
                if exc.stop_source or failures>=3:
                    repository.block_source(run_id,source,owner,str(exc))
                    return
            except (ValueError,ET.ParseError) as exc:
                repository.finish_page(page['id'],owner,'failed',str(exc)[:500],None)
    finally:client.close()
