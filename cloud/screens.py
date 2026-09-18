"""Invited-user library screens. Every file action rechecks current access."""

from datetime import datetime, timedelta, timezone
from math import ceil
from urllib.parse import urlsplit

import streamlit as st

from .access import authorize
from .drive_store import DriveStore, StorageError
from .library import Repository, period_events, read_asset
from .readiness import section


KINDS = {"news": "Новости сайтов", "telegram": "Telegram", "products": "Продукция и предложения"}
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


def coverage_table(checks):
    rows = []
    for check in checks:
        state = STATUS.get(check['status'], check['status'])
        if check['status'] == 'success' and check.get('items') == 0:
            state = "Проверен, публикаций нет"
        rows.append({"Конкурент": check.get('competitor_name', check['competitor_code']),
                     "Тип": KINDS.get(check['kind'],check['kind']), "Источник": url(check['url']),
                     "Состояние": state, "Примечание": check.get('reason', ''),
                     "Проверен (Екатеринбург)": local_time(check.get('checked_at'))})
    st.dataframe(rows, hide_index=True, width="stretch")


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
    page = st.sidebar.radio("Раздел", pages, key='nav_page')
    st.query_params["section"] = page
    periods = sorted(library['period'], reverse=True)
    current = datetime.now(LOCAL).strftime('%Y-%m')
    initial = st.query_params.get("period", current)
    if st.session_state.get('nav_period') not in periods:
        st.session_state['nav_period']=initial if initial in periods else periods[0]
    period = st.sidebar.selectbox("Период", periods, key='nav_period', format_func=period_label)
    st.query_params["period"] = period
    st.caption("Данные перенесены " + local_time(library['manifest']['source_created_at']) + " (Екатеринбург). Новый сбор из облака ещё не подключён.")
    events = period_events(library, period)
    checks = library['period'][period]['checks']

    if page == "Подключения":
        return False
    if page == "Обзор":
        st.subheader("Обзор · " + period_label(period))
        cols = st.columns(4)
        cols[0].metric("Подтверждённые материалы", len(events))
        for col, (kind, label) in zip(cols[1:], KINDS.items()):
            col.metric(label, sum(e['kind'] == kind for e in events))
        if not events:
            st.info("Подтверждённых материалов за этот период нет. Полнота проверки источников показана ниже.")
        st.subheader("Полнота проверки источников")
        coverage_table(checks)
    elif page == "Публикации":
        st.subheader("Публикации")
        competitors = {"": "Все конкуренты", **{c: v['name'] for c,v in library['competitor'].items()}}
        kinds = {"": "Все типы", **KINDS}
        left, right = st.columns(2)
        for key, parameter, options in [('pub_competitor','competitor',competitors),('pub_kind','kind',kinds)]:
            if st.session_state.get(key) not in options:
                previous=st.query_params.get(parameter,'')
                st.session_state[key]=previous if previous in options else ''
        if 'pub_query' not in st.session_state:
            st.session_state['pub_query']=st.query_params.get('q','')
        code = left.selectbox("Конкурент", list(competitors), key='pub_competitor', format_func=competitors.get)
        kind = right.selectbox("Тип публикации", list(kinds), key='pub_kind', format_func=kinds.get)
        query = st.text_input("Поиск по заголовку и тексту", key='pub_query')
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
            number = st.number_input("Страница", min_value=1, max_value=pages_count, step=1, key='pub_page')
            for event in found[(number-1)*15:number*15]:
                with st.expander(event['date_label'] + " · " + event['competitor_name'] + " · " + event['title']):
                    st.write(event['description'])
                    st.caption(KINDS.get(event['kind'],event['kind']) + " · Подтверждён")
                    source = url(event['provenance'].get('article_url') or event['url'])
                    if source:
                        st.link_button("Открыть первоисточник", source)
                    st.caption("Основание даты: " + str(event['provenance'].get('date_evidence') or '—'))
                    with st.expander("Исходный текст"):
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
        for code, competitor in sorted(library['competitor'].items(), key=lambda pair: pair[1]['name']):
            with st.expander(competitor['name']):
                if url(competitor['base_url']):
                    st.link_button("Официальный сайт", url(competitor['base_url']))
                st.write("Подтверждённых материалов за период: " + str(sum(e['competitor_code'] == code for e in events)))
                coverage_table([c for c in checks if c['competitor_code'] == code])
    elif page == "Сбор данных":
        st.subheader("История сборов")
        st.info("Здесь сохранённая история мониторинга. Запуск нового сбора из облака будет подключён следующим этапом.")
        for run in sorted(library['run'].values(), key=lambda r: r['id'], reverse=True):
            label = f"№ {run['id']} · {local_time(run['started_at'])} · {STATUS.get(run['status'],run['status'])}"
            with st.expander(label):
                st.write("Период: " + run['period_start'][:10] + " — " + run['period_end'][:10])
                st.caption("Время запуска и проверок показано по Екатеринбургу.")
                coverage_table(run['checks'])
    elif page == "Черновики":
        st.subheader("Перенесённые черновики")
        st.info("Ручные тексты и состав материалов сохранены. Совместное редактирование и выпуск новых документов ещё подключаются.")
        drafts = [d for d in library['draft'].values() if d['period'] == period]
        if not drafts:
            st.write("Черновика за выбранный период пока нет.")
        for draft in drafts:
            st.write(f"Редакция {draft['revision']} · {period_label(draft['period'])}")
            st.markdown("**Выводы аналитика**")
            st.text(draft['conclusions'] or 'Выводы не заполнены.')
            st.dataframe([{"Включён": i['included'], "Заголовок": i['title'], "Описание": i['description'],
                           "Дата": i['source']['date_label']} for i in draft['items']], hide_index=True, width="stretch")
    elif page == "Архив":
        st.subheader("Архив отчётов")
        reports = sorted(library['report'].values(), key=lambda r: (r.get('created_at') or '', r['name']), reverse=True)
        if not reports:
            st.info("В архиве пока нет отчётов.")
        for report in reports:
            with st.expander(report['name']):
                st.caption("Прежний документ" if report['legacy'] else f"Редакция {report['revision']} · {local_time(report['created_at'])}")
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
