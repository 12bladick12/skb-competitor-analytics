"""Explicit tax and unit evidence; no default VAT rate or invented price."""
import math
import re


def positive(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def parse_terms(text):
    text = re.sub(r'\s+', ' ', str(text or '')).strip()
    net = bool(re.search(r'без\s*(?:уч[её]та\s*)?НДС|не\s*включая\s*НДС|НДС\s*не\s*включ[её]н', text, re.I))
    gross_text = re.sub(r'не\s*включая\s*НДС|НДС\s*не\s*включ[её]н', '', text, flags=re.I)
    gross = bool(re.search(r'(?:с\s*(?:уч[её]том\s*)?НДС|включая\s*НДС|НДС\s*(?:\d+(?:[.,]\d+)?\s*%\s*)?включ[её]н|в\s*т\.?\s*ч\.?\s*НДС)', gross_text, re.I))
    rates = {float(v.replace(',', '.')) for v in re.findall(r'НДС\s*[:(]?\s*(\d+(?:[.,]\d+)?)\s*%', text, re.I)}
    rates.update(float(v.replace(',', '.')) for v in re.findall(r'(\d+(?:[.,]\d+)?)\s*%\s*НДС', text, re.I))
    rate = next(iter(rates)) if len(rates)==1 else None
    if rate is not None and not 0 <= rate <= 100: rate = None
    unit = 'piece' if re.search(r'(?:за\s*(?:1\s*)?(?:шт\.?|штук[ау])|/\s*шт\.?|единица\s*[:—-]?\s*шт)', text, re.I) else ''
    if re.search(r'(?:за|/)\s*(?:упак|комплект)', text, re.I): unit = 'pack'
    return {'basis': 'unknown' if net == gross else 'net' if net else 'gross',
            'rate': rate, 'unit': unit, 'evidence': text[:400]}


def terms_for(row, override=None):
    details = row.get('_specifications') or {}
    stored = details.get('price_terms') or {}
    # Reparse saved evidence after recognizer updates, without rewriting history.
    terms = {**stored, **parse_terms(stored.get('evidence') or row.get('price_text'))} if stored.get('evidence') else dict(stored or parse_terms(row.get('price_text')))
    if stored:
        for key in ('basis','rate','unit'):
            if terms.get(key) in (None,'','unknown') and stored.get(key) not in (None,'','unknown'):
                terms[key] = stored[key]
    if override:
        terms.update({k: v for k, v in override.items() if k in ('basis', 'rate', 'unit')})
        terms['evidence'] = 'Условия подтверждены пользователем для текущей цены'
    return terms


def extract_price_terms(source, soup, url):
    """Scope VAT to the actual offer; source-wide terms must explicitly say 'prices'."""
    from .sources import SOURCES
    from .models import utcnow
    scopes = {'sensoren':'.product-info__all-order', 'beskonta':'.p-p-block-price',
              'megak':'.details-payment', 'teko':'.catalog-detail-right',
              'sensor':'.navigation-product__priceblock'}
    node = soup.select_one(SOURCES[source].price_selector)
    offer = soup.select_one(scopes[source])
    texts = [n.get_text(' ', strip=True) for n in (node.parent if node else None, offer) if n]
    result = parse_terms(texts[0] if texts else '')
    if len(texts)>1:
        wider = parse_terms(texts[1])
        for key in ('basis','rate','unit'):
            if result.get(key) in (None,'','unknown') and wider.get(key) not in (None,'','unknown'):
                result[key] = wider[key]
                result['evidence'] = texts[1][:600]
    if result['basis']=='unknown' or result['rate'] is None:
        statements=[]
        candidates=list(soup.select('footer p, footer li, .footer p, .price-note, .price-notice, .vat, .nds'))
        candidates.extend(n.parent for n in soup.find_all(string=re.compile(r'цен[аы].{0,90}НДС',re.I)))
        product_root=soup.select_one(SOURCES[source].scope)
        if product_root:
            candidates.extend(n.parent for n in product_root.find_all(string=re.compile(r'стоимость.{0,90}НДС',re.I)))
        for n in candidates:
            value=n.get_text(' ',strip=True)
            if re.search(r'(?:цен[аы]|стоимость).{0,90}(?:НДС|налог)', value, re.I) and len(value)<650 and not re.search(r'доставк',value,re.I):
                statements.append(value)
        if statements:
            fallback=parse_terms(' '.join(dict.fromkeys(statements)))
            if result['basis']=='unknown' and fallback['basis']!='unknown':
                result['basis']=fallback['basis'];result['evidence']=fallback['evidence']
            if result['rate'] is None and fallback['basis']==result['basis']:
                result['rate']=fallback['rate']
    result.update(source_url=url, checked_at=utcnow(), scope='product_offer')
    return result


def price_views(value, terms):
    value = positive(value)
    result = {'internet': value, 'gross': None, 'net': None}
    if value is None: return result
    basis = terms.get('basis')
    if basis not in ('gross', 'net'): return result
    result[basis] = value
    rate = terms.get('rate')
    if rate is not None:
        try: rate = float(rate)
        except (ValueError, TypeError): return result
        if math.isfinite(rate) and 0 <= rate <= 100:
            factor = 1 + rate / 100
            result['net' if basis == 'gross' else 'gross'] = value / factor if basis == 'gross' else value * factor
    return result


def comparable(left, right, basis):
    if not left.get('unit') or left.get('unit') != right.get('unit'): return False
    if basis == 'internet':
        return left.get('basis') in ('gross', 'net') and left.get('basis') == right.get('basis') and left.get('rate') == right.get('rate')
    return True
