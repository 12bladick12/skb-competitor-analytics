from pathlib import Path
from datetime import date, timedelta
from html import escape
import atexit
import json
import logging
import math
import time

import pandas as pd
import streamlit as st

from price_monitor.exchange import COLUMNS,read_table,validate_rows,csv_bytes,xlsx_bytes,rules_rows
from price_monitor.comparison import template_bytes,read_comparisons,series,metrics,COLUMNS as COMPARISON_COLUMNS
from price_monitor.library import Library
from price_monitor.models import STATUS_LABELS,RUN_LABELS
from price_monitor.presentation import CSS,heading,money,metric_html,price_chart,result_table
from price_monitor.sources import SOURCES
from price_monitor.storage import Store
from cloud.portal import sources_footer

ROOT=Path(__file__).resolve().parent.parent
CLOUD_MODE=globals().get('CLOUD_MODE',False)
st.html(CSS)


@st.cache_resource
def services(cloud_mode,version='analytics-no-passports-2026-09-30-v1'):
    from price_monitor.scope import refresh_scope
    from price_monitor.retired_documents import disable_document_jobs,stop_document_threads
    stop_document_threads()
    if not cloud_mode:
        store=Store();disable_document_jobs(store.catalog);refresh_scope(store.catalog);return store,None
    from price_monitor.cloud import EmbeddedWorker
    settings=dict(st.secrets['database'])
    store=Store(postgres=settings);disable_document_jobs(store.catalog);refresh_scope(store.catalog);worker=EmbeddedWorker(store)
    atexit.register(worker.close)
    return store,worker


try:db,cloud_worker=services(CLOUD_MODE)
except Exception as exc:
    logging.getLogger('price_monitor').error('Storage initialization failed (%s)',type(exc).__name__)
    if st.query_params.get('price_section')=='sources':db=None;cloud_worker=None
    else:
        st.error('Не удалось подключиться к базе. Владельцу нужно проверить настройки подключения в Streamlit.');st.stop()
catalog=db.catalog if db else None
library=Library(catalog) if catalog else None
PAGES={'collect':'Сбор цен','products':'База товаров','compare':'Сравнение цен','runs':'Запуски'}
CATALOG_STATES={'pending':'В очереди','running':'Сбор идёт','completed':'Завершён','partial':'Завершён с пропусками','blocked':'Источник остановлен: см. причину','cancelled':'Остановлен'}


def go(section,model=None):
    st.query_params['workspace']='prices'
    st.query_params['price_section']=section
    if model is not None:st.query_params['model']=str(model)
    elif 'model' in st.query_params:del st.query_params['model']


def navigation_changed():go(st.session_state['price_navigation'])


section=st.query_params.get('price_section','collect')
if section not in (*PAGES,'history','sources'):section='collect'
if st.session_state.get('_price_navigation_section')!=section or 'price_navigation' not in st.session_state:
    st.session_state['price_navigation']=section if section in PAGES else 'compare'
    st.session_state['_price_navigation_section']=section
st.sidebar.radio('МОНИТОРИНГ ЦЕН',list(PAGES),index=None,format_func=PAGES.get,key='price_navigation',on_change=navigation_changed)
st.sidebar.caption('10 производителей · 5 сайтов')
st.sidebar.divider()
st.sidebar.markdown('[Источники и правила сбора](?workspace=prices&price_section=sources)')
with st.sidebar.expander('О данных'):
    st.caption('Общая база и список сравнения. Изменения видны всем посетителям.')
    st.caption('Время — UTC. Валюты и условия цены сохраняются как у источника.')
    st.caption('Подбор индуктивных датчиков · проверка обозначений производителей')


def page_number(total,size,key):
    pages=max(1,math.ceil(total/size))
    if pages==1:
        st.caption(f'{total:,} позиций'.replace(',',' '));return 0
    pager,_=st.columns([1,4])
    value=pager.number_input('Страница',min_value=1,max_value=pages,value=1,step=1,key=key)
    st.caption(f'{total:,} позиций · страница {value} из {pages}'.replace(',',' '))
    return (value-1)*size


