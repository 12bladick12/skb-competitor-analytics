"""Product-scoped candidates; documents are evidence, never executable instructions."""
from __future__ import annotations

import re
from datetime import date
from urllib.parse import urljoin, urlsplit, quote
from bs4 import BeautifulSoup

VERSION = 'passports-2026-09-30-v1'
OFFICIAL = {
    'МЕГА-К': ('mega-k.com',), 'ТЕКО': ('teko-com.ru',),
    'СЕНСОР': ('sensor-com.ru',), 'BESKONTA': ('beskonta.ru',),
    'Autonics': ('autonics.com',),
    'Balluff': ('balluff.com', 'assets.balluff.com', 'publications.balluff.com'),
    'Pepperl+Fuchs': ('pepperl-fuchs.com', 'files.pepperl-fuchs.com'),
    'ifm': ('ifm.com', 'media.ifm.com'),
    'LANBAO': ('cnlanbaosensor.com', 'lanbaosensor.com'),
    'SICK': ('sick.com', 'cdn.sick.com'),
}
REJECT = re.compile(r'сертифик|деклараци|прайс|реклам|каталог|certificate|declaration|price.?list|cad\b|3d.?model|advertis|brochure|(?:^|/)(?:catalog|catalogue)[^/]*\.pdf|\.(png|jpe?g|gif|svg|dwg|dxf|step|stp|zip)(?:$|[?#])', re.I)
TECH = re.compile(r'паспорт|техническ\w*\s+описан|passport|pasport|data\s*sheet|datenblatt|technical\s+(data|description)|pdfengine', re.I)
SECTIONS = {
    'megak': '.details-briefproperties, #properties .properties-item-row',
    'teko': '.manufacturer-docs',
    'sensor': '.product-page__document',
    'sensoren': '.product-tab-item.documents',
    'beskonta': '#product-page, .p-p-info, .tab-content',
}


def valid_url(url, manufacturer, origin_host=''):
    p = urlsplit(url)
    hosts = set(OFFICIAL.get(manufacturer, ())) | ({origin_host.removeprefix('www.')} if origin_host else set())
    host = (p.hostname or '').removeprefix('www.')
    return (p.scheme == 'https' and host in hosts and not p.username and not p.password
            and p.port in (None, 443) and not any(ord(c) < 32 for c in url) and '\\' not in url)


def candidates(source, soup, url, manufacturer=''):
    base = soup.find('base', href=True)
    base_url = urljoin(url, base['href']) if base else url
    # A malicious or stale base may not change the origin of a source card.
    if urlsplit(base_url).hostname != urlsplit(url).hostname:
        base_url = url
    sections = soup.select(SECTIONS[source]) if source in SECTIONS else [soup]
    result = []
    for section in sections:
        for a in section.select('a[href]'):
            href = urljoin(base_url, a['href'])
            label = a.get_text(' ', strip=True)
            context = a.parent.get_text(' ', strip=True)[:600]
            if source == 'megak':
                context = section.get_text(' ', strip=True)[:600]
            if REJECT.search(label + ' ' + href):
                continue
            kind = 'passport' if re.search(r'паспорт|passport|pasport', label + ' ' + context + ' ' + href, re.I) else 'datasheet'
            explicit = bool(TECH.search(label + ' ' + context + ' ' + href))
            pdf = bool(re.search(r'\.pdf(?:$|[?#])', href, re.I))
            if not (explicit or pdf) or not valid_url(href, manufacturer, urlsplit(url).hostname):
                continue
            item = {'name': label or ('Технический паспорт' if explicit else 'PDF — тип требует проверки'),
                    'url': href, 'kind': kind if explicit else 'unknown', 'context': context,
                    'source_page': url}
            if href not in {x['url'] for x in result}:
                result.append(item)
    return result


def identity(value):
    return re.sub(r'\s+', '', str(value).upper().replace('–', '-').replace('—', '-'))


def exact_model_in_text(model, text):
    code = identity(model)
    body = str(text).upper().replace('–', '-').replace('—', '-')
    pattern = r'\s*'.join(re.escape(c) for c in code)
    return bool(code and re.search(r'(?<![\w-])' + pattern + r'(?![\w-])', body))


