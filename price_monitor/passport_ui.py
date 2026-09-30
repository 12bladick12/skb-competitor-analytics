"""Passport viewer and explicit, per-product drawing review."""
import json
import uuid
import time
import pandas as pd
import streamlit as st
from .passports import Passports
from .passport_transport import SupabaseFiles,MAX_BYTES
from .passport_recognition import FIELDS,inspect_pdf,pdf_module
from .passport_review import approve
from .passport_sources import classify_pdf

STATES={'pending':'В очереди','downloaded':'Скачан','not_published':'Не опубликован',
        'unavailable':'Недоступен','review':'Требует проверки','active':'Активен',
        'discontinued':'Снят с производства','archived':'Архив'}


def settings():
    try:return dict(st.secrets.get('passports',{}))
    except Exception:return {}


def render_progress(repository):
    st.caption('Сбор карточек и распознавание паспортов завершаются независимо. Выключение локального обработчика не останавливает цены.')
    queue=Passports(repository).progress()
    if queue:
        labels={'download':'Получение паспортов','recognize':'Распознавание','manual':'Ручное прикрепление'}
        states={'pending':'В очереди','processing':'Обрабатывается','done':'Завершено','retry':'Ожидает повторной попытки'}
        display=[{**r,'kind':labels.get(r['kind'],r['kind']),'state':states.get(r['state'],r['state'])} for r in queue]
        st.dataframe(pd.DataFrame(display).rename(columns={'kind':'Очередь','state':'Состояние','total':'Заданий'}),hide_index=True,width='stretch')
    workers=repository.batch('SELECT state,detail,heartbeat FROM passport_workers WHERE heartbeat>%(after)s',{'after':int(time.time())-120})
    if not workers:st.caption('Обработчики паспортов сейчас не подключены. Задания сохраняются в базе.')
    metrics=repository.batch('''SELECT j.kind,count(*) attempts,sum(m.requests) requests,sum(m.received_bytes) received_bytes,
        sum(m.pages) pages,sum(m.seconds) seconds FROM passport_job_metrics m JOIN passport_jobs j ON j.id=m.job_id GROUP BY j.kind''')
    if metrics:
        with st.expander('Затраты на обработку'):st.dataframe(metrics,hide_index=True,width='stretch')


def render_document(repository,rule_id):
    passports=Passports(repository);product=passports.product(rule_id)
    if not product:return
    st.markdown('**Паспорт и геометрия**')
    st.caption('Карточка: '+str(product.get('card_checked_at') or 'не проверена')+' · Паспорт: '+
               str(product.get('document_checked_at') or 'не проверен')+' · '+STATES.get(product.get('document_state'),'В очереди'))
    if product.get('document_note'):st.caption(product['document_note'])
    config=settings();files=SupabaseFiles(config) if config.get('service_key') and config.get('url') else None
    if not files:st.info('Общее закрытое хранилище ещё не подключено. Настройки: passports.url и passports.service_key.')
    if st.button('Проверить паспорт сейчас',key=f'passport_refresh_{rule_id}'):
        repository.batch('INSERT INTO passport_products(rule_id) VALUES(%(id)s) ON CONFLICT(rule_id) DO NOTHING',{'id':rule_id})
        passports.enqueue(rule_id,'download',{'period':time.strftime('%Y-%m'),'manual':True},key=str(uuid.uuid4()))
        st.success('Проверка добавлена в очередь')
    links=repository.batch('''SELECT DISTINCT f.*,l.url,l.applicability,l.evidence FROM passport_links l
        JOIN passport_files f ON f.fingerprint=l.fingerprint WHERE l.rule_id=%(id)s ORDER BY f.created_at DESC''',{'id':rule_id})
    fp=product.get('current_fingerprint')
    if links:
        versions=list(dict.fromkeys(r['fingerprint'] for r in links))
        chosen=st.selectbox('Редакция паспорта',versions,index=versions.index(fp) if fp in versions else 0,
            format_func=lambda h:next(r['created_at'] for r in links if r['fingerprint']==h)+' · '+h[:12],key=f'passport_version_{rule_id}')
        selected=next(r for r in links if r['fingerprint']==chosen)
        st.caption('Редакция документа: '+(selected.get('revision_text') or 'дата не указана')+' · Получен: '+selected['created_at'])
        st.caption('Применимость: '+('подтверждена' if selected['applicability']=='confirmed' else 'требует проверки')+' · '+selected['evidence'])
        if files:
            if st.button('Открыть паспорт',key=f'passport_open_{rule_id}'):
                st.link_button('Просмотреть PDF — ссылка действует 10 минут',files.signed_url(chosen))
            if st.toggle('Показать чертёж и извлечённые характеристики',key=f'passport_preview_{rule_id}'):
                raw=files.get(chosen)
                page=st.number_input('Страница',min_value=1,max_value=selected['page_count'],value=1,key=f'passport_page_{rule_id}')
                fitz=pdf_module()
                with fitz.open(stream=raw,filetype='pdf') as pdf:
                    sheet=pdf[int(page)-1];scale=min(1.5,2000/max(sheet.rect.width,sheet.rect.height))
                    image=sheet.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False).tobytes('png')
                st.image(image,width='stretch')
                render_review(repository,rule_id,chosen,fp,selected)
        else:render_review(repository,rule_id,chosen,fp,selected)
        if st.button('Повторно распознать выбранный паспорт',key=f'recognize_{rule_id}'):
            passports.reprocess(rule_id,chosen);st.success('Задание сохранено. Его выполнит локальный обработчик.')
    if files:
        with st.expander('Прикрепить паспорт вручную'):
            uploaded=st.file_uploader('Технический паспорт PDF',type=['pdf'],key=f'upload_passport_{rule_id}')
            if st.button('Сохранить паспорт',disabled=uploaded is None,key=f'save_passport_{rule_id}'):
                raw=uploaded.getvalue()
                if len(raw)>MAX_BYTES:raise ValueError('Паспорт превышает 50 МБ')
                pdf=inspect_pdf(raw);classification=classify_pdf(pdf['text'],product['article'],product.get('title') or '')
                if not classification['accepted']:st.error(classification['reason']);return
                repository.batch('INSERT INTO passport_products(rule_id) VALUES(%(id)s) ON CONFLICT(rule_id) DO NOTHING',{'id':rule_id})
                job_id=passports.enqueue(rule_id,'manual',{},key=str(uuid.uuid4()))
                owner='manual-'+str(uuid.uuid4())
                jobs=repository.batch("UPDATE passport_jobs SET state='processing',owner=%(owner)s,lease_until=%(lease)s WHERE id=%(id)s AND state='pending' RETURNING *",
                    {'id':job_id,'owner':owner,'lease':int(time.time())+900})
                if not jobs:raise ValueError('Задание уже обрабатывается')
                fingerprint,key=files.put(raw)
                passports.register(jobs[0],owner,fingerprint,key,len(raw),pdf['pages'],classification,'')
                passports.finish(job_id,owner);st.success('Паспорт сохранён.');st.rerun()
    with st.expander('Журнал изменений товара'):
        events=repository.batch('SELECT created_at,kind,detail FROM product_events WHERE rule_id=%(id)s ORDER BY created_at DESC LIMIT 100',{'id':rule_id})
        st.dataframe(events,hide_index=True,width='stretch')