def downloads(rows,key,name='prices'):
    if not rows:return
    from price_monitor.product_exports import export_tables
    rows,properties=export_tables(library.with_specifications(rows))
    a,b=st.columns(2)
    a.download_button('Скачать XLSX',xlsx_bytes(rows,extra_sheets={'Характеристики':properties}),file_name=name+'.xlsx',key=key+'xlsx')
    b.download_button('Скачать CSV',csv_bytes(rows),file_name=name+'.csv',key=key+'csv')
    st.caption('Характеристики включены отдельными столбцами; в XLSX также есть лист «Характеристики».')


def source_connection():
    if db is None:
        st.caption('Состояние сборщиков недоступно без подключения к базе. Правила и алгоритмы доступны ниже.');return
    route=next((r for r in db.external_sources() if r['source']=='sensoren'),None)
    if route and route['enabled']:
        if route['heartbeat'] and time.time()-route['heartbeat']<50:
            if route['protocol']>=2:st.caption('● Sensoren подключён через внешний сборщик')
            else:st.warning('Внешний сборщик Sensoren нужно перезапустить, чтобы он получил поддержку полного каталога.')
        else:st.warning('Sensoren ждёт подключения внешнего сборщика. Включите компьютер со сборщиком; очередь сохранена.')
    elif CLOUD_MODE:st.warning('Для Sensoren нужен внешний сборщик: облачный адрес получает HTTP 403. Подключение описано в разделе «Источники».')
    return route


def catalog_progress(run_id, sensoren_route=None):
    from price_monitor.runtime import PHASES
    progress=catalog.progress(run_id)
    if not progress:return False
    view=[]
    for item in progress:
        phase=item.get('work_phase')
        offline = (item['source']=='sensoren' and sensoren_route and sensoren_route['enabled']
                   and time.time()-(sensoren_route['heartbeat'] or 0)>=50)
        if offline:phase=None
        activity_age=max(0,time.time()-(item.get('activity_at') or time.time()))
        if item['state']=='running':
            label=SOURCES[item['source']].label
            lease=item.get('lease_heartbeat') or 0
            if time.time()-lease>=120:
                st.warning(f'{label}: нет связи со сборщиком. Очередь сохранена.')
            elif phase and activity_age>=180:
                st.warning(f'{label}: нет активности основного потока {int(activity_age)} с; контроль сборщика проверяет восстановление.')
            else:
                st.caption(f"{label} · {PHASES.get(phase,'Подготовка сборщика')} · "
                           f"осталось карточек: {item.get('products_left',0):,} · "
                           f"пропущено по правилам: {item.get('skipped',0):,}".replace(',',' '))
                if item.get('health_detail'):st.caption(f"{label} · восстановление: {item['health_detail']}")
        view.append({'Источник':SOURCES[item['source']].label,'Производители':', '.join(json.loads(item['brands_json'])),
            'Состояние':CATALOG_STATES.get(item['state'],item['state']),'Страниц найдено':item['pages'],
            'Текущий этап':'Ожидание внешнего сборщика' if offline else PHASES.get(phase,'—'),'Восстановлений связи':item.get('recoveries') or 0,
            'Обработано страниц':item['visited'],'Карточек найдено':item['cards'],
            'Карточек обработано':item['cards_visited'],'Позиций сохранено':item['positions'],
            'Уже собрано в месяце':item['monthly_skipped'],
            'Разделов осталось':item['navigation_left'],
            'Ошибок':item['failures'],'Последняя обработка (UTC)':item['last_checked'],'Примечание':item['detail']})
    st.dataframe(view,hide_index=True,width='stretch')
    st.caption('Обработанные страницы включают пропуски по производителю и правилам источника: при их проверке число сохранённых позиций не растёт. Категории и карты сайта проверяются для поиска новых товаров. «Разделов осталось» включает карты сайта; ошибки показаны отдельно.')
    return True


