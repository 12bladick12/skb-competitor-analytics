"""A separate, read-only page for actual changes in collected prices."""
from datetime import date, timedelta
import logging
import math

import pandas as pd
import streamlit as st

from .exchange import csv_bytes, xlsx_bytes
from .price_changes import change_table, history_overview, load_price_changes


def render_price_changes(library):
    st.title("Изменения цен", anchor=False)
    st.caption("Изменения между последовательными успешными замерами одной модели на одном сайте. "
               "Дата изменения — дата обнаружения при сборе, время — UTC.")
    try:
        overview = history_overview(library)
    except Exception:
        logging.getLogger("price_monitor").exception("Price-change history overview failed")
        st.error("Не удалось прочитать историю цен. Повторите загрузку страницы.")
        return
    if not overview:
        st.info("История цен пока пуста. Выполните сбор цен; для обнаружения изменения нужны два успешных замера модели.")
        return

    period_column, brand_column = st.columns([2, 1])
    period = period_column.date_input("Период обнаружения изменений", value=(date.today() - timedelta(days=29), date.today()),
                                      format="DD.MM.YYYY", key="price_changes_period")
    brand = brand_column.selectbox("Производитель", [""] + [row["manufacturer"] for row in overview],
                                   format_func=lambda value: value or "Все производители", key="price_changes_brand")
    search_column, direction_column = st.columns([2, 1])
    query = search_column.text_input("Маркировка или часть маркировки", key="price_changes_query",
                                     placeholder="Например, ISN или SI5000")
    direction = direction_column.selectbox("Направление изменения", ["all", "increase", "decrease"],
                                            format_func={"all": "Все изменения", "increase": "Рост", "decrease": "Снижение"}.get,
                                            key="price_changes_direction")
    if not isinstance(period, (tuple, list)) or len(period) != 2:
        st.info("Выберите начало и конец периода.")
        return
    start, last_day = period
    if start > last_day:
        st.error("Начало периода не может быть позже его конца.")
        return
    with st.spinner("Сопоставляем сохранённые замеры…"):
        try:
            report = load_price_changes(library, start, last_day + timedelta(days=1), manufacturer=brand, query=query)
        except Exception:
            logging.getLogger("price_monitor").exception("Price-change history read failed")
            st.error("Не удалось загрузить изменения цен. Это ошибка чтения истории; повторите загрузку страницы.")
            return

    events = [row for row in report.events if direction == "all" or row["direction"] == direction]
    count_column, rise_column, fall_column = st.columns(3)
    count_column.metric("Изменений по фильтрам", len(events))
    rise_column.metric("Повышений", sum(row["direction"] == "increase" for row in events))
    fall_column.metric("Снижений", sum(row["direction"] == "decrease" for row in events))
    counts = [f"{number:,}".replace(",", " ") for number in
              (report.observations_in_period, report.models_in_period, report.models_with_history, report.compared_pairs)]
    st.caption(f"В периоде: {counts[0]} замеров с ценой, {counts[1]} моделей. "
               f"Моделей с повторными замерами: {counts[2]}. Сопоставлено пар: {counts[3]}.")
    if report.last_checked_at:
        st.caption("Последний замер выборки: " + str(report.last_checked_at).replace("T", " ").replace("+00:00", " UTC"))
    if report.incompatible_pairs:
        st.warning(f"Не сопоставлено пар: {report.incompatible_pairs}. Различаются или не подтверждены валюта, "
                   "единица цены либо условия НДС. Эти пары не считаются изменениями цены.")
    st.caption("Величина изменения всегда положительная; снижение указано отдельным направлением. "
               "Цены SENSOREN включают НДС. Ставка показывается только при наличии данных. "
               "Ошибка сбора и отсутствие цены не считаются снижением до нуля.")

    if not events:
        if not report.observations_in_period:
            st.info("В выбранном периоде нет сохранённых замеров с ценой по этим фильтрам.")
        elif not report.models_with_history:
            st.info("Недостаточно истории: для каждой выбранной модели сохранён только один замер с ценой. "
                    "Изменения появятся после повторного сбора.")
        elif report.events:
            st.info("Изменений выбранного направления за этот период нет.")
        else:
            st.info("Подтверждённых изменений цены за этот период не обнаружено. "
                    "Это относится только к сохранённым сопоставимым замерам.")
        return

    size = 50
    pages = math.ceil(len(events) / size)
    filter_key = (start, last_day, brand, query, direction)
    if st.session_state.get("_price_changes_filter") != filter_key or "price_changes_page" not in st.session_state:
        st.session_state["price_changes_page"] = 1
        st.session_state["_price_changes_filter"] = filter_key
    if st.session_state.get("price_changes_page", 1) > pages:
        st.session_state["price_changes_page"] = pages
    pager, _ = st.columns([1, 4])
    page = pager.number_input("Страница", min_value=1, max_value=pages, value=None, step=1, key="price_changes_page") or 1
    offset = (page - 1) * size
    st.caption(f"Показано {offset + 1}–{min(offset + size, len(events))} из {len(events)} изменений.")
    table = change_table(events[offset:offset + size])
    visible = [key for key in table[0] if key not in ("ID наблюдения", "ID предыдущего наблюдения", "Предыдущая ссылка")]
    frame = pd.DataFrame(table)[visible]
    for key in ("Дата изменения (UTC)", "Предыдущий замер (UTC)"):
        frame[key] = pd.to_datetime(frame[key], utc=True)
    st.dataframe(frame, hide_index=True, width="stretch", column_config={
        "Дата изменения (UTC)": st.column_config.DatetimeColumn(format="DD.MM.YYYY HH:mm", width="medium"),
        "Предыдущий замер (UTC)": st.column_config.DatetimeColumn(format="DD.MM.YYYY HH:mm"),
        "Маркировка": st.column_config.TextColumn(width="medium"),
        **{key: st.column_config.NumberColumn(format="%.2f") for key in
           ("Предыдущая цена", "Новая цена", "Величина изменения", "Изменение, %")},
        "Ссылка": st.column_config.LinkColumn(display_text="Карточка"),
    })
    export_rows = change_table(events)
    name = f"price_changes_{start.isoformat()}_{last_day.isoformat()}"
    left, right = st.columns(2)
    left.download_button("Скачать все изменения XLSX", xlsx_bytes(export_rows, sheet_name="Изменения цен"),
                         file_name=name + ".xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                         key="price_changes_xlsx")
    right.download_button("Скачать все изменения CSV", csv_bytes(export_rows), file_name=name + ".csv",
                          mime="text/csv", key="price_changes_csv")
    st.caption("Выгрузка включает все изменения по выбранным фильтрам, даты обоих замеров и ссылки на источники.")
