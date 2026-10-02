"""A shared search basket for single models, pasted lists and characteristics."""
import hashlib
import math

import streamlit as st

from .catalog_search import CatalogSearch, FILTERS, characteristic_label, entry_label, merge_selection


SELECTION = 'catalog_search_selection'


def _reset_page():
    st.session_state['catalog_search_result_page'] = 1


def _add(entries, records):
    st.session_state[SELECTION] = merge_selection(st.session_state.get(SELECTION, []), entries, records)
    _reset_page()


def _parse(search):
    text = st.session_state.get('catalog_search_paste', '')
    st.session_state['catalog_search_parsed_text'] = text
    st.session_state['catalog_search_batch_page'] = 1
    resolutions = search.resolve_paste(text)
    _add([row.candidates[0] for row in resolutions if row.status == 'exact'], search.records)


def _add_chosen(key, records):
    entry = st.session_state.get(key)
    if entry:
        _add([entry], records)


def _paste(search):
    st.text_area('Номенклатуры и артикулы', key='catalog_search_paste', height=110,
                 placeholder='Вставьте столбец из Excel или несколько моделей',
                 help='Разделители: новая строка, табуляция, точка с запятой, запятая. '
                      'Известные базе обозначения распознаются и через пробел; пробелы внутри модели сохраняются.')
    st.button('Разобрать список', key='catalog_search_parse', on_click=_parse, args=(search,),
              disabled=not st.session_state.get('catalog_search_paste', '').strip(), type='primary')
    original = st.session_state.get('catalog_search_parsed_text', '')
    if not original:
        return
    if original != st.session_state.get('catalog_search_paste', ''):
        st.caption('Текст изменён. Нажмите «Разобрать список», чтобы обработать новую версию.')
        return
    rows = search.resolve_paste(original)
    counts = {status: sum(row.status == status for row in rows) for status in ('exact', 'ambiguous', 'partial', 'missing')}
    st.caption(f"Позиций без повторов: {len(rows)} · точных: {counts['exact']} · "
               f"нужно выбрать: {counts['ambiguous'] + counts['partial']} · не найдено: {counts['missing']}")
    pages = max(1, math.ceil(len(rows) / 15))
    if st.session_state.get('catalog_search_batch_page', 1) > pages:
        st.session_state['catalog_search_batch_page'] = 1
    page = st.number_input('Страница распознанного списка', min_value=1, max_value=pages,
                           key='catalog_search_batch_page') if pages > 1 else 1
    for row in rows[(page - 1) * 15:page * 15]:
        if row.status == 'exact':
            chosen = row.candidates[0]
            added = chosen in st.session_state.get(SELECTION, [])
            st.caption(('Добавлено: ' if added else 'Распознано: ') + row.query + ' → ' + entry_label(search.records[chosen]))
            continue
        if row.status == 'missing':
            st.warning('Не найдено в текущей базе: ' + row.query)
            continue
        digest = hashlib.sha256(row.query.encode('utf-8')).hexdigest()[:16]
        candidates = list(row.candidates)
        if len(candidates) > 100:
            refine = st.text_input('Уточнить «' + row.query + '»', key='catalog_refine_' + digest)
            if refine:
                allowed = set(search.search(refine))
                candidates = [entry for entry in candidates if entry in allowed]
            st.caption(f'Совпадений: {len(candidates)}. Ниже первые 100; уточните обозначение для остальных.')
        choice_key = 'catalog_resolve_' + digest
        candidates = candidates[:100]
        if st.session_state.get(choice_key) not in candidates:
            st.session_state[choice_key] = None
        st.selectbox('Выберите модель для «' + row.query + '»', candidates, index=None, key=choice_key,
                     format_func=lambda entry: entry_label(search.records[entry]), placeholder='Выберите совпадение')
        st.button('Добавить выбранную модель', key='catalog_add_' + digest,
                  disabled=not st.session_state.get(choice_key), on_click=_add_chosen, args=(choice_key, search.records))


def _characteristics(search):
    st.caption('Выберите нужные параметры. Позиции без значения выбранной характеристики в результат не входят.')
    criteria = {}
    fields = list(FILTERS)
    # Cascading options are read from collected values, never a hardcoded catalog.
    for start in range(0, len(fields), 3):
        columns = st.columns(3)
        for col, field in zip(columns, fields[start:start + 3]):
            options = search.options(field, criteria)
            key = 'catalog_filter_' + field
            if st.session_state.get(key) not in options:
                st.session_state[key] = None
            criteria[field] = col.selectbox(FILTERS[field], options, index=None, key=key,
                format_func=lambda value, field=field: characteristic_label(field, value),
                placeholder='Любой / любая', disabled=not options,
                help='Доступные значения из текущей базы с учётом параметров выше.')
    if not any(value is not None for value in criteria.values()):
        st.caption('Задайте хотя бы одну характеристику для подбора.')
        return
    matches = search.filter(criteria)
    st.caption(f'Найдено моделей: {len(matches)}. Соответствие этим параметрам не подтверждает полную взаимозаменяемость.')
    if not matches:
        st.info('Среди собранных карточек нет моделей с выбранным сочетанием характеристик.')
        return
    key = 'catalog_characteristic_choices'
    st.session_state[key] = [entry for entry in st.session_state.get(key, []) if entry in matches]
    chosen = st.multiselect('Модели по характеристикам', matches, key=key,
                            format_func=lambda entry: entry_label(search.records[entry]),
                            placeholder='Выберите модели или добавьте все найденные')
    left, right = st.columns(2)
    left.button('Добавить выбранные', key='catalog_add_characteristics', disabled=not chosen,
                 on_click=_add, args=(chosen, search.records))
    right.button(f'Добавить все найденные ({len(matches)})', key='catalog_add_all_characteristics',
                  on_click=_add, args=(matches, search.records))


def render_search(index,characteristics_ready=True):
    if not hasattr(index, '_catalog_search'):
        index._catalog_search = CatalogSearch(index.records)
    search = index._catalog_search
    if SELECTION not in st.session_state:
        st.session_state[SELECTION] = [entry for entry, row in index.records.items() if row.get('selected')]
    else:
        st.session_state[SELECTION] = merge_selection(st.session_state[SELECTION], [], search.records)
    with st.container(border=True, key='catalog_search_panel'):
        st.html('''<style>
        .st-key-catalog_search_selection [role="group"][aria-label="Selected values"] {
            max-height: 140px; overflow-y: auto;
        }
        </style>''')
        selected = st.multiselect('Поиск по номенклатуре или артикулу', list(search.records), key=SELECTION,
            format_func=lambda entry: entry_label(search.records[entry]),
            on_change=_reset_page,
            placeholder='Начните вводить модель, артикул или производителя…',
            help='Подсказки из всей доступной базы появляются при вводе. Можно выбрать несколько позиций.')
        st.caption(f'В базе поиска: {len(search.records):,} позиций · выбрано: {len(selected)}'.replace(',', ' '))
        paste, specs = st.tabs(['Вставить список', 'Подобрать по характеристикам'])
        with paste:
            _paste(search)
        with specs:
            if characteristics_ready:_characteristics(search)
            else:st.info('Характеристики подготавливаются. Поиск по обозначению и вставка списка уже доступны.')
    return [search.records[entry] for entry in selected]
