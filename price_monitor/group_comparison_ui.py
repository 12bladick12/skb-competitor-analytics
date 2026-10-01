"""Multi-brand comparison using the existing Streamlit presentation."""
from collections import defaultdict, deque
from datetime import date, timedelta, datetime
from html import escape
import math

import altair as alt
import pandas as pd
import streamlit as st

from .automatic_price_terms import load_terms, refresh_terms, automatic_refresh
from .comparison_groups import ComparisonIndex, OUR_BRAND, BRAND_COLORS, price_info, delta_between, history_points
from .catalog_search_ui import render_search
from .exchange import xlsx_bytes
from .matching import load_catalog, VERSION, STATUS_LABELS, FIELD_LABELS, display
from .own_prices import OwnPrices
from .presentation import heading, money
from .sources import SOURCES

BASES={'gross':'С НДС','net':'Без НДС','internet':'Как в источнике'}
PROFILES={'auto':'По указанному исполнению','general':'Общее применение','cold':'Холодный климат','hot':'Высокая температура'}
CSS='''<style>
.comparison-model {font-size:15px;font-weight:650;line-height:1.45;overflow-wrap:anywhere;margin-bottom:3px}
.comparison-brand {font-size:12px;font-weight:600;margin-bottom:4px}
.comparison-price {font-size:17px;font-weight:650;white-space:nowrap;line-height:1.6}
.comparison-note {font-size:11px;color:#637387;line-height:1.55;overflow-wrap:anywhere}
.comparison-delta {font-size:13px;font-weight:600;line-height:1.6}
.comparison-head {font-size:11px;color:#637387;text-transform:uppercase;letter-spacing:.06em;padding-bottom:5px}
.comparison-legend {display:flex;flex-direction:column;gap:7px;margin:8px 0 12px;font-size:12px;color:#637387}
.comparison-legend>div {display:flex;gap:7px;align-items:flex-start;overflow-wrap:anywhere}
.comparison-legend i {display:inline-block;width:9px;height:9px;border-radius:50%;flex:0 0 9px;margin-top:4px}
[class*="st-key-group_model_"] {border-bottom:1px solid #edf0f3;padding:10px 0}
[class*="st-key-group_card_"] {background:#fff;border-radius:12px}
@media(max-width:1150px) {
 [class*="st-key-group_card_"]>[data-testid="stHorizontalBlock"],
 [class*="st-key-group_card_"]>div>[data-testid="stHorizontalBlock"] {flex-wrap:wrap}
 [class*="st-key-group_card_"]>[data-testid="stHorizontalBlock"]>[data-testid="stColumn"],
 [class*="st-key-group_card_"]>div>[data-testid="stHorizontalBlock"]>[data-testid="stColumn"] {width:100%;flex:1 1 100%;min-width:0}
}
@media(max-width:640px) {
 .comparison-price {font-size:15px;white-space:normal}
 .comparison-model {font-size:14px}
}
</style>'''


@st.fragment(run_every=3)
def refresh_indicator(job,key):
    if not job.done():
        st.caption('Автоматически уточняем условия цен на сайтах…')
    elif st.session_state.get('finished_terms_'+key)!=id(job):
        st.session_state['finished_terms_'+key]=id(job)
        if job.exception():st.caption('Обновление условий временно недоступно. Сохранённые цены доступны.')
        else:st.rerun()


@st.cache_resource(ttl=300, max_entries=1, show_spinner='Загружаем модели и цены…')
def comparison_index(database_key, version, _library):
    _,matcher=load_catalog()
    rows=deque(_library.comparison_products())
    own_rows=OwnPrices(_library.repo).current()
    own={r['catalog_id']:r for r in own_rows if r['catalog_id']}
    def consume_rows():
        while rows:yield rows.popleft()
    return ComparisonIndex(consume_rows(),matcher.products,own,[r for r in own_rows if not r['catalog_id']],
                           reference_loader=_library.comparison_reference)


def short_date(value):
    if not value:return 'Дата не указана'
    try:return datetime.fromisoformat(str(value)).strftime('%d.%m.%Y')
    except ValueError:return str(value)


