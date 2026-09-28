from html import escape

import altair as alt
import pandas as pd
import streamlit as st

from .models import STATUS_LABELS,AVAILABILITY_LABELS
from .sources import SOURCES

CSS='''<style>
.stApp {background:#F4F6F8;color:#32262B;font-family:'Segoe UI',Arial,sans-serif}
.block-container {max-width:1560px;padding:3.3rem 2rem 3rem}
h1 {font-size:2rem!important;font-weight:750!important;letter-spacing:-.04em}
h2 {font-size:1.3rem!important;letter-spacing:-.02em}
h3 {font-size:1.05rem!important}
[data-testid="stSidebar"] {background:#421923;border-right:0}
[data-testid="stSidebar"] * {color:#fff}
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] p {color:#d3bcc2!important}
[data-testid="stSidebar"] a {color:#f1dfe4!important}
[data-testid="stSidebar"] [role="radiogroup"] {gap:6px}
[data-testid="stSidebar"] [role="radio"] {display:none}
[data-testid="stSidebar"] label {padding:10px 14px!important;border-radius:8px}
[data-testid="stSidebar"] label:has(input:checked) {background:#7A1F2B}
[data-testid="stSidebar"] [data-testid="stRadioOption"] {width:100%;border-radius:8px;padding:10px 14px}
[data-testid="stSidebar"] [data-testid="stRadioOption"]:has(input:checked) {background:#7A1F2B}
[class*="st-key-source_card_"],[class*="st-key-compare_card_"] {background:#fff!important;border-color:#E3E8EE!important;border-radius:12px!important}
[data-testid="stVerticalBlockBorderWrapper"]>div {border-color:#E3E8EE!important;border-radius:12px!important;background:white}
[data-testid="stMetric"] {background:#fff;border:1px solid #E3E8EE;border-radius:12px;padding:15px 18px}
[data-testid="stMetricLabel"] {color:#637387;font-size:.8rem}
[data-testid="stMetricValue"] {font-size:1.6rem}
[data-testid="stCaptionContainer"] p {color:#637387!important}
.eyebrow {color:#7A1F2B;font-size:.73rem;font-weight:700;letter-spacing:.12em;margin-bottom:8px}
.brand {font-size:1.55rem;font-weight:750;line-height:1.1;margin:20px 0 8px}
.brand small {display:block;font-size:.78rem;font-weight:400;color:#d3bcc2;margin-top:10px}
.row-title {font-size:.94rem;font-weight:700;overflow-wrap:anywhere;margin:2px 0 5px}
.row-meta {font-size:.75rem;color:#637387;line-height:1.6}
.price-label {font-size:.72rem;color:#637387;margin-top:6px}
.price-value {font-size:1.17rem;font-weight:700;margin:4px 0}
.price-detail {font-size:.74rem;color:#637387}
.positive {color:#AE3838}.negative {color:#18756A}
[data-testid="stDataFrame"] {border-radius:10px;overflow:hidden}
[data-testid="stExpander"] {background:#fff;border-radius:10px}
@media(max-width:800px){.block-container{padding:2rem .9rem}}
</style>'''


def heading(title,subtitle):
    st.markdown('<div class="eyebrow">СКБ · МОНИТОРИНГ РЫНКА</div>',unsafe_allow_html=True)
    st.title(title);st.caption(subtitle)


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
        x=alt.X('date:T',title=None,axis=alt.Axis(format=date_format,tickCount=3,labelColor='#637387')),
        y=alt.Y('price:Q',title=None,scale=alt.Scale(zero=False),axis=alt.Axis(format=',.0f',tickCount=3,labelColor='#637387')),
        detail='segment:N',tooltip=[alt.Tooltip('date:T',title='Получено (UTC)',format='%d.%m.%Y %H:%M'),alt.Tooltip('price:Q',title='Цена',format=',.2f'),alt.Tooltip('run:O',title='Запуск')])
    if reference is not None:
        line=alt.Chart(pd.DataFrame([{'reference':reference}])).mark_rule(color='#18756A',strokeDash=[5,4]).encode(y='reference:Q',tooltip=[alt.Tooltip('reference:Q',title='Наша текущая цена',format=',.2f')])
        chart=chart+line
    st.altair_chart(chart.properties(height=height).configure_view(stroke=None).configure_axis(gridColor='#EDF0F3'),width='stretch')


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