@st.fragment(run_every=10)
def active_progress():
    runs=db.runs();active=next((r for r in runs if r['state'] in ('queued','running')),None)
    heartbeat=db.lease()
    st.caption('● Облачный сборщик подключён' if heartbeat and time.time()-heartbeat<40 else '○ Облачный сборщик запускается или ожидает подключения')
    sensoren_route=source_connection()
    st.caption('Состояние обновлено: '+time.strftime('%H:%M:%S UTC',time.gmtime())+' · Автообновление каждые 10 с')
    if active:
        with st.container(border=True):
            st.subheader(f"Запуск №{active['id']} · {RUN_LABELS[active['state']]}")
            if not catalog_progress(active['id'],sensoren_route):
                st.progress(active['finished']/max(1,active['total']),text=f"Обработано {active['finished']} из {active['total']}")
            if st.button('Остановить сбор',key='stop_active'):
                db.cancel(active['id']);st.rerun()
    elif runs:
        st.caption(f"Последний запуск №{runs[0]['id']}: {RUN_LABELS[runs[0]['state']]} · обработано {runs[0]['finished']} позиций")


def details_panel(rule_id):
    details=library.details(rule_id)
    from price_monitor.matching_normalize import normalize_sensor
    from price_monitor.notation_ui import render_decoding
    rows=library.products(rule_id=rule_id)[1]
    if rows:
        normalized=normalize_sensor(library.with_specifications(rows)[0])
        render_decoding(normalized)
        if normalized.conflicts:st.warning('Противоречивые характеристики требуют проверки: '+', '.join(sorted(normalized.conflicts)))
    if not details:
        st.info('Исходные характеристики карточки появятся после её повторного сбора. Расшифровка обозначения показана отдельно, если она доступна. Исторические цены уже сохранены.');return
    if details.get('description'):st.write(details['description'])
    if details.get('attributes'):
        st.dataframe(pd.DataFrame(details['attributes']).rename(columns={'name':'Характеристика','value':'Значение','group':'Группа'}),hide_index=True,width='stretch')
    else:st.caption('Характеристики не опубликованы в распознанном блоке страницы или блок требует обновления адаптера.')
    st.download_button('Характеристики JSON',json.dumps(details,ensure_ascii=False,indent=2),file_name=f'product_{rule_id}.json',key=f'details_{rule_id}')


def collect_page():
    heading('Сбор цен','Полный каталог выбранных производителей, опубликованные цены и характеристики товаров.')
    from price_monitor.monthly import month_window
    period=month_window()
    st.info(f"Период сбора: {period['month']} (UTC). Успешно проверенные карточки пропускаются до следующего календарного месяца. Ошибки можно обработать повторно.")
    active_progress()
    st.subheader('Выберите конкурентов')
    selection={}
    competitors=[(source,brand) for source,spec in SOURCES.items() for brand in spec.brands]
    for offset in range(0,len(competitors),5):
        columns=st.columns(5)
        for column,(source,brand) in zip(columns,competitors[offset:offset+5]):
            with column,st.container(border=True,key=f'source_card_{source}_{brand}'):
                if st.checkbox(brand,value=True,key=f'brand_{source}_{brand}'):selection.setdefault(source,[]).append(brand)
                st.caption(SOURCES[source].host)
    left,right=st.columns(2)
    all_clicked=left.button('Запустить сбор по всем конкурентам',type='primary',width='stretch')
    selected_clicked=right.button('Запустить сбор по конкретным конкурентам',disabled=not selection,width='stretch')
    if all_clicked or selected_clicked:
        try:
            run_id=db.enqueue_catalog({s:list(spec.brands) for s,spec in SOURCES.items()} if all_clicked else selection)
            st.session_state['selected_run']=run_id;st.toast(f'Запуск №{run_id} сохранён');st.rerun()
        except ValueError as exc:st.error(str(exc))
    st.caption('Память сбора и история хранятся в базе. Повторный запуск в этом месяце дополняет каталог новыми товарами и повторяет неудачные проверки. В новом месяце цены собираются заново; запуск остаётся ручным.')
    with st.expander('Собрать только модели из Excel / CSV'):
        st.write('Для небольшого списка используйте source, manufacturer, article и product_url (или url_template).')
        a,b=st.columns(2)
        a.download_button('Шаблон заданий',xlsx_bytes([],COLUMNS,'Задания'),file_name='tasks_template.xlsx')
        b.download_button('Примеры заданий',(ROOT/'examples/tasks.csv').read_bytes(),file_name='tasks_examples.csv')
        uploaded=st.file_uploader('Файл заданий',type=['xlsx','csv'],max_upload_size=10,key='tasks')
        if uploaded:
            try:
                rules,errors=validate_rows(read_table(uploaded.getvalue(),uploaded.name))
                if errors:st.error('Исправьте строки перед запуском');st.dataframe(errors,hide_index=True)
                if rules:st.dataframe(rules_rows(rules),hide_index=True,width='stretch')
                if st.button('Собрать модели из файла',disabled=bool(errors) or not rules):
                    st.session_state['selected_run']=db.enqueue(rules);st.rerun()
            except ValueError as exc:st.error(str(exc))