def classify_pdf(text, article, title=''):
    """Series-name similarity is never proof of variant applicability."""
    header = text[:1800]
    technical = bool(re.search(r'техническ|характеристик|technical|specification|datasheet|data\s+sheet|паспорт|dimensions|габарит', text, re.I))
    cert_heading=re.search(r'сертификат\s+соответствия|declaration\s+of\s+conformity|certificate\s+of',header,re.I)
    tech_heading=re.search(r'паспорт\b|passport|data\s*sheet',header,re.I)
    certification=bool(cert_heading and (not tech_heading or cert_heading.start()<tech_heading.start()))
    if certification or not technical:
        return {'accepted': False, 'reason': 'Документ не является паспортом или техническим описанием'}
    exact = exact_model_in_text(article, text)
    # The complete designation may be distinct from the catalog's order code.
    if not exact and title:
        exact = exact_model_in_text(title, text)
    return {'accepted': True, 'applicability': 'confirmed' if exact else 'review',
            'reason': 'Полное обозначение найдено в техническом документе' if exact else 'Применимость к исполнению требует проверки',
            'kind': 'passport' if re.search(r'паспорт|passport', header, re.I) else 'datasheet',
            'language': 'ru' if len(re.findall('[А-Яа-я]', header)) > 25 else 'en',
            'revision_text': revision_date(text)}


def revision_date(text):
    """Only explicit edition labels; download/manufacturing dates are not editions."""
    pattern=r'(?:дата\s+(?:редакции|изменения)|редакция|revision\s+date|date\s+of\s+(?:issue|release)|release\s+date|дата\s+выпуска\s+документа)\s*[:№#-]?\s*(\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{4})'
    found=[]
    for match in re.finditer(pattern,text,re.I):
        parts=re.split(r'[./-]',match[1]);parts=parts if len(parts[0])==4 else parts[::-1]
        try:found.append(date(*map(int,parts)).isoformat())
        except ValueError:continue
    return max(found,default='')


def official_pages(manufacturer, article, product_url='', title=''):
    """Only product page templates, never guessed file names."""
    code = quote(article.strip(), safe='')
    if manufacturer == 'ifm':
        return [f'https://www.ifm.com/gb/en/product/{code}']
    if manufacturer == 'Autonics':
        return [f'https://www.autonics.com/us/model/{code}']
    if manufacturer == 'Balluff':
        order = re.search(r'\bB(?:ES|CS|OS)[0-9A-Z]{4}\b', (article + ' ' + title).upper())
        if not order:
            order = re.search(r'(bes[0-9a-z]{4})(?:/|$)', product_url, re.I)
        return [f'https://www.balluff.com/en-us/products/{(order.group(1) if order.lastindex else order.group(0)).upper()}'] if order else []
    return []


def official_seeds(manufacturer):
    """Public navigation for brands without a stable model route."""
    return {
        'Pepperl+Fuchs': ['https://www.pepperl-fuchs.com/sitemap.xml'],
        'SICK': ['https://www.sick.com/robots.txt'],
        'LANBAO': ['https://www.cnlanbaosensor.com/sitemap.xml'],
        'BESKONTA': ['https://beskonta.ru/informaciya/'],
        'ТЕКО': ['https://teko-com.ru/pdf/'],
        'СЕНСОР': ['https://sensor-com.ru/sitemap/main.xml'],
        'МЕГА-К': ['https://mega-k.com/sitemap.xml'],
    }.get(manufacturer, [])


# These URLs were actually published and inspected in the source audit.
# Applicability still must be checked in the downloaded PDF, never inferred here.
AUDITED = {
    ('Pepperl+Fuchs', 'NJ4-12GK-SN'): ['https://files.pepperl-fuchs.com/webcat/navi/productInfo/pds/70133109_eng.pdf'],
    ('SICK', 'IME12-04BPSZC0S'): ['https://www.sick.com/media/pdf/1/81/481/dataSheet_IME12-04BPSZC0S_1040764_en.pdf'],
}
SERIES = {
    'Autonics': [(r'^PR(?:L|W|WL|CM|CML)?\d+-.*', 'https://www.autonics.com/in/data/manual/en/PR_DC_3-wire')],
    'LANBAO': [(r'^LR12XB[FN].*Y-E2$', 'https://www.cnlanbaosensor.com/uploads/LR12XB-Y-DC-3-E2.pdf')],
}


def audited_candidates(manufacturer, article):
    urls = list(AUDITED.get((manufacturer, article), []))
    urls += [url for pattern, url in SERIES.get(manufacturer, []) if re.fullmatch(pattern, article, re.I)]
    return [{'url': u, 'name': 'Официальное техническое описание', 'kind': 'datasheet', 'source_page': u} for u in urls]
