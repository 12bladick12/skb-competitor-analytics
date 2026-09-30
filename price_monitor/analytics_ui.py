"""Brand and execution summaries, with inspectable denominators and prices."""
from collections import defaultdict
from datetime import timedelta

import streamlit as st

from .exchange import xlsx_bytes, csv_bytes
from .matching import evaluate
from .matching_normalize import normalize_sensor
from .price_analytics import model_statistics, aggregate, functional_group, execution_group

BASES={'internet':'Интернет-цена','gross':'С НДС','net':'Без НДС'}


def price_conditions(item, library):
    with st.expander('Условия цен и НДС'):
        st.caption('Подтвердите условия по карточке или прайс-листу. Ставка не выбирается автоматически. Условия конкурента относятся только к текущей цене, а не ко всей истории.')
        stored=item.get('_price_terms') or {}
        values={}
        for column, side, label in zip(st.columns(2), ('ours','competitor'), ('СКБ ИНДУКЦИЯ','Конкурент')):
            with column:
                st.markdown('**'+label+'**')
                old=stored.get(side) or {}
                basis=st.selectbox('НДС в исходной цене', ['unknown','gross','net'],
                                   index=['unknown','gross','net'].index(old.get('basis','unknown')),
                                   format_func=lambda v:{'unknown':'Не подтверждён','gross':'Включён','net':'Не включён'}[v], key=f'term_basis_{side}_{item["rule_id"]}')
                rate=st.text_input('Ставка НДС, %',value='' if old.get('rate') is None else str(old['rate']), key=f'term_rate_{side}_{item["rule_id"]}')
                unit=st.checkbox('Цена за 1 штуку подтверждена',value=old.get('unit')=='piece', key=f'term_unit_{side}_{item["rule_id"]}')
                values[side]={'basis':basis,'rate':rate,'unit':'piece' if unit else ''}
        if st.button('Сохранить условия цен',key=f'terms_save_{item["rule_id"]}'):
            try:
                for term in values.values():term['rate']=float(term['rate'].replace(',','.')) if term['rate'].strip() else None
                values['competitor']['price_checked_at']=str(item.get('price_checked_at'))
                library.save_price_terms(item['rule_id'],values['ours'],values['competitor'])
                st.toast('Условия цен сохранены');st.rerun()
            except (ValueError,TypeError):st.error('Укажите ставку числом от 0 до 100 или оставьте поле пустым.')


