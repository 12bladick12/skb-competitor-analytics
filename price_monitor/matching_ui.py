"""Price comparison with the agreed compact layout and explicit match evidence."""
from collections import Counter
from datetime import date, timedelta
from html import escape
import math

import streamlit as st

from .comparison import series, metrics
from .exchange import xlsx_bytes, csv_bytes
from .matching import VERSION, STATUS_LABELS, FIELD_LABELS, display, load_catalog, evaluate, matching_export
from .matching_normalize import normalize_sensor
from .presentation import heading, money, metric_html, price_chart
from .sources import SOURCES
from .models import STATUS_LABELS as PRICE_STATES

PROFILES={'auto':'По указанному исполнению','general':'Общее применение','cold':'Холодный климат','hot':'Высокая температура'}


@st.cache_data(show_spinner=False,max_entries=128,ttl=900)
def suggestions(record,profile,version=VERSION):
    _,matcher=load_catalog()
    reference=normalize_sensor(record)
    return reference,matcher.suggest(reference,profile)


def reference_price(item,match,matcher):
    """An old price belongs only to its saved model, never to a new suggestion."""
    stored=matcher.resolve(item.get('our_article'))
    if match and match.status not in ('incompatible','unsupported') and stored and stored.id==match.candidate.id:return item.get('our_price')
    return None


def match_badge(status,proposed=False):
    label=STATUS_LABELS.get(status,'Требует проверки')
    st.markdown(f'<span class="match-badge match-{escape(status)}">{escape(label)}</span>'+('<span class="match-proposed">Предложение</span>' if proposed else ''),unsafe_allow_html=True)


def comparison_row(item,reference,match,options,history,profile,library,matcher):
    rid=item['rule_id'];saved=matcher.resolve(item.get('our_article'))
    price=reference_price(item,match,matcher)
    currency_ours=item.get('our_currency') or 'RUB'
    proposed=bool(match and (not saved or saved.id!=match.candidate.id))
    with st.container(border=True,key=f'compare_card_{rid}'):
        info,chart,competitor,ours=st.columns([2.8,3.1,1.5,1.5],vertical_alignment='center')
        with info:
            st.markdown(f'<div class="row-meta">{escape(item["manufacturer"])} · {escape(SOURCES[item["source"]].label)}</div><div class="row-title">{escape(item["article"])}</div>',unsafe_allow_html=True)
            if match:
                st.markdown(f'<div class="match-arrow">↓ СКБ Индукция</div><div class="row-title match-our-model">{escape(match.candidate.model)}</div>',unsafe_allow_html=True)
                match_badge(match.status,proposed)
                if match.missing:st.caption(f'Уточнить характеристик: {len(match.missing)}')
                elif match.differences:st.caption(f'Отличий: {len(match.differences)} · приоритет {match.priority or "—"}')
            else:
                if item.get('our_article'):st.caption('Ручная связь: '+item['our_article'])
                match_badge('unsupported' if reference.family not in (None,'inductive') else 'review')
            st.markdown(f'[История модели](?workspace=prices&price_section=history&model={rid})')
        data=[x for x in history if x['rule_id']==rid]
        currencies=list(dict.fromkeys(x['currency'] for x in data if x['status']=='priced' and x.get('currency')))
        currency=currencies[0] if currencies else item.get('last_currency') or 'RUB'
        if len(currencies)>1:
            with competitor:currency=st.selectbox('Валюта графика',currencies,key=f'currency_{rid}')
        points=series(data,currency)
        latest,change,baseline,gap=metrics(points,price,currency_ours,currency)
        with chart:price_chart(points,baseline,height=130)
        with competitor:
            movement=(f'{change:+.1f}% за период' if change is not None else 'Одна цена или нет наблюдений')
            metric_html('Цена конкурента',money(latest,currency),movement,change)
            if points:st.caption(points[-1]['date'].strftime('%d.%m.%Y · UTC'))
            if data and data[-1]['status']!='priced':st.caption(PRICE_STATES.get(data[-1]['status'],data[-1]['status']))
        with ours:
            detail=f'{gap:+.1f}% к нашей цене' if gap is not None else ('Разные валюты' if price and currency_ours!=currency else 'Укажите цену этой модели' if match else 'Выберите нашу модель')
            metric_html('Наша текущая цена',money(price,currency_ours),detail,gap)
            if proposed:st.caption('Сопоставление ещё не сохранено')
            if item.get('our_price') and price is None:st.caption('Прежний ориентир: '+money(item['our_price'],currency_ours)+' · к другой или непроверенной модели')
        with st.expander('Характеристики и варианты подбора',expanded=False):
            if match:
                for note in match.notices:st.info(note)
                st.caption('Назначение: '+PROFILES.get(match.profile,match.profile)+'. Результат относится к сравнению цен; для установки нужны условия применения.')
                if match.fields:st.dataframe(match.rows(),hide_index=True,width='stretch')
                if match.candidate.source_row:
                    catalog,_=load_catalog()
                    st.link_button('Наша карточка в базе номенклатуры',catalog['source_url']+f'#gid=1326881636&range=A{match.candidate.source_row}:H{match.candidate.source_row}')
            elif reference.family not in (None,'inductive'):
                st.info('Сейчас автоматический подбор настроен для индуктивных датчиков. Для этого типа правила будут добавлены отдельно.')
            else:
                st.info('Подходящий вариант не подтверждён. Проверьте тип, характеристики конкурента и обязательные условия; неизвестные значения не считаются совпадением.')
            if item.get('our_article') and not saved:st.caption('Сохранённое обозначение нашей модели не найдено однозначно в справочнике. Оно сохранено как ручная связь.')
            if options:
                counts=Counter(m.status for m in options)
                st.caption('Варианты: '+ ' · '.join(f'{STATUS_LABELS[s]}: {counts[s]}' for s in ('direct','close','review') if counts[s]))
                st.caption('Одинаковый приоритет сохраняет альтернативы: для несопоставимых уступок произвольные веса не назначаются.')
                st.dataframe([{'Приоритет':m.priority,'Наша модель':m.candidate.model,'Результат':STATUS_LABELS[m.status],
                               'Отличия':'; '.join(FIELD_LABELS[x['key']]+': '+display(x['reference'],x['key'])+' → '+display(x['candidate'],x['key']) for x in m.differences),
                               'Нужно уточнить':'; '.join(FIELD_LABELS[x['key']] for x in m.missing)} for m in options],hide_index=True,width='stretch',height=230)
                by_id={m.candidate.id:m for m in options}
                default=match.candidate.id if match and match.candidate.id in by_id else options[0].candidate.id
                widget=f'match_choice_{rid}_{profile}'
                if st.session_state.get(widget) not in by_id:st.session_state[widget]=default
                selected=st.selectbox('Наша модель для сопоставления',list(by_id),format_func=lambda v:f'{by_id[v].candidate.model} · {STATUS_LABELS[by_id[v].status]} · приоритет {by_id[v].priority}',key=widget)
                selected_match=by_id[selected]
                # The price input is keyed to the model identity: switching models
                # cannot reuse an entered price from a different candidate.
                with st.form(f'match_save_{rid}_{selected}_{profile}'):
                    left,right=st.columns([3,1])
                    old=reference_price(item,selected_match,matcher)
                    current=left.text_input('Текущая цена выбранной модели',value=str(old or ''),placeholder='Цена необязательна',key=f'match_price_{rid}_{selected}_{profile}')
                    curr=right.selectbox('Валюта нашей цены',['RUB','USD','EUR','CNY'],index=['RUB','USD','EUR','CNY'].index(currency_ours),key=f'match_currency_{rid}_{selected}_{profile}')
                    if st.form_submit_button('Сохранить сопоставление',type='primary'):
                        try:
                            library.save_comparisons([{**item,'our_article':selected_match.candidate.model,'our_price':current,'our_currency':curr}])
                            st.toast('Модель и её цена сохранены');st.rerun()
                        except ValueError as exc:st.error(str(exc))
            with st.expander('Исходные характеристики конкурента'):
                attrs=(item.get('_specifications') or {}).get('attributes') or []
                if attrs:st.dataframe(attrs,hide_index=True,width='stretch')
                else:st.caption('В собранной карточке характеристики отсутствуют.')


