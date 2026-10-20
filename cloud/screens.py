"""Invited-user library screens. Every file action rechecks current access."""

from datetime import datetime, timedelta, timezone
from math import ceil
import re
from urllib.parse import urlsplit

import streamlit as st

from .access import current_access
from .drive_store import DriveStore, StorageError
from .library import Repository, period_events, read_asset
from .readiness import section
from .presentation import html, safe, note, section_heading, event_card, research_card, coverage_summary, competitor_bars
from .editor import render_editor, navigation_guard
from .coverage import monitored, outside_scope


KINDS = {"news": "Новости сайтов", "telegram": "Telegram", "products": "Продукция и предложения",
         "arbitration": "Арбитражные дела", "media": "Упоминания в СМИ"}
OFFICIAL_KINDS = ('news', 'telegram', 'products')
SOURCE_KINDS = {**KINDS, "promotions": "Акции", "website": "Сайт"}
STATUS = {"success": "Проверен", "partial": "Сбор неполный", "error": "Ошибка",
          "not_configured": "Канал не подтверждён", "running": "Выполнялся при переносе",
          "interrupted": "Прерван", "queued": "Ожидание", "completed": "Завершён"}
MONTHS = ("", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
LOCAL = timezone(timedelta(hours=5))


@st.cache_data(ttl=60, max_entries=2, show_spinner=False)
def load_library(database_url):
    # Called only after authorization; cache data never authorizes a request.
    # Short shared cache reduces database wakeups and traffic on the free plan.
    return Repository({"cloud": {"database_url": database_url}}).load()


def url(value):
    try:
        parsed = urlsplit(value or "")
        return value if parsed.scheme in ("http", "https") and parsed.netloc and not parsed.username and not parsed.password else ""
    except ValueError:
        return ""


def local_time(value):
    if not value:
        return "—"
    try:
        result = datetime.fromisoformat(value)
        return (result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result).astimezone(LOCAL).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return "—"


def period_label(value):
    if len(value) == 7:
        return f"{MONTHS[int(value[5:])]} {value[:4]}"
    return value.replace("__", " — ")


def available_periods(library, current):
    periods = set(library['period'])
    for name in ('research_run', 'research_item'):
        for record in (library.get(name) or {}).values():
            if not isinstance(record, dict):
                continue
            candidate = record.get('period')
            if (isinstance(candidate, str) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', candidate)
                    and candidate[:4] != '0000'):
                periods.add(candidate)
    return sorted(periods, reverse=True) or [current]


def report_label(report):
    match = re.fullmatch(r'Аналитическая_записка_конкуренты_(\d{4})_(\d{2})\.docx', report['name'])
    if match and 1 <= int(match[2]) <= 12:
        return 'Аналитическая записка · ' + period_label(f'{match[1]}-{match[2]}')
    return report['name'].removesuffix('.docx').replace('__', ' — ').replace('_', ' ')


def source_reason(check):
    reason=str(check.get('reason') or '')
    if not reason:
        return '—'
    if reason=='Ещё не проверен':
        return reason
    labels={
        'publication_date_missing':'Дата публикации не указана',
        'no_recognized_cards':'Публикации на странице не распознаны',
        'page_not_found':'Страница не найдена',
        'access_block':'Источник ограничил доступ',
        'archive_page_limit':'Проверены не все страницы архива',
        'article_page_limit':'Проверены не все публикации',
        'article_content_missing':'Не удалось прочитать текст публикации',
        'incomplete_product_listing':'Каталог продукции загружен не полностью',
        'baseline_evidence_missing':'Недостаточно данных для сравнения каталога',
        'product_detail_unavailable':'Карточка продукции недоступна',
        'conflicting_publication_dates':'Даты публикации противоречат друг другу',
        'article_listing_date_conflict':'Дата в новости отличается от даты в списке',
        'future_publication_date':'Указана будущая дата публикации',
    }
    found=[text for code,text in labels.items() if code in reason]
    if found:
        return '; '.join(found)
    return 'Источник временно недоступен' if check.get('status')=='error' else 'Проверены не все материалы источника'


def coverage_table(checks, diagnostic=False):
    excluded = outside_scope(checks)
    checks = monitored(checks)
    if excluded:
        st.caption(f'Без настроенного Telegram-канала: {excluded}. Эти компании не учитываются как непроверенные источники.')
    if not checks:
        st.caption("За этот период проверки источников не сохранены.")
        return
    rows = []
    for check in checks:
        state = STATUS.get(check['status'], check['status'])
        if check['status'] == 'success' and check.get('items') == 0:
            state = "Проверен, публикаций нет"
        reason = source_reason(check)
        target = url(check.get('url'))
        source = (f'<a href="{safe(target)}" target="_blank" rel="noopener noreferrer" title="{safe(target)}">'
                  f'{safe(urlsplit(target).netloc)} ↗</a>') if target else '—'
        tone = check['status'] if check['status'] in ('success', 'partial', 'error') else 'muted'
        rows.append(f'<tr><td>{safe(check.get("competitor_name", check["competitor_code"]))}</td>'
                    f'<td>{safe(SOURCE_KINDS.get(check["kind"], check["kind"]))}</td><td>{source}</td>'
                    f'<td><span class="status-pill status-{tone}">{safe(state)}</span></td>'
                    f'<td>{safe(reason or "—")}</td><td>{safe(local_time(check.get("checked_at")))}</td></tr>')
    html('<div class="table-scroll" tabindex="0" role="region" aria-label="Проверки источников">'
         '<table class="source-table"><thead><tr><th scope="col">Конкурент</th><th scope="col">Тип</th>'
         '<th scope="col">Источник</th><th scope="col">Состояние</th><th scope="col">Примечание</th>'
         '<th scope="col">Проверен · ЕКБ</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>')
    if diagnostic:
        details=[{'Источник':c.get('url',''),'Подробности':c['reason']} for c in checks if c.get('reason')]
        if details:
            with st.expander('Диагностика для администратора'):
                st.dataframe(details,hide_index=True,width='stretch')


def navigate(page):
    st.session_state['nav_page'] = page
    st.query_params['section'] = page


def file_bytes(settings, identity, library_id, asset_id):
    fresh = settings()
    if not current_access(identity(), fresh).allowed:
        raise PermissionError("Доступ отозван. Обновите страницу.")
    drive = DriveStore(fresh)
    try:
        return read_asset(Repository(fresh), drive, library_id, asset_id)
    finally:
        drive.close()


RESEARCH_CATEGORIES = {'arbitration': 'Арбитражные дела', 'media': 'Упоминания в СМИ'}


def research_items(library, period=None, category=None):
    """Keep research separate from confirmed events and from undated material."""
    items = library.get('research_item') or {}
    return sorted((item for item in items.values() if isinstance(item, dict)
                   and not item.get('superseded_by')
                   and (category is None or item.get('category') == category)
                   and item.get('period') == period),
                  key=lambda item: (str(item.get('published_at') or ''), str(item.get('id') or '')),
                  reverse=True)


def latest_research_run(library, period, category):
    runs = library.get('research_run') or {}
    matching = [run for run in runs.values() if isinstance(run, dict)
                and run.get('period') == period and run.get('category') == category]
    return max(matching, key=lambda run: (str(run.get('finished_at') or run.get('started_at') or ''),
                                          str(run.get('id') or '')), default=None)


def research_panel(library, period, category):
    items = research_items(library, period, category)
    run = latest_research_run(library, period, category)
    section_heading(RESEARCH_CATEGORIES[category], f'Материалов {len(items)}' if items else '')
    if run is None:
        st.info('Исследование за этот период ещё не проводилось.')
    else:
        status = run.get('status')
        if status == 'error':
            st.error('Исследование завершилось с ошибкой. Сохранённые материалы показаны ниже; полнота не подтверждена.')
        elif status == 'partial':
            st.warning('Исследование выполнено не полностью. Сохранённые материалы показаны ниже.')
        elif status != 'success':
            st.info('Исследование ещё не завершено. Показаны только сохранённые материалы.')
        if status == 'success' and not items:
            st.info('В сохранённом результате материалов за этот период нет. Это не доказывает их отсутствие в других источниках.')
        coverage = run.get('coverage') or []
        unavailable = [entry for entry in coverage if isinstance(entry, dict)
                       and entry.get('status') in ('error', 'partial', 'unavailable', 'blocked')]
        if unavailable:
            st.caption(f'Неполный охват: {len(unavailable)} источников требуют проверки.')
        limitations = run.get('limitations') or []
        if coverage or limitations:
            with st.expander('Охват и ограничения исследования'):
                for entry in coverage:
                    if isinstance(entry, dict):
                        st.write(f"{entry.get('source') or 'Источник'} · {entry.get('status') or 'нет статуса'}"
                                 + (f" · {entry['detail']}" if entry.get('detail') else ''))
                for limit in limitations:
                    st.write(str(limit))
    for item in items[:3]:
        research_card(item, url(item.get('source_url')), url(item.get('case_url')))
    if len(items) > 3:
        with st.expander(f'Все материалы · {len(items)}'):
            for item in items[3:]:
                research_card(item, url(item.get('source_url')), url(item.get('case_url')))


def undated_research(library):
    items = research_items(library)
    if not items:
        return
    with st.expander(f'Материалы без установленной даты · {len(items)}'):
        st.caption('Дата публикации или события не установлена. Эти записи не относятся к выбранному месяцу.')
        for item in items:
            research_card(item, url(item.get('source_url')), url(item.get('case_url')))


def render_library(library, access, settings, identity):
    pages = ["Обзор", "Публикации", "Конкуренты"]
    if access.role in ('admin','editor'):
        pages.extend(['Сбор данных','Черновики'])
    pages.append('Архив')
    if access.role == "admin":
        pages.extend(["Пользователи","Подключения"])
    icons={'Обзор':'▦','Публикации':'≡','Конкуренты':'◈','Сбор данных':'↻',
           'Черновики':'▤','Архив':'▣','Пользователи':'♙','Подключения':'⚙'}
    html('<style>'+''.join(f'[data-testid="stSidebar"] [data-testid="stRadioGroup"]>div:nth-child({i}) label::before '
         +'{content:"'+icons[name]+'";}' for i,name in enumerate(pages,1))+'</style>')
    selected = st.query_params.get("section", "Обзор")
    if st.session_state.get('nav_page') not in pages:
        st.session_state['nav_page']=selected if selected in pages else pages[0]
    page = st.sidebar.radio("РАБОЧЕЕ ПРОСТРАНСТВО", pages, key='nav_page')
    st.query_params["section"] = page
    if page in ('Пользователи','Подключения'):
        if navigation_guard(page,st.session_state.get('nav_period','')):
            return True
        if page=='Пользователи':
            from .member_screen import render_members
            render_members(settings,identity)
            return True
        return False
    current = datetime.now(LOCAL).strftime('%Y-%m')
    periods = available_periods(library, current)
    initial = st.query_params.get("period", current)
    if st.session_state.get('nav_period') not in periods:
        st.session_state['nav_period']=initial if initial in periods else periods[0]
    period = st.sidebar.selectbox("Период", periods, key='nav_period', format_func=period_label)
    st.query_params["period"] = period
    st.sidebar.caption("Обновлено " + local_time(library['manifest']['source_created_at']) + " · Екатеринбург")
    if navigation_guard(page, period):
        return True
    events = period_events(library, period)
    checks = library['period'].get(period,{}).get('checks',[])

    if page == "Обзор":
        official_events = [event for event in events if event['kind'] in OFFICIAL_KINDS]
        st.subheader("Обзор · " + period_label(period))
        st.caption("Публикации конкурентов и состояние источников за выбранный период")
        with st.container(key='metrics'):
            cols = st.columns(4)
            cols[0].metric("Материалы официальных источников", len(official_events))
            for col, kind in zip(cols[1:], OFFICIAL_KINDS):
                col.metric(KINDS[kind], sum(e['kind'] == kind for e in official_events))
        if not official_events:
            st.info("Подтверждённых материалов официальных источников за этот период нет. " +
                    ("Полнота проверки источников показана ниже." if checks else
                     "Проверки официальных источников за этот месяц не сохранены."))
        feed, arbitration, media = st.columns([1.4, 1, 1], gap='medium')
        with feed:
            with st.container(key='overview-feed'):
                section_heading("Материалы периода", f"Всего {len(official_events)}")
                for event in official_events[:3]:
                    event_card(event, KINDS.get(event['kind'], event['kind']), compact=True)
                if not official_events:
                    st.caption("Здесь появятся подтверждённые материалы официальных источников выбранного периода.")
                st.button("Все публикации →", key='overview_publications', on_click=navigate,
                          args=('Публикации',), width='stretch')
        with arbitration:
            with st.container(key='overview-arbitration'):
                research_panel(library, period, 'arbitration')
        with media:
            with st.container(key='overview-media'):
                research_panel(library, period, 'media')
        st.caption('Карточки исследования со статусом «Требует проверки» не включаются в публикации и записку. '
                   'После подтверждения даты, первоисточника и сохранения доказательства материал появится в редакторе.')
        coverage_column, competitors_column = st.columns(2, gap='medium')
        with coverage_column:
            with st.container(key='overview-coverage'):
                section_heading("Проверка источников", f"{len(monitored(checks))} источников")
                if checks:
                    coverage_summary(checks)
                else:
                    st.caption('За этот период проверки официальных источников не сохранены.')
        with competitors_column:
            with st.container(key='overview-competitors'):
                section_heading("Публикации по конкурентам", "Топ-5 за период")
                competitor_bars(official_events, library['competitor'])
        undated_research(library)
        with st.expander("Полнота проверки источников · подробная таблица"):
            coverage_table(checks,diagnostic=access.role=='admin')
    elif page == "Публикации":
        st.subheader("Публикации")
        competitors = {"": "Все конкуренты", **{c: v['name'] for c,v in library['competitor'].items()}}
        kinds = {"": "Все типы", **KINDS}
        for key, parameter, options in [('pub_competitor','competitor',competitors),('pub_kind','kind',kinds)]:
            if st.session_state.get(key) not in options:
                previous=st.query_params.get(parameter,'')
                st.session_state[key]=previous if previous in options else ''
        if 'pub_query' not in st.session_state:
            st.session_state['pub_query']=st.query_params.get('q','')
        st.caption(period_label(period) + " · Подтверждённые материалы с сохранёнными первоисточниками")
        st.caption('Кандидаты из исследования арбитражных дел и СМИ появятся здесь после проверки даты, '
                   'первоисточника и сохранения доказательства.')
        with st.container(key='filters'):
            search, left, right = st.columns([1.4, 1, 1])
            query = search.text_input("Поиск по заголовку и тексту", key='pub_query', placeholder='Название, продукт или тема…')
            code = left.selectbox("Конкурент", list(competitors), key='pub_competitor', format_func=competitors.get)
            kind = right.selectbox("Тип публикации", list(kinds), key='pub_kind', format_func=kinds.get)
        st.query_params.update(competitor=code, kind=kind, q=query)
        found = period_events(library, period, competitor=code, kind=kind, query=query)
        st.caption(f"Найдено материалов: {len(found)}")
        if not found:
            st.info("По выбранным условиям публикаций нет.")
        else:
            pages_count = ceil(len(found) / 15)
            signature=(period,code,kind,query)
            if st.session_state.get('pub_filter_signature') != signature:
                st.session_state['pub_page']=1
                st.session_state['pub_filter_signature']=signature
            st.session_state['pub_page']=min(max(int(st.session_state.get('pub_page',1)),1),pages_count)
            page_controls, _ = st.columns([1, 4])
            number = page_controls.number_input("Страница", min_value=1, max_value=pages_count, step=1, key='pub_page')
            for event in found[(number-1)*15:number*15]:
                with st.container(key=f"pubcard_{event['id']}"):
                    event_card(event, KINDS.get(event['kind'],event['kind']))
                    source = url(event['provenance'].get('article_url') or event['url'])
                    if source:
                        st.link_button("Открыть первоисточник ↗", source)
                    with st.expander("Исходный текст и подтверждение"):
                        st.caption("Основание даты: " + str(event['provenance'].get('date_evidence') or '—'))
                        st.text(event['original_text'])
                        if event['evidence_ids']:
                            chosen = st.selectbox("Сохранённый снимок", range(len(event['evidence_ids'])),
                                                  format_func=lambda i: f"Снимок {i+1}", key=f"proof_{event['id']}")
                            if st.button("Показать сохранённый HTML", key=f"open_proof_{event['id']}"):
                                try:
                                    with st.spinner("Загружаем снимок…"):
                                        content = file_bytes(settings, identity, library['id'], event['evidence_ids'][chosen])
                                    st.caption("Сохранённый код страницы; скрипты не выполняются.")
                                    st.code(content.decode('utf-8', errors='replace'), language='html')
                                except (StorageError, ValueError, PermissionError) as exc:
                                    st.error(str(exc))
    elif page == "Конкуренты":
        st.subheader("Конкуренты")
        st.caption(f"{len(library['competitor'])} компаний в мониторинге · " + period_label(period))
        for index, (code, competitor) in enumerate(sorted(library['competitor'].items(), key=lambda pair: pair[1]['name'])):
            if index % 2 == 0:
                columns = st.columns(2)
            with columns[index % 2], st.container(key=f'competitorcard_{index}'):
                number = sum(e['competitor_code'] == code for e in events)
                html(f'<div class="record-heading"><span class="record-icon" aria-hidden="true">{safe(competitor["name"][:2])}</span>'
                     f'<div><h3>{safe(competitor["name"])}</h3><p>Подтверждённых материалов: {number}</p></div></div>')
                if url(competitor['base_url']):
                    st.link_button("Официальный сайт ↗", url(competitor['base_url']))
                with st.expander("Проверка источников"):
                    coverage_table([c for c in checks if c['competitor_code'] == code],diagnostic=access.role=='admin')
    elif page == "Сбор данных":
        from .jobs import JobService
        from .job_screen import render_jobs, launch
        st.subheader('Сбор данных')
        st.caption('Сайты производителей и Telegram ТЕКО. После сбора добавьте новые публикации в черновик кнопкой «Обновить материалы».')
        chosen=st.text_input('Месяц сбора',value=period[:7],help='Формат ГГГГ-ММ. По умолчанию выбран период из меню.')
        if access.role in ('admin','editor') and st.button('Запустить сбор',type='primary'):
            try:
                JobService(settings,identity).collect(library['id'],chosen)
                launch(settings)
                st.rerun()
            except (StorageError,PermissionError,ValueError) as exc:
                st.error(str(exc))
        render_jobs(library['id'],settings,identity,access.role in ('admin','editor'))
        st.subheader("История сборов")
        for run in sorted(library['run'].values(), key=lambda r: r['id'], reverse=True):
            label = f"№ {run['id']} · {local_time(run['started_at'])} · {STATUS.get(run['status'],run['status'])}"
            with st.expander(label):
                st.write("Период: " + run['period_start'][:10] + " — " + run['period_end'][:10])
                st.caption("Время запуска и проверок показано по Екатеринбургу.")
                coverage_table(run['checks'],diagnostic=access.role=='admin')
    elif page == "Черновики":
        if period in library['period']:
            render_editor(library, access, settings, identity, period)
        else:
            st.info('Сначала запустите сбор за этот период в разделе «Сбор данных».')
    elif page == "Архив":
        st.subheader("Архив отчётов")
        reports = sorted(library['report'].values(), key=lambda r: (r.get('created_at') or '', r['name']), reverse=True)
        st.caption(f"Документов: {len(reports)} · Все периоды · Сохранённые версии записок и подтверждающие материалы")
        if not reports:
            st.info("В архиве пока нет отчётов.")
        for index, report in enumerate(reports):
            if index % 2 == 0:
                report_columns = st.columns(2)
            with report_columns[index % 2], st.container(key=f'reportcard_{index}'):
                subtitle = "Аналитическая записка" if report['legacy'] else f"Редакция {report['revision']} · {local_time(report['created_at'])}"
                html(f'<div class="record-heading"><span class="record-icon" aria-hidden="true">DOCX</span>'
                     f'<div><h3>{safe(report_label(report))}</h3><p>{safe(subtitle)}</p></div></div>')
                from .downloads import FORMATS, asset_download
                for label, field, extension, mime in FORMATS:
                    if not report.get(field):
                        continue
                    key = f"report_{report['id']}_{field}"
                    name = report['name'] if report['name'].endswith(extension) else report['name'] + extension
                    st.download_button("Скачать " + label,
                        data=asset_download(settings, identity, library['id'], report[field]),
                        file_name=name, mime=mime, key=key+'_download', on_click='ignore')
    return True
