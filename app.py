from pathlib import Path
import atexit
import logging
import time

import pandas as pd
import streamlit as st

from price_monitor.exchange import COLUMNS, read_table, validate_rows, csv_bytes, xlsx_bytes, rules_rows
from price_monitor.models import STATUS_LABELS, AVAILABILITY_LABELS, RUN_LABELS
from price_monitor.sources import SOURCES
from price_monitor.storage import Store

ROOT = Path(__file__).resolve().parent
CLOUD_MODE = globals().get("CLOUD_MODE", False)
st.set_page_config(page_title="Мониторинг цен",page_icon="📊",layout="wide")
st.markdown("""<style>
.block-container {padding-top:2rem; max-width:1500px;}
div[data-testid="stMetric"] {border:1px solid #dde4ed; border-radius:12px; padding:16px;}
div[data-testid="stSidebar"] {border-right:1px solid #dde4ed;}
</style>""",unsafe_allow_html=True)


@st.cache_resource
def services(cloud_mode):
    if not cloud_mode:
        return Store(), None
    from price_monitor.cloud import EmbeddedWorker
    try:
        settings = dict(st.secrets["database"])
    except (FileNotFoundError, KeyError):
        raise ValueError("database secrets missing") from None
    store = Store(postgres=settings)
    worker = EmbeddedWorker(store)
    atexit.register(worker.close)
    return store, worker


try:
    db, cloud_worker = services(CLOUD_MODE)
except Exception as e:
    logging.getLogger("price_monitor").error("Storage initialization failed (%s)", type(e).__name__)
    st.error("Приложение пока не подключено к хранилищу. Владельцу нужно проверить настройки базы в Secrets Streamlit.")
    st.stop()
st.sidebar.title("Мониторинг цен")
st.sidebar.caption("Промышленные датчики · 5 источников")
page = st.sidebar.radio("Раздел",["Сбор цен","Результаты","История модели","Источники"],label_visibility="collapsed")
st.sidebar.divider()
st.sidebar.caption("Время в журнале — UTC. Цены сохраняются в валюте источника.")
if CLOUD_MODE:
    st.sidebar.caption("Общий доступ: задания, результаты и история видны всем посетителям. Запустить и остановить сбор может любой посетитель.")