def products_page():
    heading('База товаров','Поиск по артикулу, названию и категории. Отметьте позиции для сравнения цен.')
    summary=library.summary();a,b,c=st.columns(3)
    a.metric('Товаров в базе',summary['products']);b.metric('С характеристиками',summary['with_specs']);c.metric('В сравнении',summary['selected'])
    with st.container(key='product_filters'):
        a,b,c=st.columns([3,1.3,1.3])
        query=a.text_input('Поиск номенклатуры',placeholder='Артикул, название или категория')
        source=b.selectbox('Источник',['']+list(SOURCES),format_func=lambda x:SOURCES[x].label if x else 'Все источники')
        brands=list(SOURCES[source].brands) if source else [x for s in SOURCES.values() for x in s.brands]
        brand=c.selectbox('Производитель',['']+brands,format_func=lambda x:x or 'Все производители')
    from price_monitor.characteristic_audit import render_audit
    render_audit(library,query,source,brand)
    total,_=library.products(query,source,brand,limit=1)
    offset=page_number(total,50,'products_page_'+str(hash((query,source,brand))))
    _,rows=library.products(query,source,brand,offset=offset)
    if not rows:st.info('По этому запросу товаров пока нет. Запустите сбор или измените фильтры.');return
    view=[]
    for row in rows:
        view.append({'Выбрать':False,'Артикул':row['article'],'Производитель':row['manufacturer'],'Источник':SOURCES[row['source']].label,
            'Последняя цена':float(row['last_price']) if row['last_price'] else None,'Валюта':row['last_currency'],
            'Дата цены (UTC)':row['price_checked_at'],'Последняя проверка':STATUS_LABELS.get(row['status'],row['status']),
            'Состояние товара':{'active':'Активен','review':'Требует проверки','archived':'Архив','discontinued':'Снят с производства'}.get(row['product_state'],row['product_state']),
            'Проверка карточки':row['specifications_checked_at'],
            'Характеристик':row['attributes_count'],'В сравнении':'Да' if row['selected'] else '',
            'История':f"?workspace=prices&price_section=history&model={row['rule_id']}"})
    edited=st.data_editor(view,hide_index=True,width='stretch',disabled=[x for x in view[0] if x!='Выбрать'],key=f'products_{query}_{source}_{brand}_{offset}',
        column_config={'Выбрать':st.column_config.CheckboxColumn(),'История':st.column_config.LinkColumn(display_text='История'),'Последняя цена':st.column_config.NumberColumn(format='%.2f')})
    wanted=[row['rule_id'] for row,edit in zip(rows,edited) if edit['Выбрать']]
    a,b=st.columns(2)
    if a.button(f'Добавить выбранные в сравнение ({len(wanted)})',type='primary',disabled=not wanted):
        library.select(wanted);st.toast('Позиции добавлены в общий список');st.rerun()
    if b.button('Перейти к сравнению'):go('compare');st.rerun()
    st.caption('«Последняя цена» — последнее успешное получение цены с указанной датой. Ошибка последней проверки не заменяет цену нулём.')
    with st.expander('Карточка товара и характеристики'):
        rid=st.selectbox('Модель на этой странице',[r['rule_id'] for r in rows],format_func=lambda x:next(r['article'] for r in rows if r['rule_id']==x))
        item=next(r for r in rows if r['rule_id']==rid)
        if item['product_url']:st.link_button('Карточка на сайте',item['product_url'])
        details_panel(rid)
    with st.expander('Выгрузить найденные товары'):
        st.caption('Экспорт текущих фильтров, до 10 000 товаров. Для большего каталога уточните источник, производителя или поиск.')
        if st.button('Подготовить выгрузку',disabled=total>10000):
            result=library.export_products(query,source,brand)
            st.session_state['products_export']=(query,source,brand,result)
        ready=st.session_state.get('products_export')
        if ready and ready[:3]==(query,source,brand):downloads(ready[3],'base','products')


