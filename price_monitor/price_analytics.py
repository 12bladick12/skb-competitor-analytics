"""Equal-weight model changes and saved, verified analogue deltas."""
from collections import defaultdict
from statistics import mean

from .matching import display
from .price_terms import terms_for, price_views, comparable

FAMILIES = {'inductive': 'Индуктивные', 'capacitive': 'Ёмкостные', 'optical': 'Оптические',
            'reed': 'Магниточувствительные', 'ultrasonic': 'Ультразвуковые',
            'pressure': 'Давление', 'temperature': 'Температура', 'level': 'Уровень'}


def functional_group(sensor):
    return FAMILIES.get(sensor.family, sensor.category.split(' / ')[-1] if sensor.category else 'Тип не определён')


def execution_group(sensor):
    keys = ('body_type', 'diameter', 'output', 'function', 'mount', 'sn', 'connection')
    return ' · '.join(display(sensor.values.get(k), k) for k in keys) + (' · ' + ', '.join(sensor.special) if sensor.special else '')


def model_statistics(item, sensor, history, basis='internet', own_price=None, direct=False):
    terms = item.get('_price_terms') or {}
    observations = sorted(history, key=lambda r: (str(r.get('checked_at', '')), r.get('id', 0)))
    priced = [r for r in observations if r.get('status') == 'priced' and r.get('price') is not None]
    last = priced[-1] if priced else None
    currency = last.get('currency') if last else item.get('last_currency')
    current_terms = terms_for(last or {})
    # A manual confirmation describes today's price; it is not historical evidence.
    latest_is_current = bool(last and str(last.get('checked_at')) == str(item.get('price_checked_at')))
    if latest_is_current and terms.get('competitor',{}).get('price_checked_at') == str(item.get('price_checked_at')):
        current_terms = terms_for(last, terms['competitor'])
    latest = price_views(last.get('price') if last else None, current_terms)[basis]
    change = None
    if len(priced) >= 2:
        first = priced[0]
        ft, lt = terms_for(first), terms_for(last)
        a, b = price_views(first['price'], ft)[basis], price_views(last['price'], lt)[basis]
        same = first.get('currency') == currency and ft.get('unit') == lt.get('unit')
        if basis == 'internet': same = same and ft.get('basis') == lt.get('basis') and ft.get('rate') == lt.get('rate')
        if same and a and b: change = (b / a - 1) * 100
    own_terms = terms.get('ours') or {}
    own = price_views(own_price, own_terms)[basis]
    delta = gap = None
    if direct and latest_is_current and latest is not None and own and item.get('our_currency') == currency and comparable(current_terms, own_terms, basis):
        delta = latest - own
        gap = delta / own * 100
    return {'rule_id': item['rule_id'], 'brand': item['manufacturer'], 'model': item['article'],
            'family': functional_group(sensor), 'execution': execution_group(sensor),
            'currency': currency or 'Не указана', 'unit': current_terms.get('unit') or 'Не указана',
            'tax_basis': current_terms.get('basis', 'unknown'), 'rate': current_terms.get('rate'),
            'price': latest, 'change': change, 'delta': delta, 'gap': gap,
            'gross': price_views(last.get('price') if last else None, current_terms)['gross'],
            'net': price_views(last.get('price') if last else None, current_terms)['net'],
            'internet': price_views(last.get('price') if last else None, current_terms)['internet'],
            'checked_at': last.get('checked_at') if last else None}


def aggregate(rows, grouped=False):
    buckets = defaultdict(list)
    for row in rows:
        key = (row['brand'], row['currency'], row['unit'], row['tax_basis'], row['rate'])
        if grouped: key += (row['family'], row['execution'])
        buckets[key].append(row)
    output = []
    for key, values in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        changes = [r['change'] for r in values if r['change'] is not None]
        gaps = [r['gap'] for r in values if r['gap'] is not None]
        deltas = [r['delta'] for r in values if r['delta'] is not None]
        prices = [r['price'] for r in values if r['price'] is not None]
        row = {'Бренд': key[0], 'Валюта': key[1], 'Единица': 'шт.' if key[2] == 'piece' else key[2],
               'НДС в источнике': {'gross': 'С НДС', 'net': 'Без НДС'}.get(key[3], 'Не указан'), 'Ставка НДС, %': key[4]}
        if grouped: row.update({'Функциональная группа': key[5], 'Исполнение': key[6]})
        row.update({'Моделей': len(values), 'С ценой': len(prices), 'С динамикой': len(changes),
                    'Средняя цена': mean(prices) if prices and key[2] != 'Не указана' and key[3] != 'unknown' else None,
                    'Среднее изменение, %': mean(changes) if changes else None,
                    'Мин. изменение, %': min(changes) if changes else None,
                    'Макс. изменение, %': max(changes) if changes else None,
                    'Подтверждённых пар с ценами': len(gaps),
                    'Δ конкурент − СКБ': mean(deltas) if deltas else None,
                    'Δ к СКБ, %': mean(gaps) if gaps else None})
        output.append(row)
    return output