def render_review(repository,rule_id,fp,current,selected):
    rows=repository.batch('SELECT result_json FROM passport_geometry WHERE fingerprint=%(fp)s ORDER BY updated_at DESC LIMIT 1',{'fp':fp})
    result=json.loads(rows[0]['result_json']) if rows else {'fields':[],'notes':''}
    if result.get('notes'):st.caption(result['notes'])
    if fp!=current:st.caption('Историческая редакция: подтверждать значения можно для текущего паспорта.');return
    saved=repository.batch('SELECT fields_json,reviewer FROM passport_field_reviews WHERE rule_id=%(id)s AND fingerprint=%(fp)s',{'id':rule_id,'fp':fp})
    fields=json.loads(saved[0]['fields_json']) if saved else result['fields']
    if not fields:st.info('Распознавание ещё не предложило геометрию. Можно заполнить подтверждённые значения вручную с указанием страницы и области.')
    table=[{**f,'Подтвердить':bool(saved),'bbox':json.dumps(f['bbox'])} for f in fields]
    if not table:table=[{'Подтвердить':False,'name':'active_length_mm','value':'','unit':'mm','page':1,'bbox':'[0, 0, 1, 1]','evidence':'','model':'','datum':''}]
    for row in table:
        row['value']=str(row['value']);row['name']=FIELDS[row['name']]
    with st.form(f'geometry_review_{rule_id}_{fp}'):
        edited=st.data_editor(pd.DataFrame(table),hide_index=True,width='stretch',num_rows='dynamic',
            column_config={'name':st.column_config.SelectboxColumn('Характеристика',options=list(FIELDS.values())),
                'Подтвердить':st.column_config.CheckboxColumn(),'value':'Значение','page':'Страница',
                'bbox':'Область [x0,y0,x1,y1], доли страницы','evidence':'Основание','datum':'Откуда и докуда измерено','model':'Исполнение'})
        reviewer=st.text_input('Проверил сотрудник',value=saved[0]['reviewer'] if saved else '')
        applies=st.checkbox('Подтверждаю применимость выбранного чертежа к этому исполнению',value=selected['applicability']=='confirmed')
        submitted=st.form_submit_button('Сохранить подтверждённые значения')
    if submitted:
        try:
            accepted=[]
            for row in edited.to_dict('records'):
                if not row.pop('Подтвердить',False):continue
                row['name']=next(k for k,v in FIELDS.items() if v==row['name'])
                row['bbox']=json.loads(row['bbox']);row['page']=int(row['page'])
                if row['name'].endswith('_mm'):row['value']=float(str(row['value']).replace(',','.'))
                accepted.append(row)
            approve(repository,rule_id,fp,accepted,reviewer,applies)
        except (ValueError,TypeError,KeyError) as exc:
            st.error('Проверьте заполнение: '+str(exc));return
        st.success('Сохранены только отмеченные значения.');st.rerun()


def render_page(repository):
    st.markdown('### Паспорта и проверка геометрии')
    render_progress(repository)
    query=st.text_input('Поиск паспорта по артикулу или производителю').strip()
    rows=repository.batch('''SELECT q.id,q.manufacturer,q.article,p.state,p.note,p.checked_at
        FROM passport_products p JOIN rules q ON q.id=p.rule_id
        WHERE lower(q.article||' '||q.manufacturer) LIKE %(query)s
        ORDER BY CASE p.state WHEN 'review' THEN 0 WHEN 'unavailable' THEN 1 ELSE 2 END,q.manufacturer,q.article LIMIT 200''',{'query':'%'+query.casefold()+'%'})
    if not rows:st.info('По запросу нет паспортов.' if query else 'Очередь появится после первичного заполнения и подключения обработчика.');return
    if len(rows)==200:st.caption('Показаны первые 200 позиций. Уточните артикул или производителя для поиска в полной базе.')
    choice=st.selectbox('Товар',rows,format_func=lambda r:f'{r["manufacturer"]} · {r["article"]} · {STATES.get(r["state"],r["state"])}')
    render_document(repository,choice['id'])