def render_comparison(library,import_comparisons,downloads):
    heading('Сравнение цен','Конкурент → наша модель. Прямые аналоги в основном сравнении, близкие варианты и неполные данные — отдельно.')
    catalog,matcher=load_catalog()
    with st.expander('Настройки и загрузка сравнения'):
        import_comparisons()
        st.caption(f'Наша номенклатура: {len(matcher.products):,} активных индуктивных датчиков · снимок {catalog["snapshot_date"]}. Цены задаются отдельно.'.replace(',',' '))
        st.link_button('Алгоритмы подбора','?workspace=prices&price_section=sources&source_tab=algorithms')
    search,purpose,dates=st.columns([2,1.6,2])
    query=search.text_input('Найти в сравнении',placeholder='Артикул или производитель',key='matching_search')
    profile=purpose.selectbox('Назначение подбора',list(PROFILES),format_func=PROFILES.get,key='matching_profile',help='«По исполнению» использует явно указанное назначение. Одна низкая Tmin сама по себе не включает холодный профиль.')
    period=dates.date_input('Период истории (UTC)',value=(date.today()-timedelta(days=90),date.today()),format='DD.MM.YYYY',key='compare_dates')
    if len(period)!=2:st.info('Выберите начало и конец периода');return
    with st.spinner('Сопоставляем характеристики выбранных моделей…'):
        items=library.export_products(query=query,selected=True)
        prepared=[]
        for item in items:
            # Prices and history are deliberately excluded from matching inputs.
            record={k:item.get(k) for k in ('rule_id','article','title','category','_specifications')}
            reference,options=suggestions(record,profile)
            saved=matcher.resolve(item.get('our_article'))
            match=next((m for m in options if saved and m.candidate.id==saved.id),None)
            if saved and match is None:match=evaluate(reference,saved,profile)
            if not item.get('our_article') and options:match=options[0]
            status=match.status if match else ('unsupported' if reference.family not in (None,'inductive') else 'review')
            prepared.append((item,reference,match,options,status))
    if not items:st.info('Добавьте позиции из «Базы товаров» или загрузите выборку Excel. Подбор выполняется по собранным характеристикам.');return
    counts=Counter(row[4] for row in prepared)
    review_count=sum(counts[s] for s in ('review','incompatible','unsupported'))
    groups={'direct':f'Прямые аналоги · {counts["direct"]}','close':f'Близкие аналоги · {counts["close"]}','review':f'Требуют проверки · {review_count}','all':f'Все · {len(items)}'}
    default='direct' if counts['direct'] else 'close' if counts['close'] else 'review'
    group=st.radio('Группа сопоставления',list(groups),index=list(groups).index(default),format_func=groups.get,horizontal=True,key='matching_group')
    filtered=[row for row in prepared if group=='all' or row[4]==group or (group=='review' and row[4] in ('incompatible','unsupported'))]
    if not filtered:st.info('В этой группе нет позиций. Остальные варианты доступны в соседних группах.');return
    pages=max(1,math.ceil(len(filtered)/10))
    page=st.number_input('Страница',min_value=1,max_value=pages,value=1,key=f'matching_page_{query}_{profile}_{group}_{len(filtered)}') if pages>1 else 1
    visible=filtered[(page-1)*10:page*10]
    st.caption(f'{len(filtered)} позиций · бордовая линия — конкурент, зелёный пунктир — текущая цена сохранённой нашей модели. Валюты, НДС и упаковка автоматически не уравниваются.')
    history=library.history([row[0]['rule_id'] for row in visible],period[0].isoformat(),(period[1]+timedelta(days=1)).isoformat())
    for item,reference,match,options,_ in visible:comparison_row(item,reference,match,options,history,profile,library,matcher)
    with st.expander('Изменить ручные связи, цены и примечания'):
        st.caption('При смене обозначения нашей модели цена очищается. Задать цену новой модели можно в её блоке сопоставления.')
        edits=[{k:row[0].get(k) or '' for k in ('rule_id','article','our_article','our_price','our_currency','note')} for row in visible]
        changed=st.data_editor(edits,hide_index=True,width='stretch',disabled=['rule_id','article'],key=f'manual_matching_{query}_{group}_{page}',column_config={
            'rule_id':None,'article':'Конкурент','our_article':'Наша модель','our_price':'Наша цена','our_currency':st.column_config.SelectboxColumn('Валюта',options=['RUB','USD','EUR','CNY'],required=True),'note':'Примечание'})
        if st.button('Сохранить ручные изменения'):
            previous={r[0]['rule_id']:r[0] for r in visible}
            for row in changed:
                if row['our_article']!=previous[row['rule_id']].get('our_article'):row['our_price']=None
            try:library.save_comparisons(changed);st.toast('Изменения сохранены');st.rerun()
            except ValueError as exc:st.error(str(exc))
        ids=[row[0]['rule_id'] for row in visible]
        remove=st.selectbox('Убрать позицию',[None]+ids,format_func=lambda v:'Выберите позицию' if v is None else next(row[0]['article'] for row in visible if row[0]['rule_id']==v),key='matching_remove')
        if st.button('Убрать из сравнения',disabled=remove is None):library.remove(remove);st.rerun()
    with st.expander('Выгрузить сопоставление и цены'):
        rows=[];properties=[]
        for item,reference,match,_,status in visible:
            rows.append({'Конкурент':item['article'],'Производитель':item['manufacturer'],'Наша модель':match.candidate.model if match else item.get('our_article'),
                         'Результат':STATUS_LABELS[status],'Сохранено':bool(match and matcher.resolve(item.get('our_article')) and matcher.resolve(item.get('our_article')).id==match.candidate.id),
                         'Наша цена':reference_price(item,match,matcher),'Валюта нашей цены':item.get('our_currency'),
                         'Цена конкурента':item.get('last_price'),'Валюта конкурента':item.get('last_currency'),'Дата цены конкурента':item.get('price_checked_at'),
                         'Профиль':PROFILES[match.profile] if match else PROFILES[profile],'Версия алгоритма':VERSION})
            if match:properties.extend(matching_export(reference,match))
        st.download_button('Сопоставление XLSX',xlsx_bytes(rows,extra_sheets={'Характеристики и отличия':properties}),file_name='inductive_comparison.xlsx',key='matching_export_xlsx')
        st.download_button('Сопоставление CSV',csv_bytes(rows),file_name='inductive_comparison.csv',key='matching_export_csv')
        st.caption('Сопоставления на текущей странице. История ниже содержит наблюдения за выбранный период.')
        downloads(history,'comparison_history','competitor_history')
