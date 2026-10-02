"""Shared, observation-based price charts for the local Streamlit screens."""
from collections import OrderedDict

import altair as alt
import pandas as pd

from .price_terms import positive


UNITS = {'piece': 'шт.', 'pack': 'упаковка / комплект'}
BASES = {'gross': 'с НДС', 'net': 'без НДС', 'unknown': 'НДС не указан'}
CURRENCIES = {'RUB': '₽', 'EUR': '€', 'USD': '$', 'CNY': '¥'}
DASHES = ([1, 0], [7, 3], [2, 2], [9, 3, 2, 3], [12, 4])


def chart_points(points, *, currency=None):
    """Validate without replacing missing prices with zero or inventing dates."""
    output = []
    for index, original in enumerate(points):
        point = dict(original)
        price = positive(point.get('price'))
        stamp = point.get('date') or point.get('checked_at')
        if isinstance(stamp, (int, float)):
            continue
        stamp = pd.to_datetime(stamp, utc=True, errors='coerce')
        if price is None or pd.isna(stamp):
            continue
        label = str(point.get('series') or point.get('model') or 'Цена источника')
        point.update(date=stamp, price=price, series=label,
                     currency=str(point.get('currency') or currency or '').upper(),
                     unit=point.get('unit') or '', price_basis=point.get('price_basis') or 'unknown',
                     group=str(point.get('group') or point.get('entry_id') or label) + ':' + str(point.get('segment', '0')),
                     observed_at=stamp.strftime('%d.%m.%Y %H:%M UTC'),
                     source=point.get('source') or 'Источник не указан', _order=index)
        symbol = CURRENCIES.get(point['currency'], point['currency'])
        unit = UNITS.get(point['unit'], point['unit'])
        point['price_label'] = f'{price:,.2f}'.replace(',', ' ').replace('.', ',') + (' ' + symbol if symbol else '') + (' / ' + unit if unit else '')
        point['conditions'] = BASES.get(point['price_basis'], 'НДС не указан') + (' · ' + unit if unit else ' · единица не указана')
        output.append(point)
    return sorted(output, key=lambda p: (p['date'], p['_order']))


def comparison_panels(points):
    """Unknown terms never imply that different products are comparable."""
    panels = OrderedDict()
    for point in chart_points(points):
        identity = str(point.get('entry_id') or point['series'])
        uncertain = identity if not point['unit'] or point['price_basis'] == 'unknown' else ''
        key = (point['currency'], point['unit'], point['price_basis'], uncertain)
        panels.setdefault(key, []).append(point)
    return list(panels.values())


def panel_label(points):
    point = points[0]
    unit = UNITS.get(point['unit'], point['unit']) or 'единица не указана'
    label = (point['currency'] or 'валюта не указана') + ' · ' + unit + ' · ' + BASES.get(point['price_basis'], 'НДС не указан')
    if not point['unit'] or point['price_basis'] == 'unknown':
        label += ' · ' + point['series']
    return label


def build_price_chart(points, *, palette=None, height=300, reference=None, reference_label='Текущая цена СКБ (ориентир)'):
    """Return a validated chart; a singleton is a point, not fabricated history."""
    points = chart_points(points)
    if not points:
        return None
    frame = pd.DataFrame(points)
    first, last = frame.date.min(), frame.date.max()
    span = (last - first).total_seconds()
    padding = pd.Timedelta(hours=12) if span == 0 else pd.Timedelta(seconds=max(span * .035, 60))
    date_format = '%d.%m.%Y' if first.year != last.year else '%d.%m %H:%M' if span < 86400 else '%d.%m'
    labels = list(dict.fromkeys(point['series'] for point in points))
    palette = palette or {}
    colors = [palette.get(label, '#7A1F2B') for label in labels]
    point = points[0]
    symbol = CURRENCIES.get(point['currency'], point['currency'])
    unit = UNITS.get(point['unit'], point['unit'])
    price_title = 'Цена' + (', ' + symbol if symbol else '') + (' / ' + unit if unit else '')
    reference = positive(reference)
    ceiling = max(frame.price.max(), reference or 0) * 1.1
    tick_step = 86400000 if span >= 86400 else 3600000
    base = alt.Chart(frame).encode(
        x=alt.X('date:T', title='Дата проверки (UTC)', scale=alt.Scale(type='utc', domain=[(first-padding).isoformat(), (last+padding).isoformat()]),
                axis=alt.Axis(format=date_format, tickCount=4, tickMinStep=tick_step, labelOverlap='greedy', labelFlush=False, labelAngle=0)),
        y=alt.Y('price:Q', title=price_title, scale=alt.Scale(zero=True, domainMin=0, domain=[0, ceiling]),
                axis=alt.Axis(tickCount=5, labelExpr="replace(format(datum.value, ',.0f'), /,/g, ' ')")),
        color=alt.Color('series:N', title=None, scale=alt.Scale(domain=labels, range=colors), legend=None),
        detail='group:N',
        tooltip=[alt.Tooltip('series:N', title='Модель'), alt.Tooltip('observed_at:N', title='Дата цены'),
                 alt.Tooltip('price_label:N', title='Цена'), alt.Tooltip('conditions:N', title='Условия'),
                 alt.Tooltip('source:N', title='Источник')])
    lines = base.mark_line(interpolate='step-after', strokeWidth=2.2).encode(
        order=alt.Order('date:T'),
        strokeDash=alt.StrokeDash('series:N', scale=alt.Scale(domain=labels, range=[list(DASHES[i % len(DASHES)]) for i in range(len(labels))]), legend=None))
    marks = base.mark_point(filled=True, size=65, stroke='white', strokeWidth=1)
    chart = lines + marks
    if reference is not None:
        ref = alt.Chart(pd.DataFrame([{'price': reference, 'label': reference_label}])).encode(y='price:Q', tooltip=[alt.Tooltip('label:N', title='Ориентир'), alt.Tooltip('price:Q', title=price_title, format=',.2f')])
        chart += ref.mark_rule(color='#18756A', strokeDash=[6, 4], strokeWidth=1.5)
    return chart.properties(height=max(height, 150)).configure_view(stroke=None).configure_axis(
        labelFontSize=12, titleFontSize=12, labelColor='#526175', titleColor='#526175', gridColor='#EDF0F3', titlePadding=10)
