"""Invited-user library screens. Every file action rechecks current access."""

from datetime import datetime, timedelta, timezone
from math import ceil
import re
from urllib.parse import urlsplit

import streamlit as st

from .access import authorize
from .drive_store import DriveStore, StorageError
from .library import Repository, period_events, read_asset
from .readiness import section
from .presentation import html, safe, note, section_heading, event_card, coverage_summary, competitor_bars
from .editor import render_editor, navigation_guard


KINDS = {"news": "Новости сайтов", "telegram": "Telegram", "products": "Продукция и предложения"}
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


def report_label(report):
    match = re.fullmatch(r'Аналитическая_записка_конкуренты_(\d{4})_(\d{2})\.docx', report['name'])
    if match and 1 <= int(match[2]) <= 12:
        return 'Аналитическая записка · ' + period_label(f'{match[1]}-{match[2]}')
    return report['name'].removesuffix('.docx').replace('__', ' — ').replace('_', ' ')


def coverage_table(checks):
    if not checks:
        st.caption("За этот период проверки источников не сохранены.")
        return
    rows = []
    for check in checks:
        state = STATUS.get(check['status'], check['status'])
        if check['status'] == 'success' and check.get('items') == 0:
            state = "Проверен, публикаций нет"
        reason = str(check.get('reason') or '')
        for technical, readable in [('publication_date_missing', 'Дата публикации не указана'),
                                    ('no_recognized_cards', 'Публикации на странице не распознаны'),
                                    ('page_not_found', 'Страница не найдена'),
                                    ('access_block', 'Источник ограничил доступ'),
                                    ('playwright returned no HTML', 'Не удалось загрузить страницу')]:
            reason = reason.replace(technical, readable)
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


def navigate(page):
    st.session_state['nav_page'] = page
    st.query_params['section'] = page


def file_bytes(settings, identity, library_id, asset_id):
    fresh = settings()
    if not authorize(identity(), section(fresh, "access")).allowed:
        raise PermissionError("Доступ отозван. Обновите страницу.")
    drive = DriveStore(fresh)
    try:
        return read_asset(Repository(fresh), drive, library_id, asset_id)
    finally:
        drive.close()


def render_library(library, access, settings, identity):
    pages = ["Обзор", "Публикации", "Конкуренты", "Сбор данных", "Черновики", "Архив"]
    if access.role == "admin":
        pages.append("Подключения")
    selected = st.query_params.get("section", "Обзор")
    if st.session_state.get('nav_page') not in pages:
        st.session_state['nav_page']=selected if selected in pages else pages[0]
    page = st.sidebar.radio("РАБОЧЕЕ ПРОСТРАНСТВО", pages, key='nav_page')
    st.query_params["section"] = page
    periods = sorted(library['period'], reverse=True)
    current = datetime.now(LOCAL).strftime('%Y-%m')
    initial = st.query_params.get("period", current)
    if st.session_state.get('nav_period') not in periods:
        st.session_state['nav_period']=initial if initial in periods else periods[0]
    period = st.sidebar.selectbox("Период", periods, key='nav_period', format_func=period_label)
    st.query_params["period"] = period
    st.sidebar.caption("Данные на " + local_time(library['manifest']['source_created_at']) + " · Екатеринбург")
    with st.sidebar.expander("Статус приложения"):
        st.caption("Доступны перенесённые материалы и общий редактор записок. Новый сбор и выпуск новых Word-отчётов ещё не подключены.")
    if navigation_guard(page, period):
        return True
    events = period_events(library, period)
    checks = library['period'][period]['checks']

    if page == "Подключения":
        return False
    if page == "Обзор":
        st.subheader("Обзор · " + period_label(period))
        st.caption("Публикации конкурентов и состояние источников за выбранный период")
        with st.container(key='metrics'):
            cols = st.columns(4)
            cols[0].metric("Подтверждённые материалы", len(events))
            for col, (kind, label) in zip(cols[1:], KINDS.items()):
                col.metric(label, sum(e['kind'] == kind for e in events))
        if not events:
            st.info("Подтверждённых материалов за этот период нет. Полнота проверки источников показана ниже.")
        feed, summary = st.columns([1.35, 1], gap='large')
        with feed:
            with st.container(key='overview-feed'):
                section_heading("Материалы периода", f"Всего {len(events)}")
                for event in events[:3]:
                    event_card(event, KINDS.get(event['kind'], event['kind']), compact=True)
                if not events:
                    st.caption("Здесь появятся подтверждённые публикации выбранного периода.")
                st.button("Все публикации →", key='overview_publications', on_click=navigate,
                          args=('Публикации',), width='stretch')
        with summary:
            with st.container(key='overview-coverage'):
                section_heading("Проверка источников", f"{len(checks)} источников")
                coverage_summary(checks)
            with st.container(key='overview-competitors'):
                section_heading("Публикации по конкурентам", "Топ-5 за период")
                competitor_bars(events, library['competitor'])
        with st.expander("Полнота проверки источников · подробная таблица"):
            coverage_table(checks)
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
                    coverage_table([c for c in checks if c['competitor_code'] == code])
    elif page == "Сбор данных":
        st.subheader("История сборов")
        note("История мониторинга", "Здесь сохранены выполненные проверки. Запуск нового сбора из облака появится на следующем этапе.")
        for run in sorted(library['run'].values(), key=lambda r: r['id'], reverse=True):
            label = f"№ {run['id']} · {local_time(run['started_at'])} · {STATUS.get(run['status'],run['status'])}"
            with st.expander(label):
                st.write("Период: " + run['period_start'][:10] + " — " + run['period_end'][:10])
                st.caption("Время запуска и проверок показано по Екатеринбургу.")
                coverage_table(run['checks'])
    elif page == "Черновики":
        render_editor(library, access, settings, identity, period)
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
                subtitle = "Документ из прежнего архива" if report['legacy'] else f"Редакция {report['revision']} · {local_time(report['created_at'])}"
                html(f'<div class="record-heading"><span class="record-icon" aria-hidden="true">DOCX</span>'
                     f'<div><h3>{safe(report_label(report))}</h3><p>{safe(subtitle)}</p></div></div>')
                for label, field, extension, mime in [("Word", "docx_id", ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                                                     ("ZIP с доказательствами", "bundle_id", ".zip", "application/zip")]:
                    if not report.get(field):
                        continue
                    key = f"report_{report['id']}_{field}"
                    if st.button("Подготовить " + label, key=key):
                        try:
                            with st.spinner("Загружаем документ…"):
                                content = file_bytes(settings, identity, library['id'], report[field])
                            name = report['name'] if report['name'].endswith(extension) else report['name'] + extension
                            st.download_button("Скачать " + label, data=content, file_name=name, mime=mime, key=key+'_download', on_click='ignore')
                        except (StorageError, ValueError, PermissionError) as exc:
                            st.error(str(exc))
    return True