def entry_label(row):
    return row['brand']+' · '+row['model']


def price_register(library):
    with st.expander('Прайс СКБ Индукция'):
        prices=OwnPrices(library.repo)
        imports=prices.imports()
        if not imports:
            st.caption('Прайс СКБ ещё не загружен.');return
        latest=imports[0]
        st.caption(f"{latest['source_name']} · {latest['row_count']:,} позиций · {latest['matched_count']:,} связаны с каталогом индуктивных датчиков".replace(',',' '))
        st.caption('Исходные цены без НДС. Цена с НДС = исходная цена × 1,22; округление до копейки. Остальные позиции прайса сохранены отдельно от подбора датчиков.')
        if st.checkbox('Показать цены и строки исходного файла', key='show_own_price_register'):
            query=st.text_input('Найти в прайсе СКБ',key='own_register_search')
            rows=prices.current()
            if query:rows=[r for r in rows if query.casefold() in (r['article']+' '+r['source_model']).casefold()]
            output=[{'Артикул':r['article'],'Наименование':r['source_model'],'Без НДС, ₽':float(r['net_price']),
                     'С НДС 22%, ₽':float(r['gross_price']),'Дата прайса':r['effective_date'],
                     'Модель в каталоге':r['catalog_model'],'Лист':r['source_sheet'],'Строка':r['source_row']} for r in rows]
            st.dataframe(output,hide_index=True,width='stretch',height=320)
            st.download_button('Скачать проверенный прайс',xlsx_bytes(output),file_name='skb_prices_2026-10-01.xlsx')


def render_chart(rows, visible, histories, basis, period, baseline):
    selected=[row for row in rows if row['entry_id'] in visible]
    if not selected:
        st.info('Выберите модели галочками для отображения на графике.');return
    currencies=list(dict.fromkeys(price_info(row)['currency'] for row in selected if price_info(row)['raw'] is not None))
    currency=price_info(baseline)['currency']
    if len(currencies)>1:
        currency=st.selectbox('Валюта графика',currencies,index=currencies.index(currency) if currency in currencies else 0,
                              key='group_chart_currency_'+rows[0]['entry_id']+'_'+baseline['entry_id'])
    elif currencies:currency=currencies[0]
    points=[];missing=[]
    for row in selected:
        if price_info(row)['currency']!=currency:continue
        values=history_points(row,histories.get(row.get('rule_id'),[]),basis)
        values=[v for v in values if period[0].isoformat()<=str(v['date'])[:10]<=period[1].isoformat()]
        if not values:missing.append(row['brand'])
        for value in values:
            value['series']=entry_label(row)
            value['group']=row['entry_id']+':'+value['segment']
            value['observed_at']=short_date(info_date) if (info_date:=price_info(row)['date']) and row['is_ours'] else str(value['date']).replace('T',' ')
        points.extend(values)
    st.markdown('**История цен · '+BASES[basis]+', '+currency+'**')
    if points:
        frame=pd.DataFrame(points)
        frame['date']=pd.to_datetime(frame['date'],utc=True)
        span=(frame['date'].max()-frame['date'].min()).total_seconds()
        ticks='day' if 86400<=span<=14*86400 else 5
        date_format='%d.%m' if span>=86400 else '%d.%m %H:%M'
        labels=list(dict.fromkeys(p['series'] for p in points))
        palette={entry_label(r):BRAND_COLORS.get(r['brand'],'#637387') for r in rows}
        chart=alt.Chart(frame).mark_line(point=alt.OverlayMarkDef(size=60,filled=True),strokeWidth=2.3).encode(
            x=alt.X('date:T',title=None,scale=alt.Scale(type='utc'),axis=alt.Axis(format=date_format,tickCount=ticks,labelFlush=True,labelColor='#637387')),
            y=alt.Y('price:Q',title=currency,scale=alt.Scale(zero=False),axis=alt.Axis(format=',.0f',tickCount=5,labelColor='#637387')),
            color=alt.Color('series:N',title=None,scale=alt.Scale(domain=labels,range=[palette[x] for x in labels]),legend=None),
            detail='group:N',tooltip=[alt.Tooltip('brand:N',title='Производитель'),alt.Tooltip('model:N',title='Модель'),
                alt.Tooltip('observed_at:N',title='Дата цены (UTC)'),alt.Tooltip('price:Q',title='Цена',format=',.2f')])
        st.altair_chart(chart.properties(height=300).configure_view(stroke=None).configure_axis(gridColor='#EDF0F3'),width='stretch')
        legend=''.join(f'<div><i style="background:{palette[label]}"></i><span>{escape(label)}</span></div>' for label in labels)
        st.markdown('<div class="comparison-legend">'+legend+'</div>',unsafe_allow_html=True)
    else:
        st.info('В выбранном периоде нет наблюдений с этим видом цены.')
    if missing:
        st.caption('Нет точек для выбранного периода и вида цены: '+', '.join(dict.fromkeys(missing))+'.')
    st.caption('Точки — полученные цены. Цена СКБ относится к дате прайса; разрывы после ошибок сбора сохранены.')


