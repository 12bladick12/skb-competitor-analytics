"""Product identity and specifications from the main product, including offers."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import normalize

BRAND_PATTERNS = {
    'Autonics': r'autonics', 'Balluff': r'balluff',
    'Pepperl+Fuchs': r'pepperl[\s_+&-]*fuchs', 'ifm': r'ifm(?:[\s_]+electronic)?',
    'LANBAO': r'lanbao', 'SICK': r'sick',
}


def text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def offers(soup):
    found = []
    for node in soup.select('input[data-offer-id][data-change-cost]'):
        try:
            values = json.loads(node['data-change-cost'])
            article = str(values.get('.rs-product-barcode', '')).strip()
            if article:
                found.append({'id':node['data-offer-id'], 'article':article,
                    'price':str(values.get('.rs-price-new', '')),
                    'attributes':json.loads(node.get('data-info','[]')),
                    'selected':node.has_attr('checked')})
        except (ValueError, TypeError):
            continue
    return found


def manufacturer(source, soup):
    from .sources import SOURCES
    if source=='teko':
        values=attributes(source,soup)
        return manufacturer_from_details({'attributes':values})
    if source!='sensoren':
        return SOURCES[source].brands[0]
    explicit = text(soup.select_one('.product-info__brand-name'))
    value = explicit or text(soup.h1)
    for brand, pattern in BRAND_PATTERNS.items():
        if re.search(r'(?<![a-z])'+pattern+r'(?![a-z])', value, re.I):
            return brand
    return explicit


def main_structured_product(soup):
    """Only JSON-LD matching the current heading; recommendation data is ignored."""
    heading=text(soup.h1)
    if not heading:return None
    matches=[]
    def visit(value):
        if isinstance(value,list):
            for item in value:visit(item)
        elif isinstance(value,dict):
            kind=value.get('@type',[])
            if kind=='Product' or isinstance(kind,list) and 'Product' in kind:
                name=str(value.get('name') or '').strip()
                if name and re.search(r'(?<![\w-])'+re.escape(normalize(name))+r'(?![\w-])',normalize(heading)):
                    matches.append(value)
            if '@graph' in value:visit(value['@graph'])
    for node in soup.select('script[type="application/ld+json"]'):
        try:visit(json.loads(node.string or node.get_text()))
        except (ValueError,TypeError):continue
    # Conflicting structured products cannot establish manufacturer identity.
    identities={json.dumps(p.get('brand'),ensure_ascii=False,sort_keys=True) for p in matches}
    return matches[0] if matches and len(identities)==1 else None


def manufacturer_from_details(details):
    """TEKO is also a dealer: only an explicit product brand is evidence."""
    for row in details.get('attributes',[]):
        if str(row.get('name','')).strip().rstrip(':').casefold() in ('бренд','производитель','manufacturer','brand'):
            value=str(row.get('value') or '').strip()
            if re.fullmatch(r'(?:(?:АО\s+)?НПК\s+)?[«"\s]*(?:ТЕКО|TEKO)[»"\s]*',value,re.I):
                return 'ТЕКО'
            return value
    return str(details.get('manufacturer') or '').strip()


def attributes(source, soup, variant=None):
    rows = []
    def add(name, value, group=''):
        name,value = re.sub(r'\s+',' ',str(name)).strip().rstrip(':'),re.sub(r'\s+',' ',str(value)).strip()
        if name and value and (name,value,group) not in {(x['name'],x['value'],x['group']) for x in rows}:
            rows.append({'name':name,'value':value,'group':group})
    if source=='sensoren':
        for row in soup.select('.characteristics-all li'):
            spans=row.find_all('span',recursive=False)
            value = spans[-1] if spans else row.find('span')
            if not value:
                label,sep,raw=text(row).partition(':')
                if sep:add(label,raw)
                continue
            # The label can be wrapped in a nested element; exclude the value.
            label=BeautifulSoup(str(row),'html.parser').li
            label_spans=label.find_all('span',recursive=False)
            value_copy=label_spans[-1] if label_spans else label.find('span')
            if value_copy:value_copy.decompose()
            add(text(label),text(value))
        # Some cards publish useful fields only in the brief beside the price.
        # Keep both sources, including disagreements, and never parse recommendations.
        for row in soup.select('.product-info__all-characteristics > ul > li'):
            label,sep,raw=text(row).partition(':')
            if sep:add(label,raw)
    elif source=='beskonta':
        for row in soup.select('tr.tab-content_table_character-text, table.p-p-table-mini-h tr'):
            if row.find_parent('tbody',class_='rs-offer-property'):
                continue
            cells=row.find_all('td',recursive=False)
            if len(cells)==2:
                add(text(cells[0]),text(cells[1]),'Общие характеристики')
        if variant:
            for pair in variant.get('attributes',[]):
                if isinstance(pair,list) and len(pair)==2:
                    add(pair[0],pair[1],'Характеристики исполнения')
        else:
            for row in soup.select('tbody.rs-offer-property:not(.hidden) tr'):
                cells=row.find_all('td',recursive=False)
                if len(cells)==2:add(text(cells[0]),text(cells[1]),'Характеристики исполнения')
    elif source=='megak':
        # The summary beside the price has only a few properties. The product's
        # full tab contains frequency, material, IP and load; read both, preserve
        # contradictory values and deduplicate identical pairs.
        for row in soup.select('#properties .properties-item-row'):
            add(text(row.select_one('.properties-item-name')),text(row.select_one('.properties-item-value')))
        for name in soup.select('.details-param-name'):
            row=name.parent
            add(text(name),text(row.select_one('.details-param-value')))
    elif source=='teko':
        for row in soup.select('.product-item-detail-properties .one_prop'):
            add(text(row.select_one('.name')),text(row.select_one('.value')))
        if not any(row['name'].casefold() in ('бренд','производитель','brand','manufacturer') for row in rows):
            product=main_structured_product(soup)
            if product:
                brand=product.get('brand')
                if isinstance(brand,dict):brand=brand.get('name')
                if isinstance(brand,str) and brand.strip():add('Бренд',brand)
    # Product-scoped structured properties complement visible site selectors.
    product=main_structured_product(soup)
    if product:
        properties=product.get('additionalProperty') or []
        if isinstance(properties,dict):properties=[properties]
        for prop in properties:
            if isinstance(prop,dict) and prop.get('name') and prop.get('value') is not None:
                add(prop['name'],str(prop['value'])+(' '+str(prop['unitText']) if prop.get('unitText') else ''))
    for row in soup.select('[itemtype$="/Product"] [itemprop="additionalProperty"]'):
        name=row.select_one('[itemprop="name"]');value=row.select_one('[itemprop="value"]')
        if name and value:add(name.get('content') or text(name),value.get('content') or text(value))
    if source=='sensor':
        for row in soup.select('.product-page__tab-item--char .char-table__item'):
            group=row.find_parent(class_='group_param')
            add(text(row.select_one('.char-table__name')),text(row.select_one('.char-table__value')),
                text(group.select_one('.group_param-name')) if group else '')
    return rows


def article(source, soup, brand):
    values=attributes(source,soup)
    for label in ('артикул','маркировка','наименование','модель'):
        for row in values:
            if row['name'].casefold()==label:
                return row['value']
    if source=='beskonta':
        return text(soup.select_one('.rs-product-barcode'))
    title=text(soup.h1)
    if source=='sensoren':
        match=re.search(BRAND_PATTERNS.get(brand,r'(?!)'),title,re.I)
        if not match:return ''
        tail=title[match.end():].strip()
        # The site uses this product-type phrase ahead of Pepperl+Fuchs's model.
        return re.sub(r'^Radar\s+sensor\s+', '', tail, flags=re.I).strip()
    if source=='teko':
        return re.split(r'\bТЕКО\s+',title,flags=re.I)[-1].strip()
    if source=='sensor':
        match=re.search(r'([A-ZА-ЯЁ0-9]+(?:[-–][A-ZА-ЯЁ0-9]+)+)',title,re.I)
        return match.group(1) if match else title
    return title


def safe_link(base, value):
    if not value:return ''
    target=urljoin(base,value)
    parsed=urlsplit(target)
    return target if parsed.scheme in ('https','http') and parsed.netloc and not parsed.username and not parsed.password else ''


def extract_details(source, soup, url, variant=None):
    from .price_terms import parse_terms
    from .sources import SOURCES
    props=attributes(source,soup,variant)
    selectors={
        'sensoren':'.product-tab-item[data-tab="description"], .product-tab-item.description',
        'beskonta':'#product-page [itemprop="description"], .p-p-description, #description',
        'megak':'.details-description, [itemprop="description"]',
        'teko':'.catalog-detail-text, [itemprop="description"]',
        'sensor':'.product-page__description .product-page__tab-item:first-child',
    }
    descriptions=list(dict.fromkeys(text(n) for n in soup.select(selectors[source]) if text(n)))
    crumb_selector='.bread-crumbs__link' if source=='sensoren' else '[itemtype$="/BreadcrumbList"] [itemprop="name"], .breadcrumbs a, .breadcrumb a'
    crumbs=[text(n) for n in soup.select(crumb_selector)]
    crumbs=list(dict.fromkeys(x for x in crumbs if x and x.casefold() not in ('главная','каталог','каталог товаров','домой','home')))
    title=text(soup.h1)
    crumbs=[x for x in crumbs if normalize(x)!=normalize(title)]
    from .passport_sources import candidates, VERSION as document_version
    documents=candidates(source,soup,url,manufacturer(source,soup))
    images=[]
    meta=soup.select_one('meta[property="og:image"]')
    if meta:
        image=safe_link(url,meta.get('content'))
        if image:images.append(image)
    price_node=soup.select_one(SOURCES[source].price_selector)
    price_terms=parse_terms(text(price_node.parent) if price_node else '')
    return {'attributes':props,'description':'\n\n'.join(descriptions),'category':' / '.join(crumbs), 'price_terms':price_terms,
        'manufacturer':manufacturer(source,soup),
        'documents':documents,'document_parser_version':document_version,'images':images,'variant_id':variant['id'] if variant else '',
        'specification_state':'collected' if props else 'not_published_or_unrecognized',
        'evidence':'public_html'}


def parse_catalog_product(source, html, url, selected_brands):
    from .adapters import ADAPTERS
    from .models import Rule
    from .sources import SOURCES
    soup=BeautifulSoup(html,'html.parser')
    if not soup.h1 or not soup.select_one(SOURCES[source].scope):
        return [], 'Не распознана карточка товара'
    brand=manufacturer(source,soup)
    if source in ('sensoren','teko') and not brand:
        return [], 'Производитель не подтверждён в карточке; товар не включён в базу'
    if brand not in selected_brands:
        return [], 'outside_scope'
    variants=offers(soup) if source=='beskonta' else []
    if variants:
        # One page may contain hundreds of offers. Parse common HTML once;
        # each public offer supplies its own barcode, price and specifications.
        from dataclasses import replace
        from .adapters import parse_money
        first=Rule(source,brand,variants[0]['article'],url)
        base=ADAPTERS[source].parse(first,html,url,200,soup=soup)
        common=extract_details(source,soup,url,{'id':'','attributes':[]})
        currency='RUB' if text(soup.select_one('.p-p-price-currency'))=='₽' else base.currency
        results=[]
        for variant in variants:
            rule=Rule(source,brand,variant['article'],url)
            props=common['attributes']+[
                {'name':str(pair[0]),'value':str(pair[1]),'group':'Характеристики исполнения'}
                for pair in variant['attributes'] if isinstance(pair,list) and len(pair)==2]
            detail={**common,'attributes':props,'variant_id':variant['id'],
                    'variant_set_complete':len(variants)==len(soup.select('input[data-offer-id][data-change-cost]')),
                    'specification_state':'collected' if props else 'not_published_or_unrecognized'}
            price=parse_money(variant['price'])
            empty_price=not variant['price'].strip() or bool(re.fullmatch(r'0(?:[.,]0+)?',variant['price'].strip()))
            status='priced' if price and currency else ('no_price' if empty_price else 'parse_error')
            result=replace(base,status=status,price=price if status=='priced' else None,
                currency=currency if status=='priced' else None,price_text=variant['price'],
                availability='unknown',availability_text='',
                detail='' if status=='priced' else 'Цена исполнения не опубликована или валюта не распознана',
                details_json=json.dumps(detail,ensure_ascii=False))
            results.append((rule,result))
        return results,''
    models=list(dict.fromkeys(v['article'] for v in variants)) or [article(source,soup,brand)]
    results=[]
    for model in models:
        if not model or len(model)>240:continue
        rule=Rule(source,brand,model,url)
        results.append((rule,ADAPTERS[source].parse(rule,html,url,200,soup=soup)))
    return results, '' if results else 'Не удалось определить артикул карточки'