def render_results(rows, key):
    if not rows:
        st.info("Данных пока нет.")
        return
    df = pd.DataFrame(rows)
    c1,c2 = st.columns(2)
    source_filter = c1.multiselect("Источники",list(dict.fromkeys(df.source)),format_func=lambda s:SOURCES[s].label,key=key+"sources")
    statuses = c2.multiselect("Статусы",list(dict.fromkeys(df.status)),format_func=lambda s:STATUS_LABELS.get(s,s),key=key+"statuses")
    if source_filter:
        df = df[df.source.isin(source_filter)]
    if statuses:
        df = df[df.status.isin(statuses)]
    view = df.copy()
    view["source"] = view.source.map(lambda s:SOURCES[s].label)
    view["status"] = view.status.map(lambda s:STATUS_LABELS.get(s,s))
    view["availability"] = view.availability.map(lambda s:AVAILABILITY_LABELS.get(s,s))
    visible = ["source","manufacturer","article","price","currency","status","availability","http_status","checked_at","url","detail"]
    labels = {"source":"Источник","manufacturer":"Производитель","article":"Артикул","price":"Цена","currency":"Валюта","status":"Результат","availability":"Наличие","http_status":"HTTP","checked_at":"Проверено (UTC)","url":"Карточка","detail":"Примечание"}
    st.dataframe(view[visible].rename(columns=labels),hide_index=True,width="stretch",column_config={"Карточка":st.column_config.LinkColumn("Карточка",display_text="Открыть"),"HTTP":st.column_config.NumberColumn("HTTP",format="%d")})
    a,b = st.columns(2)
    export_rows = df.where(pd.notnull(df),None).to_dict("records")
    a.download_button("Скачать XLSX",xlsx_bytes(export_rows,list(df.columns)),file_name=f"prices_{key}.xlsx",mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",key=key+"xlsx")
    b.download_button("Скачать CSV",csv_bytes(export_rows,list(df.columns)),file_name=f"prices_{key}.csv",mime="text/csv",key=key+"csv")
    st.caption("Выгружаются строки с выбранными фильтрами, включая исходный текст цены, статус HTTP и отпечаток ответа.")


@st.fragment(run_every=3)
def active_progress():
    runs = db.runs()
    active = next((r for r in runs if r["state"] in ("queued","running")),None)
    heartbeat = db.lease()
    online = bool(heartbeat and time.time()-heartbeat<35)
    if online:
        st.caption("● Сборщик подключён")
    else:
        st.warning("Сборщик запускается или восстанавливается. Задания сохранены в очереди." if CLOUD_MODE else "Сборщик не подключён. Задания сохранятся в очереди; администратору нужно запустить процесс сборщика.")
    if active:
        st.subheader(f"Запуск №{active['id']} · {RUN_LABELS[active['state']]}")
        done,total = active["finished"],active["total"]
        st.progress(done/max(1,total),text=f"Обработано {done} из {total}")
        if active["cancel_requested"]:
            st.info("Остановка запрошена. Уже полученные результаты сохранены.")
        elif st.button("Остановить запуск",key="stop_active"):
            db.cancel(active["id"])
            st.rerun()
        data = db.results(run_id=active["id"])
        last = [r for r in data if r["status"] not in {"pending","processing"}][-8:]
        if last:
            st.dataframe(pd.DataFrame([{"Артикул":r["article"],"Статус":STATUS_LABELS.get(r["status"],r["status"]),"Цена":r["price"],"Примечание":r["detail"]} for r in last]),hide_index=True,width="stretch")
    elif runs:
        latest = runs[0]
        st.success(f"Последний запуск №{latest['id']}: {RUN_LABELS[latest['state']]} · {latest['finished']} из {latest['total']}")


if page == "Сбор цен":
    st.title("Сбор цен конкурентов")
    st.write("Загрузите список моделей и ссылок, проверьте задания и запустите сбор. Результаты и история сохранятся автоматически.")
    active_progress()
    st.divider()
    if CLOUD_MODE:
        with st.expander("Sensoren: сбор с компьютера при HTTP 403 в облаке"):
            st.write("При отказе Sensoren облачному сборщику владелец может запустить run_sensoren.cmd в папке приложения на своём компьютере. Сбор выполняется обычными HTTP-запросами с проверкой robots.txt.")
            st.write("Программа берёт сохранённые задания Sensoren из Supabase и добавляет отдельный завершённый запуск. После окончания откройте «Результаты» → «Обновить результаты» и выберите новый запуск.")
            st.caption("Для других моделей передайте локальному запуску CSV/XLSX с заданиями. Подробная инструкция — в разделе «Источники». При отказе сайта локальному сборщику нужен согласованный доступ или фид поставщика.")
    st.subheader("1. Подготовьте задания")
    with st.expander("Формат таблицы и примеры",expanded=False):
        st.write("Обязательные столбцы: source, manufacturer, article. Для каждой строки заполните product_url или url_template. Шаблон — ссылка на карточку с параметром {article}; он не выполняет поиск или Python-код.")
        st.write("Артикулы храните текстом, чтобы Excel не убирал ведущие нули. Для BESKONTA указывайте полную маркировку выбранного исполнения.")
        template = [{c:"" for c in COLUMNS}]
        left,right = st.columns(2)
        left.download_button("Пустой шаблон XLSX",xlsx_bytes(template,COLUMNS,"Задания"),file_name="tasks_template.xlsx")
        right.download_button("Примеры на пяти сайтах",(ROOT/"examples"/"tasks.csv").read_bytes(),file_name="tasks_examples.csv")
    uploaded = st.file_uploader("Таблица заданий",type=["csv","xlsx"],max_upload_size=10)
    if uploaded:
        try:
            rows = read_table(uploaded.getvalue(),uploaded.name)
            rules,errors = validate_rows(rows)
            st.subheader("2. Проверьте список")
            if errors:
                st.error("Исправьте ошибки в таблице. Запуск всего файла заблокирован до их исправления.")
                st.dataframe(errors,hide_index=True,width="stretch")
            if rules:
                st.dataframe(rules_rows(rules),hide_index=True,width="stretch")
                st.caption(f"Корректных заданий: {len(rules)}. Одновременно обрабатываются разные сайты, внутри сайта запросы идут последовательно.")
                if st.button("Запустить сбор",type="primary",disabled=bool(errors)):
                    try:
                        run_id = db.enqueue(rules)
                        st.session_state["selected_run"] = run_id
                        st.success(f"Запуск №{run_id} добавлен в очередь")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))
        except Exception as e:
            st.error(f"Не удалось загрузить задания: {e}")
    with st.expander("Ранее использованные правила"):
        saved = db.saved_rules()
        if saved:
            st.download_button("Скачать правила XLSX",xlsx_bytes(saved,COLUMNS,"Задания"),file_name="saved_rules.xlsx")
        else:
            st.caption("Правила появятся после первого запуска.")