def render_group(anchor,index,library,profile,basis,period):
    aid=anchor['entry_id']
    with st.spinner('Подбираем аналоги всех производителей…'):
        alternatives=index.alternatives(anchor,profile)
    ordered=sorted(alternatives,key=lambda brand:(brand!=OUR_BRAND,brand))
    rows=[dict(anchor)];matches={}
    with st.expander('Варианты аналогов',expanded=False):
        if not alternatives:st.caption('Для этой модели подходящие варианты пока не найдены.')
        for brand in ordered:
            pairs=alternatives[brand];lookup={r['entry_id']:(r,m) for r,m in pairs}
            options=list(lookup)
            widget='group_variants_'+aid+'_'+brand+'_'+profile
            saved=next((key for key,(r,m) in lookup.items() if r['is_ours'] and r['model']==anchor.get('our_article')),None)
            if widget not in st.session_state:st.session_state[widget]=[saved or options[0]]
            else:st.session_state[widget]=[v for v in st.session_state[widget] if v in lookup]
            chosen=st.multiselect(brand,options,key=widget,
                format_func=lambda key,lookup=lookup:lookup[key][0]['model']+' · '+STATUS_LABELS[lookup[key][1].status])
            for entry_id in chosen:
                row,match=lookup[entry_id];rows.append(dict(row));matches[entry_id]=match
    # Rechecks are attached to copies, never to cached shared records.
    load_terms(library.repo,[r for r in rows if not r['is_ours']])
    if job:=automatic_refresh(library.repo,[r for r in rows if not r['is_ours']]):
        refresh_indicator(job,aid)
    ids=[r['entry_id'] for r in rows]
    lookup={r['entry_id']:r for r in rows}
    default=next((r['entry_id'] for r in rows if r['is_ours']),aid)
    key='group_baseline_'+aid
    if st.session_state.get(key) not in ids:st.session_state[key]=default
    basecol,_=st.columns([3,2])
    base=basecol.selectbox('Считать Δ относительно',ids,format_func=lambda v:entry_label(lookup[v]),key=key)
    baseline=lookup[base]
    with st.container(border=True,key='group_card_'+aid.replace(':','_')):
        table,plot=st.columns([1.45,1],gap='large',vertical_alignment='top')
        visible=set()
        with table:
            cols=st.columns([.36,2.5,1.2,1.05],vertical_alignment='center')
            for col,title in zip(cols,('','Производитель / модель','Текущая цена','Разница')):
                col.markdown('<div class="comparison-head">'+title+'</div>',unsafe_allow_html=True)
            export=[]
            for row in rows:
                entry=row['entry_id'];info=price_info(row);color=BRAND_COLORS.get(row['brand'],'#637387')
                with st.container(key='group_model_'+aid.replace(':','_')+'_'+entry.replace(':','_')):
                    check,model,price,diff=st.columns([.36,2.5,1.2,1.05],vertical_alignment='center')
                    enabled=check.checkbox('На графике: '+entry_label(row),value=info['raw'] is not None,
                        key='group_visible_'+aid+'_'+entry,label_visibility='collapsed',disabled=info['raw'] is None)
                    if enabled:visible.add(entry)
                    with model:
                        st.markdown(f'<div class="comparison-brand" style="color:{color}">{escape(row["brand"])}</div>'
                            f'<div class="comparison-model" style="color:{color if row["is_ours"] else "#1b2b3d"}">{escape(row["model"])}</div>',unsafe_allow_html=True)
                        st.caption('Искомая модель' if entry==aid else 'Подобранный аналог')
                    value=info.get(basis)
                    with price:
                        st.markdown('<div class="comparison-price">'+escape(money(value,info['currency']))+'</div>',unsafe_allow_html=True)
                        if value is None and info['raw'] is not None:
                            st.caption('На сайте: '+money(info['raw'],info['currency'])+' · НДС не определён')
                        st.caption(short_date(info['date']))
                    delta,gap,note=delta_between(row,baseline,basis)
                    with diff:
                        if entry==base:st.caption('База сравнения')
                        elif gap is None:st.caption(note)
                        else:
                            sign='+' if delta>0 else ''
                            st.markdown(f'<div class="comparison-delta">{sign}{escape(money(delta,info["currency"]))}<br>{gap:+.1f}%</div>',unsafe_allow_html=True)
                            if note:st.caption(note)
                            if 'НДС не выровнен' in note:
                                st.caption('Исходные: '+money(info['raw'],info['currency'])+' − '+money(price_info(baseline)['raw'],info['currency']))
                    if not row['is_ours'] and row.get('status') not in ('priced','pending'):
                        st.caption('Последняя полученная цена; последняя проверка: '+str(row.get('status')))
                    export.append({'Производитель':row['brand'],'Модель':row['model'],'Цена':value,'Валюта':info['currency'],
                        'Вид цены':BASES[basis],'Исходная цена':info['raw'],'Дата цены':info['date'],'База Δ':entry_label(baseline),
                        'Δ':delta,'Δ, %':gap,'Оговорка':note,'Источник':info['source_url'] or info['source'],
                        'На графике':enabled,'Статус подбора':STATUS_LABELS[matches[entry].status] if entry in matches else 'Искомая модель'})
        with plot:
            history=defaultdict(list)
            competitor_ids=[r['rule_id'] for r in rows if not r['is_ours']]
            observations=library.with_specifications(library.history(competitor_ids,period[0].isoformat(),(period[1]+timedelta(days=1)).isoformat()))
            for row in observations:history[row['rule_id']].append(row)
            render_chart(rows,visible,history,basis,period,baseline)
            st.caption('Δ = цена модели − цена базы; Δ, % = Δ / цена базы × 100. Галочки меняют только график.')
        with st.expander('Условия цен и НДС'):
            output=[]
            for row in rows:
                info=price_info(row);term=info['terms'];check=row.get('_terms_check') or {}
                output.append({'Производитель':row['brand'],'Модель':row['model'],
                    'НДС в исходной цене':{'gross':'Включён','net':'Не включён'}.get(term.get('basis'),'Не указан'),
                    'Ставка, %':term.get('rate'),'Единица':{'piece':'шт.','pack':'упаковка'}.get(term.get('unit'),'Не указана'),
                    'Без НДС':info['net'],'С НДС':info['gross'],'Валюта':info['currency'],
                    'Источник':info['source_url'] or info['source'],'Проверено':term.get('checked_at') or info['date'],
                    'Подтверждение':term.get('evidence',''),'Статус обновления':check.get('detail','')})
            st.dataframe(output,hide_index=True,width='stretch',column_config={'Источник':st.column_config.TextColumn(width='medium')})
            st.caption('Условия извлекаются при сборе цены. Для ранее собранных карточек можно повторить проверку; при ошибке прежние сведения сохраняются.')
            competitors=[r for r in rows if not r['is_ours']]
            if st.button('Обновить условия с сайтов',key='group_refresh_terms_'+aid,disabled=not competitors):
                with st.spinner('Проверяем условия выбранных карточек…'):
                    outcome=refresh_terms(library.repo,competitors)
                st.session_state['terms_outcome_'+aid]=outcome;st.rerun()
            if st.session_state.get('terms_outcome_'+aid):st.dataframe(st.session_state['terms_outcome_'+aid],hide_index=True,width='stretch')
        with st.expander('Характеристики и отличия аналогов'):
            st.caption('Модели участвуют в сравнении цен. Отличия характеристик учитываются отдельно при выборе замены для установки.')
            for row in rows:
                match=matches.get(row['entry_id'])
                if not match:continue
                st.markdown('**'+entry_label(row)+'** · '+STATUS_LABELS[match.status])
                output=[{'Характеристика':FIELD_LABELS[f['key']],'Искомая модель':display(f['reference'],f['key']),
                         'Аналог':display(f['candidate'],f['key']),'Пояснение':f['reason']} for f in match.fields]
                st.dataframe(output,hide_index=True,width='stretch',height=230)
        st.download_button('Скачать сравнение XLSX',xlsx_bytes(export),file_name='comparison.xlsx',key='group_export_'+aid)
    present={r['brand'] for r in rows}
    absent=[b for spec in SOURCES.values() for b in spec.brands if b not in present and b not in alternatives]
    if absent:st.caption('Подходящие модели не найдены среди собранных карточек: '+', '.join(absent)+'.')


