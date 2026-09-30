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
    net = bool(re.search(r'без\s*НДС', text, re.I))
    gross = bool(re.search(r'(?:с\s*НДС|включая\s*НДС|НДС\s*включ[её]н|в\s*т\.?\s*ч\.?\s*НДС)', text, re.I))
    rate = re.search(r'НДС\s*[:(]?\s*(\d+(?:[.,]\d+)?)\s*%', text, re.I)
    rate = float(rate[1].replace(',', '.')) if rate else None
    if rate is not None and not 0 <= rate <= 100: rate = None
    unit = 'piece' if re.search(r'(?:за\s*(?:1\s*)?(?:шт\.?|штук[ау])|/\s*шт\.?|единица\s*[:—-]?\s*шт)', text, re.I) else ''
    if re.search(r'(?:за|/)\s*(?:упак|комплект)', text, re.I): unit = ''
    return {'basis': 'unknown' if net == gross else 'net' if net else 'gross',
            'rate': rate, 'unit': unit, 'evidence': text[:400]}


def terms_for(row, override=None):
    details = row.get('_specifications') or {}
    terms = dict(details.get('price_terms') or parse_terms(row.get('price_text')))
    if override:
        terms.update({k: v for k, v in override.items() if k in ('basis', 'rate', 'unit')})
        terms['evidence'] = 'Условия подтверждены пользователем для текущей цены'
    return terms


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