def import_comparisons():
    with st.expander('Загрузить выборку и наши цены из Excel / CSV'):
        st.download_button('Скачать шаблон Excel с инструкцией',template_bytes(),file_name='comparison_template.xlsx')
        st.caption('Импорт находит точный артикул конкурента в базе. Подбор нашей модели выполняется по характеристикам; явная связь и цена из файла сохраняются как заданные вами.')
        uploaded=st.file_uploader('Файл сравнения',type=['xlsx','csv'],max_upload_size=10,key='comparison_import')
        if uploaded:
            try:
                valid,errors=read_comparisons(uploaded.getvalue(),uploaded.name)
                matches,lookup_errors=library.resolve(valid);errors+=lookup_errors
                if errors:st.error('Исправьте все ошибки перед сохранением');st.dataframe(errors,hide_index=True,width='stretch')
                if matches:st.dataframe([{k:r.get(k) for k in COMPARISON_COLUMNS} for r in matches],hide_index=True,width='stretch')
                if st.button('Сохранить выборку и наши цены',disabled=bool(errors) or not matches):
                    library.save_comparisons(matches);st.success(f'Сохранено позиций: {len(matches)}')
            except ValueError as exc:st.error(str(exc))


def compare_page():
    from price_monitor.matching_ui import render_comparison
    render_comparison(library,import_comparisons,downloads)


def runs_page():
    heading('Запуски и результаты','Журнал сбора, состояние каталогов и причины пропусков.')
    if st.button('Обновить журнал'):st.rerun()
    total=db.runs_count()
    offset=page_number(total,50,'runs_history_page')
    runs=db.runs(limit=50,offset=offset)
    if not runs:st.info('Запусков пока нет.');return
    ids=[r['id'] for r in runs];chosen=st.session_state.get('selected_run',ids[0])
    run_id=st.selectbox('Запуск',ids,index=ids.index(chosen) if chosen in ids else 0,format_func=lambda value:next(f"№{r['id']} · {r['created_at']} · {RUN_LABELS[r['state']]}" for r in runs if r['id']==value))
    run=next(r for r in runs if r['id']==run_id)
    a,b=st.columns(2);a.metric('Обработано заданий',run['finished']);b.metric('Всего заданий',run['total'])
    is_catalog=catalog_progress(run_id)
    if is_catalog and run['state'] in ('cancelled','completed_with_errors'):
        if st.button('Продолжить необработанные страницы'):
            try:catalog.resume(run_id);go('collect');st.rerun()
            except ValueError as exc:st.error(str(exc))
    if is_catalog:
        issues=catalog.issues(run_id)
        if issues:
            with st.expander('Ошибки страниц (первые 100)'):st.dataframe(issues,hide_index=True,width='stretch')
    visible_total=library.result_count(run_id)
    if visible_total<run['total']:
        st.caption(f"Из результатов исключено {run['total']-visible_total} позиций без подтверждения производителя ТЕКО. Исходная история сохранена.")
    offset=page_number(visible_total,100,f'run_page_{run_id}')
    rows=library.result_page(run_id,offset)
    if rows:result_table(rows);downloads(rows,f'run_{run_id}_{offset}',f'run_{run_id}_page_{offset//100+1}')
    elif run['state'] in ('completed','completed_with_errors','cancelled'):
        st.info('Новых результатов в этом запуске нет. Уже собранные за месяц товары доступны в «Базе товаров» и предыдущих запусках.')
    else:st.info('Очередь страниц формируется. Позиции появятся после обработки карточек.')
    if rows and any(row.get('status')=='already_collected' for row in rows):
        st.caption('«Уже собрано в этом месяце» — сохранённый результат с исходной датой проверки. Такие строки не добавляют точки в историю цен.')
    st.caption('Таблица и выгрузка показывают текущую страницу. Полная база доступна в разделе «База товаров».')