def render_comparison(library,import_comparisons=None,downloads=None):
    st.html(CSS)
    heading('Сравнение цен','Искомая модель и аналоги всех производителей. Текущие цены, НДС и история изменений.')
    prices=OwnPrices(library.repo)
    imports=prices.imports()
    version=VERSION+':'+(imports[0]['batch_id'] if imports else 'no-prices')+':'+str(getattr(library,'data_version',''))
    st.button('Обновить базу поиска',key='catalog_search_refresh',on_click=comparison_index.clear,
              help='Загрузить новые карточки и характеристики после сбора. При работе со страницей база поиска также обновляется каждые 5 минут.')
    index=comparison_index(str(library.repo.path or 'cloud'),version,library)
    candidates=render_search(index)
    profilecol,periodcol=st.columns([1.35,1.6])
    profile=profilecol.selectbox('Назначение подбора',list(PROFILES),format_func=PROFILES.get,key='matching_profile')
    period=periodcol.date_input('Период графика (UTC)',value=(date.today()-timedelta(days=90),date.today()),format='DD.MM.YYYY',key='compare_dates')
    if len(period)!=2:st.info('Выберите начало и конец периода.');return
    basis=st.radio('Вид цены',list(BASES),format_func=BASES.get,horizontal=True,key='group_price_basis')
    if not candidates:
        st.info('Выберите модели в поиске, вставьте список или задайте характеристики.');price_register(library);return
    pages=max(1,math.ceil(len(candidates)/5))
    if st.session_state.get('catalog_search_result_page',1)>pages:st.session_state['catalog_search_result_page']=1
    page=st.number_input('Страница результатов',min_value=1,max_value=pages,key='catalog_search_result_page') if pages>1 else 1
    st.caption(f'Выбрано моделей: {len(candidates)} · страница {page} из {pages}. На странице до 5 сравнений.')
    st.caption('Цены в строках — последние полученные; период ограничивает только график. СКБ Индукция выделена зелёным.')
    for anchor in candidates[(page-1)*5:page*5]:
        st.subheader(entry_label(anchor))
        if anchor['sensor'].family not in (None,'inductive'):
            st.info('Модель найдена по каталогу. Автоматический подбор аналогов этого типа пока не настроен; доступны её цена и история.')
        render_group(anchor,index,library,profile,basis,period)
    price_register(library)
    with st.expander('Обзор по брендам и сохранённые сравнения'):
        if st.checkbox('Показать обзор',key='group_show_overview'):
            from .analytics_ui import render_summary
            _,matcher=load_catalog()
            selected=[r for r in index.records.values() if not r['is_ours'] and r.get('selected')]
            render_summary(library,matcher,period,profile,selected)
