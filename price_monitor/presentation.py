from html import escape

import altair as alt
import pandas as pd
import streamlit as st

from .models import STATUS_LABELS,AVAILABILITY_LABELS
from .sources import SOURCES

CSS='''<style>
[class*="st-key-source_card_"],[class*="st-key-compare_card_"] {background:#fff!important;border-color:#E3E8EE!important;border-radius:12px!important}
.row-title {font-size:.94rem;font-weight:700;overflow-wrap:anywhere;margin:2px 0 5px}
.row-meta {font-size:.75rem;color:#637387;line-height:1.6}
.price-label {font-size:.72rem;color:#637387;margin-top:6px}
.price-value {font-size:1.17rem;font-weight:700;margin:4px 0}
.price-detail {font-size:.74rem;color:#637387}
.positive {color:#AE3838}.negative {color:#18756A}
.match-badge{display:inline-block;font-size:.7rem;font-weight:650;padding:4px 8px;border-radius:6px;margin:4px 5px 0 0}
.match-direct{background:#E9F5F1;color:#176557}.match-close{background:#FFF5E4;color:#8B5A13}
.match-review,.match-unsupported{background:#F0F3F7;color:#526175}.match-incompatible{background:#FBECEF;color:#922C3D}
.match-proposed{font-size:.7rem;color:#788596}.match-arrow{font-size:.68rem;color:#7A1F2B;margin:5px 0 2px}
.match-our-model{font-size:.85rem;color:#176557}
</style>'''


def heading(title,subtitle):
    st.title(title, anchor=False)
    st.html(f'<p class="page-intro">{escape(subtitle)}</p>')


def money(value,currency='RUB'):
    if value is None or value=='':return '—'
    symbol={'RUB':'₽','EUR':'€','USD':'$','CNY':'¥'}.get(currency,currency or '')
    return f'{float(value):,.2f}'.replace(',',' ').replace('.',',')+' '+symbol


def metric_html(label,value,detail='',direction=None):
    css='positive' if direction and direction>0 else 'negative' if direction and direction<0 else ''
    st.markdown(f'<div class="price-label">{escape(label)}</div><div class="price-value">{escape(value)}</div><div class="price-detail {css}">{escape(detail)}</div>',unsafe_allow_html=True)


def price_chart(points,reference=None,height=120):
    if not points:
        st.caption('В выбранном периоде нет опубликованных цен.');return
    data=pd.DataFrame(points)
    date_format='%d.%m %H:%M' if (data.date.max()-data.date.min()).total_seconds()<86400 else '%d.%m'
    chart=alt.Chart(data).mark_line(color='#7A1F2B',strokeWidth=2,point=alt.OverlayMarkDef(size=34,filled=True,color='#7A1F2B')).encode(
        x=alt.X('date:T',title=None,scale=alt.Scale(type='utc'),axis=alt.Axis(format=date_format,tickCount=3,labelColor='#637387')),
        y=alt.Y('price:Q',title=None,scale=alt.Scale(zero=False),axis=alt.Axis(format=',.0f',tickCount=3,labelColor='#637387')),
        detail='segment:N',tooltip=[alt.Tooltip('checked_at:N',title='Получено (UTC)'),alt.Tooltip('price:Q',title='Цена',format=',.2f'),alt.Tooltip('run:O',title='Запуск')])
    if reference is not None:
        line=alt.Chart(pd.DataFrame([{'reference':reference}])).mark_rule(color='#18756A',strokeDash=[5,4]).encode(y='reference:Q',tooltip=[alt.Tooltip('reference:Q',title='Наша текущая цена',format=',.2f')])
        chart=chart+line
    st.altair_chart(chart.properties(height=height,background='#FFFFFF').configure_view(stroke=None).configure_axis(gridColor='#EDF0F3'),width='stretch')


def visible_rows(rows):
    output=[]
    for row in rows:
        output.append({'Источник':SOURCES[row['source']].label,'Производитель':row['manufacturer'],'Артикул':row['article'],
            'Цена':float(row['price']) if row.get('price') else None,'Валюта':row.get('currency'),
            'Статус':STATUS_LABELS.get(row.get('status'),row.get('status')),
            'Наличие':AVAILABILITY_LABELS.get(row.get('availability'),row.get('availability')),
            'Проверено (UTC)':row.get('checked_at'),'HTTP':row.get('http_status'),
            'Карточка':row.get('product_url') or row.get('url'),'Примечание':row.get('detail')})
    return output


def result_table(rows):
    st.dataframe(visible_rows(rows),hide_index=True,width='stretch',column_config={
        'Карточка':st.column_config.LinkColumn(display_text='Открыть'),'Цена':st.column_config.NumberColumn(format='%.2f')})