def history_page():
    heading('История модели','Все проверки конкретного артикула, опубликованные цены и характеристики.')
    try:rid=int(st.query_params.get('model','0'))
    except ValueError:rid=0
    _,items=library.products(rule_id=rid,limit=1)
    if not items:st.info('Выберите ссылку «История» в базе или сравнении.');return
    item=items[0];st.subheader(item['article']);st.caption(item['manufacturer']+' · '+SOURCES[item['source']].label)
    a,b,c=st.columns(3)
    a.link_button('Назад к сравнению','?workspace=prices&price_section=compare');b.link_button('База товаров','?workspace=prices&price_section=products')
    if item['product_url']:c.link_button('Товар на сайте',item['product_url'])
    if st.button('Добавить в сравнение'):library.select([rid]);st.toast('Позиция добавлена')
    history=library.history([rid]);currencies=list(dict.fromkeys(x['currency'] for x in history if x['status']=='priced'))
    if currencies:
        currency=st.selectbox('Валюта истории',currencies)
        price_chart(series(history,currency),height=220)
    rows=[{**item,**x} for x in history]
    result_table(rows);downloads(rows,'model_'+str(rid),'model_'+str(rid))
    with st.expander('Характеристики последней распознанной карточки',expanded=True):details_panel(rid)


def sources_page():
    heading('Источники и правила сбора','Источники данных, правила сбора и алгоритмы сопоставления номенклатуры.')
    default='Алгоритмы подбора' if st.query_params.get('source_tab')=='algorithms' else 'Источники и правила сбора'
    sources,algorithms=st.tabs(['Источники и правила сбора','Алгоритмы подбора'],default=default)
    with sources:
        collection_rules=ROOT/'docs'/'prices'/'PRODUCT_UPDATES.md'
        if collection_rules.exists():
            with st.expander('Постоянная база и обновление товаров'):st.markdown(collection_rules.read_text(encoding='utf-8'))
        st.dataframe([{'Источник':s.label,'Сайт':'https://'+s.host,'Производители':', '.join(s.brands),'Данные':'Карты сайта, каталог, карточки и характеристики'} for s in SOURCES.values()],hide_index=True,width='stretch')
        source_connection()
        st.info('При проверке браузера/CAPTCHA, HTTP 403/429 или запрете robots.txt источник останавливается. Причина отказа сохраняется в журнале.')
        for name,label in [('AUDIT_2026_09_30.md','BESKONTA, СЕНСОР и ТЕКО: исправления 30.09.2026'),('SENSOREN_RECOVERY_2026_09_29.md','Sensoren: устранение зависания и проверка восстановления'),('MONTHLY_COLLECTION.md','Память сбора и обновление по месяцам'),('AUDIT_2026_09_29.md','Sensoren, ТЕКО и характеристики: аудит 29.09.2026'),('CATALOGS.md','Полные каталоги и характеристики'),('SENSOREN.md','Подключение Sensoren'),('AUDIT.md','Первичный аудит источников')]:
            path=ROOT/'docs'/'prices'/name
            if path.exists():
                with st.expander(label):st.markdown(path.read_text(encoding='utf-8'))
    with algorithms:
        from price_monitor.notation_ui import render_registry
        render_registry()
        enrichment_rules=ROOT/'docs'/'prices'/'INDUCTIVE_ENRICHMENT.md'
        if enrichment_rules.exists():
            with st.expander('Заполненность характеристик и дозаполнение индуктивных датчиков'):
                st.markdown(enrichment_rules.read_text(encoding='utf-8'))
        from price_monitor.algorithms import render_algorithms
        render_algorithms()


try:
    {'collect':collect_page,'products':products_page,'compare':compare_page,'runs':runs_page,'history':history_page,'sources':sources_page}[section]()
    sources_footer()
except Exception:
    logging.getLogger('price_monitor').exception('Page failed: %s',section)
    st.error('Не удалось выполнить действие. Уже сохранённые данные остаются в базе. Обновите страницу; если ошибка повторится, проверьте журнал приложения.')
