"""All-time archive inside the existing News workspace."""
import streamlit as st
from cloud.drive_store import StorageError
from .gate import released, stale
from .model import ROLES, safe_url
from .service import CourtService

TITLE='Судебные дела и исполнительные производства'
DOMAINS={'arbitral':'Арбитраж','general':'Общая юрисдикция','magistrate':'Мировые судьи',
         'supreme':'Верховный суд','constitutional':'Конституционный суд',
         'enforcement':'ФССП','bankruptcy':'Банкротство','identity':'ФНС'}
STATES={'success':'Проверено','partial':'Проверено частично','error':'Ошибка',
        'access_denied':'Доступ ограничен','challenge':'Требуется CAPTCHA','rate_limited':'Лимит запросов'}


def value(item,key):
    v=item.get(key)
    return 'Не получено' if v is None or v=='' else v


def amount(v):
    return 'Не получено' if v is None else str(v['amount'])+' '+v['currency']


def render(library,settings,identity):
    from cloud.downloads import asset_download
    service=CourtService(settings,identity);ident=library['id']
    st.subheader(TITLE)
    st.caption('Архив за всё время. Период в меню относится к новостной сводке.')
    try:
        overview=service.overview(ident)
        if not released(overview['release']):
            st.warning('Внутренняя проверка. Сбор неполный; судебные сведения пока не включены в общие новости и отчёты.')
        cols=st.columns(3)
        for col,kind,label in zip(cols,('court_case','court_event','court_enforcement'),('Дела','Процессуальные события','Исполнительные производства')):
            col.metric(label,overview['counts'].get(kind,0))
        with st.expander('Актуальность и полнота источников',expanded=True):
            checks=overview['checks']
            if not checks:st.info('Источники ещё не проверены. Отсутствие записей не означает отсутствие дел.')
            else:
                st.dataframe([{'Юрлицо / ИНН':c['entity_id'],'Источник':c['source'],
                    'Проверка':STATES.get(c['status'],c['status']),'Последняя попытка':c['checked_at'],
                    'Последняя полная проверка':c.get('last_success_at') or 'Не выполнена',
                    'Старше 36 часов':stale(c),'Подробности':c['reason']} for c in checks],hide_index=True)
        entities=overview['entities']
        brands={'':'Все бренды',**{e['brand']:e['brand_name'] for e in entities}}
        brand=st.selectbox('Бренд',list(brands),format_func=brands.get)
        choices={'':'Все юридические лица',**{e['id']:e['name']+' · '+e['inn'] for e in entities if not brand or e['brand']==brand}}
        entity=st.selectbox('Юридическое лицо',list(choices),format_func=choices.get)
        kind=st.radio('Реестр',['court_case','court_enforcement'],format_func=lambda k:'Дела' if k=='court_case' else 'Исполнительные производства',horizontal=True)
        query=st.text_input('Номер дела, производства или предмет спора')
        a,b,c=st.columns(3)
        role=a.selectbox('Роль',['',*ROLES],format_func=lambda x:ROLES.get(x,'Все роли'))
        domain=b.selectbox('Судопроизводство',['',*DOMAINS],format_func=lambda x:DOMAINS.get(x,'Все виды'))
        status=c.text_input('Статус из источника',help='Оставьте пустым для всех статусов.')
        with st.expander('Даты и суммы'):
            limit_dates=st.checkbox('Ограничить дату регистрации')
            start=st.date_input('Зарегистрировано с') if limit_dates else None
            end=st.date_input('Зарегистрировано по') if limit_dates else None
            st.caption('Для дел фильтруется сумма требований; для производств — опубликованный остаток.')
            limit_amount=st.checkbox('Ограничить сумму')
            low=st.number_input('Минимум',min_value=0.0) if limit_amount else ''
            high=st.number_input('Максимум',min_value=0.0,value=1000000.0) if limit_amount else ''
            currency=st.selectbox('Валюта',['RUB','USD','EUR']) if limit_amount else ''
        filters=dict(kind=kind,entity=entity,role=role,jurisdiction=domain,status=status,query=query,
                     date_from=start.isoformat() if start else '',date_to=end.isoformat() if end else '',
                     amount_from=low,amount_to=high,currency=currency,brand=brand)
        signature=repr(filters)
        if st.session_state.get('court_filters')!=signature:
            st.session_state['court_page']=1;st.session_state['court_filters']=signature
        page=st.number_input('Страница архива',min_value=1,step=1,key='court_page')
        count,rows=service.archive(ident,offset=(page-1)*20,**filters)
        st.caption(f'Найдено: {count}. На странице до 20 записей.')
        if not rows:st.info('В сохранённых данных по этим условиям записей нет. Проверьте полноту источников выше.')
        for item in rows:
            with st.expander(item['number']+' · '+str(value(item,'court'))):
                fields=('jurisdiction','status','subject','registered_on','next_hearing','judge','as_of','checked_at')
                labels=('Вид','Статус','Предмет','Дата регистрации','Следующее заседание','Судья','Состояние на дату','Последняя проверка')
                st.dataframe([{'Поле':l,'Значение':str(value(item,k))} for k,l in zip(fields,labels)],hide_index=True)
                monetary=(('claim_amount','Требования'),('award_amount','Присуждено'),('costs','Расходы')) if kind=='court_case' else (('original_amount','Сумма производства'),('balance','Опубликованный остаток'),('fee','Сбор'))
                for key,label in monetary:st.write(label+': '+amount(item.get(key)))
                st.dataframe([{'Участник':p.get('name') or p.get('inn'),'ИНН':p.get('inn'),
                               'Роль':ROLES.get(p.get('role'),p.get('role'))} for p in item.get('participants',[])],hide_index=True)
                for key,label in [('basis','Основание исполнения'),('department','Подразделение ФССП'),('closed_reason','Основание окончания')]:
                    if kind=='court_enforcement':st.write(label+': '+str(value(item,key)))
                if item.get('limitations'):st.caption(item['limitations'])
                if item.get('conflicts'):
                    st.warning('Источники содержат различающиеся сведения.');st.json(item['conflicts'])
                if safe_url(item.get('url')):st.link_button('Открыть источник',item['url'])
                if item.get('linked_cases'):st.write('Связанные дела:',item['linked_cases'])
                if kind=='court_case':
                    detail_page=st.number_input('Страница истории',min_value=1,step=1,key='history_'+item['id'])
                    for event in service.details(ident,item['id'],'court_event',(detail_page-1)*50):
                        st.write((event.get('occurred_on') or 'Дата не опубликована')+' · '+event['title'])
                        st.caption(event.get('description') or '')
                    for doc in service.details(ident,item['id'],'court_document',(detail_page-1)*50):
                        if safe_url(doc.get('url')):st.link_button(doc['title'],doc['url'])
                        for proof in doc.get('evidence_ids',[]):
                            st.download_button('Сохранённая копия',asset_download(settings,identity,ident,proof),
                                file_name=proof+'.html.txt',key=item['id']+doc['id']+proof,on_click='ignore')
                st.caption('Сумма требований не равна установленной задолженности.')
        with st.expander('Юридические лица и подтверждение принадлежности'):
            st.dataframe([{'Бренд':e['brand_name'],'Название':e['name'],'ИНН':e['inn'],'ОГРН':e['ogrn'],
                'Названия':', '.join(e['names']),'Адрес':e.get('address') or 'Не получен',
                'ФНС проверена':e.get('fns_verified_at') or 'Не проверена',
                'История проверена':e.get('history_verified',False),'Источники':'; '.join(e['membership_sources'])} for e in entities],hide_index=True)
    except (PermissionError,ValueError,StorageError) as exc:st.error(str(exc))