def render_summary(library, matcher, period, profile, selected_items):
    st.subheader('Обзор цен')
    with st.container(key='analysis_filters'):
        scope, price = st.columns([2,1])
        scope_value=scope.radio('Выборка обзора',['В сравнении','Вся база товаров'],horizontal=True,key='analytics_scope')
        basis=price.selectbox('Вид цены',list(BASES),format_func=BASES.get,key='analytics_basis')
        items=selected_items if scope_value=='В сравнении' else library.export_products()
        if not items:
            st.info('Выборка пуста. Выберите всю базу или добавьте товары в сравнение.')
            return basis
        price_terms=library.price_terms()
        normalized=[(item,normalize_sensor(item)) for item in items]
        brand_col,family_col,execution_col=st.columns(3)
        brands=brand_col.multiselect('Бренды',sorted({item['manufacturer'] for item in items}),key='analytics_brands',placeholder='Все бренды')
        available=[(item,s) for item,s in normalized if not brands or item['manufacturer'] in brands]
        families=family_col.multiselect('Функциональные группы',sorted({functional_group(s) for _,s in available}),key='analytics_families',placeholder='Все функциональные группы')
        available=[(item,s) for item,s in available if not families or functional_group(s) in families]
        executions=execution_col.multiselect('Исполнения',sorted({execution_group(s) for _,s in available}),key='analytics_executions',placeholder='Все исполнения')
        available=[(item,s) for item,s in available if not executions or execution_group(s) in executions]
    histories=defaultdict(list)
    with st.spinner('Читаем историю цен выбранных товаров…'):
        ids=[item['rule_id'] for item,_ in available]
        for offset in range(0,len(ids),200):
            history=library.history(ids[offset:offset+200],period[0].isoformat(),(period[1]+timedelta(days=1)).isoformat())
            for row in library.with_specifications(history): histories[row['rule_id']].append(row)
    stats=[]
    for item,sensor in available:
        item['_price_terms']=price_terms.get(item['rule_id'],{})
        saved=matcher.resolve(item.get('our_article'))
        direct=bool(saved and evaluate(sensor,saved,profile).status=='direct')
        stats.append(model_statistics(item,sensor,histories[item['rule_id']],basis,item.get('our_price') if saved else None,direct))
    summary=aggregate(stats)
    grouped=aggregate(stats,True)
    with st.container(key='analysis_metrics'):
        one,two,three=st.columns(3)
        one.metric('Моделей в выборке',len(stats))
        two.metric('С динамикой за период',sum(r['change'] is not None for r in stats))
        three.metric('С подтверждённой Δ',sum(r['gap'] is not None for r in stats))
    if summary:
        primary=('Бренд','Среднее изменение, %','Мин. изменение, %','Макс. изменение, %','Δ к СКБ, %','С динамикой','Подтверждённых пар с ценами')
        compact=[{'Бренд':r['Бренд'],'Условия':f"{r['Валюта']} · {r['Единица']} · {r['НДС в источнике']}"+(f" {r['Ставка НДС, %']:g}%" if r['Ставка НДС, %'] is not None else ''),**{k:r[k] for k in primary if k!='Бренд'}} for r in summary]
        with st.container(key='brand_overview'):
            st.subheader('Динамика по брендам')
            st.caption('Изменение за выбранный период · валюты, единицы и условия НДС разделены')
            st.dataframe(compact,hide_index=True,width='stretch',row_height=40,placeholder='—',
                         column_config={
                             **{k:st.column_config.NumberColumn(format='%.2f') for k in primary if '%' in k},
                             'Среднее изменение, %':st.column_config.NumberColumn('Среднее, %',format='%.2f',help='Среднее изменение по моделям с одинаковым весом.'),
                             'Мин. изменение, %':st.column_config.NumberColumn('Минимум, %',format='%.2f'),
                             'Макс. изменение, %':st.column_config.NumberColumn('Максимум, %',format='%.2f'),
                             'Подтверждённых пар с ценами':st.column_config.NumberColumn('Пар для Δ',help='Сохранённые прямые аналоги с подтверждёнными сопоставимыми условиями цен.'),
                         })
        with st.expander('Условия и полнота расчёта по брендам'):
            st.dataframe(summary,hide_index=True,width='stretch',placeholder='—')
    else:st.info('По выбранным фильтрам данных нет.')
    with st.expander('Функциональные группы и исполнения',expanded=True):
        st.caption('Исполнение: корпус · диаметр · схема выхода · функция · монтаж · Sn · подключение. Неизвестные значения выделяются отдельно.')
        st.dataframe(grouped,hide_index=True,width='stretch',placeholder='—')
    with st.expander('Как рассчитываются изменение цены и Δ'):
        st.caption('Изменение модели = (последняя цена / первая цена − 1) × 100% за выбранный период; нужны минимум два наблюдения. Среднее — по моделям с одинаковым весом. Мин./макс. включают снижение. Валюты, единицы и условия НДС разделены.')
        st.caption('Δ = цена конкурента − цена СКБ ИНДУКЦИЯ; Δ% = Δ / цена СКБ × 100. Плюс означает, что конкурент дороже. Расчёт только для сохранённых прямых аналогов, текущей цены и подтверждённых сопоставимых условий. Группы исполнения сами по себе не подтверждают взаимозаменяемость.')
    detail=[{'Бренд':r['brand'],'Модель':r['model'],'Группа':r['family'],'Исполнение':r['execution'],
             'Интернет-цена':r['internet'],'С НДС':r['gross'],'Без НДС':r['net'],'Валюта':r['currency'],
             'Изменение, %':r['change'],'Δ конкурент − СКБ':r['delta'],'Δ к СКБ, %':r['gap'],'Дата цены':r['checked_at']} for r in stats]
    with st.expander('Цены товаров и выгрузка обзора'):
        st.dataframe(detail,hide_index=True,width='stretch',placeholder='—')
        if stats:
            a,b=st.columns(2)
            a.download_button('Обзор XLSX',xlsx_bytes(summary,extra_sheets={'Группы и исполнения':grouped,'Цены товаров':detail}),file_name='price_analytics.xlsx',key='analytics_xlsx')
            b.download_button('Цены товаров CSV',csv_bytes(detail),file_name='price_analytics.csv',key='analytics_csv')
    st.caption('Пустое значение НДС означает, что ставка или состав исходной цены не подтверждены. Условия можно сохранить в карточке сопоставления.')
    return basis