elif page == "Результаты":
    st.title("Результаты запусков")
    runs = db.runs()
    if runs:
        if st.button("Обновить результаты"):
            st.rerun()
        selected = st.session_state.get("selected_run",runs[0]["id"])
        ids = [r["id"] for r in runs]
        lookup = {r["id"]:r for r in runs}
        run_id = st.selectbox("Запуск",ids,index=ids.index(selected) if selected in ids else 0,format_func=lambda n:f"№{n} · {lookup[n]['created_at']} · {RUN_LABELS[lookup[n]['state']]}")
        if lookup[run_id]["note"]:
            st.caption(lookup[run_id]["note"])
        data = db.results(run_id=run_id)
        a,b,c = st.columns(3)
        a.metric("Заданий",len(data))
        b.metric("С публичной ценой",sum(r["status"]=="priced" for r in data))
        c.metric("Обработано",lookup[run_id]["finished"])
        render_results(data,f"run_{run_id}")
    else:
        st.info("Запустите сбор на вкладке «Сбор цен».")

elif page == "История модели":
    st.title("История модели")
    options = db.rule_options()
    if options:
        index = st.selectbox("Модель и правило",range(len(options)),format_func=lambda i:f"{SOURCES[options[i]['source']].label} · {options[i]['manufacturer']} · {options[i]['article']} · правило {options[i]['id']}")
        rule = options[index]
        st.caption(rule["product_url"] or rule["url_template"])
        history = db.results(rule_id=rule["id"])
        priced = [r for r in history if r["status"]=="priced"]
        if priced:
            currencies = list(dict.fromkeys(r["currency"] for r in priced))
            currency = st.selectbox("Валюта графика",currencies)
            chart = pd.DataFrame([{"Проверено":pd.to_datetime(r["checked_at"]),"Цена":float(r["price"])} for r in priced if r["currency"]==currency])
            st.line_chart(chart,x="Проверено",y="Цена")
            st.caption("График включает только полученные цены в выбранной валюте. Пропуски и ошибки сохранены в таблице.")
        render_results(history,f"history_{rule['id']}")
    else:
        st.info("История появится после первого запуска.")

else:
    st.title("Источники и аудит")
    st.dataframe([{"Источник":s.label,"Сайт":f"https://{s.host}","Производители":", ".join(s.brands),"Метод":"Публичная HTML-карточка"} for s in SOURCES.values()],hide_index=True,width="stretch")
    st.info("Источник останавливается при авторизации, CAPTCHA, HTTP 403/429 или невозможности проверить robots.txt. Данные, полученные до остановки, сохраняются.")
    sensoren_report = ROOT/"docs"/"SENSOREN.md"
    if sensoren_report.exists():
        with st.expander("Sensoren: доступ из облака и локальный сбор", expanded=True):
            st.markdown(sensoren_report.read_text(encoding="utf-8"))
    report = ROOT/"docs"/"AUDIT.md"
    if report.exists():
        st.markdown(report.read_text(encoding="utf-8"))
    else:
        st.caption("Отчёт аудита ещё не добавлен.")
