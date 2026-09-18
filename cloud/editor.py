"""Draft editor with explicit saves, source review and protected navigation."""
from copy import deepcopy
import json
from pathlib import Path
from uuid import uuid4

import streamlit as st

from .drafts import DraftService
from .draft_rules import DraftConflict
from .drive_store import StorageError
from .presentation import html, safe, note, event_card


def editable(draft):
    return {'conclusions': draft['conclusions'], 'items': [
        {k: i[k] for k in ('event_id', 'included', 'title', 'description')} for i in draft['items']]}


def dirty():
    state = st.session_state.get('draft_editor')
    return bool(state and (state['working'] != editable(state['base']) or state['accepted']))


def reset_editor(view, import_id, period):
    st.session_state['draft_editor'] = {
        'import_id': import_id, 'period': period, 'base': deepcopy(view['draft']),
        'working': editable(view['draft']), 'accepted': {}, 'view': view, 'epoch': uuid4().hex,
    }
    st.session_state['draft_guard_reset'] = uuid4().hex


def discard_editor():
    st.session_state.pop('draft_editor', None)
    st.session_state['draft_guard_reset'] = uuid4().hex


def render_guard():
    # Only our fixed application script runs. Source HTML remains inert st.code.
    script = Path(__file__).with_name('draft_guard.js').read_text(encoding='utf-8')
    token = st.session_state.setdefault('draft_guard_reset', uuid4().hex)
    script = script.replace('__SERVER_DIRTY__', 'true' if dirty() else 'false').replace('__RESET_TOKEN__', token)
    st.html('<script>' + script + '</script>', unsafe_allow_javascript=True)


def navigation_guard(page, period):
    state = st.session_state.get('draft_editor')
    if not dirty():
        if state and (page != 'Черновики' or period != state['period']):
            discard_editor()
        return False
    if page == 'Черновики' and period == state['period']:
        return False
    st.warning('В записке есть несохранённые изменения. Сохраните их перед переходом или явно отмените.')
    back, leave = st.columns(2)
    def return_to_draft():
        st.session_state['nav_page'] = 'Черновики'
        st.session_state['nav_period'] = state['period']
    back.button('Вернуться к черновику', type='primary', on_click=return_to_draft)
    leave.button('Продолжить без сохранения', on_click=discard_editor)
    return True


def _change(field, key, event_id=None):
    state = st.session_state['draft_editor']
    if event_id is None:
        state['working'][field] = st.session_state[key]
    elif field == 'accept_version':
        if st.session_state[key]:
            state['accepted'][event_id] = state['view']['library']['event'][str(event_id)]['version']
        else:
            state['accepted'].pop(event_id, None)
    else:
        item = next(i for i in state['working']['items'] if i['event_id'] == event_id)
        item[field] = st.session_state[key]
    state.pop('preview', None)


def _preview(value):
    from .screens import coverage_table, KINDS, period_label
    st.caption(f"Сохранённая редакция {value['revision']} · {period_label(value['period'])}")
    if value['incomplete']:
        st.warning('Сбор неполный. Отсутствие материалов не означает отсутствие новостей по непроверенным источникам.')
    st.write('Всего включено: ' + str(len(value['items'])) + '. ' + '; '.join(
        f'{label}: {value["counts"].get(kind, 0)}' for kind, label in KINDS.items()))
    st.caption('Заголовки и описания отредактированы аналитиком. Даты, ссылки и проверка сохранены из источников.')
    for kind, label in KINDS.items():
        st.markdown('**' + label + '**')
        selected = [i for i in value['items'] if i['source']['kind'] == kind]
        if not selected:
            st.caption('Материалы не отобраны.')
        for item in selected:
            event_card({**item['source'], 'title': item['title'], 'description': item['description']}, label, compact=True)
    st.markdown('**Выводы аналитика**')
    st.text(value['conclusions'] or 'Выводы не добавлены.')
    with st.expander('Полнота проверки источников · независимо от отбора'):
        coverage_table(value['checks'])


def _diff(issue):
    previous, current = issue['previous'], issue['current'] or {}
    fields = [('title','Заголовок источника'), ('original_text','Исходный текст'), ('date_label','Дата'),
              ('status','Статус'), ('reason','Причина статуса'), ('url','Ссылка'), ('provenance','Подтверждение даты и первоисточник'),
              ('date_basis','Основание даты'), ('detected_at','Обнаружено'), ('evidence_ids','Сохранённые доказательства')]
    changed = [(key, label) for key, label in fields if previous.get(key) != current.get(key)]
    if not changed:
        st.caption('Изменилась версия исходной записи. Проверьте первоисточник перед подтверждением.')
    for key, label in changed:
        st.markdown('**' + label + '**')
        before, after = st.columns(2)
        before.caption('В черновике')
        after.caption('Сейчас')
        def text_value(value):
            return json.dumps(value, ensure_ascii=False, indent=2) if isinstance(value, (dict, list)) else str(value or '—')
        before.text(text_value(previous.get(key)))
        after.text(text_value(current.get(key)))


def render_editor(library, access, settings, identity, period):
    from .screens import local_time, period_label, url
    service = DraftService(settings, identity)
    import_id = library['id']
    st.subheader('Редактор записки')
    st.caption(period_label(period) + ' · Общий черновик для команды')
    state = st.session_state.get('draft_editor')
    can_write = access.role in ('admin', 'editor')
    if not state or state['import_id'] != import_id or state['period'] != period:
        try:
            with st.spinner('Открываем сохранённый черновик…'):
                view = service.open(import_id, period)
        except (StorageError, ValueError, PermissionError) as exc:
            st.error(str(exc))
            return
        if not view['draft']:
            note('Черновик ещё не создан', 'При создании в него попадут подтверждённые материалы выбранного периода.')
            if can_write and st.button('Создать черновик', type='primary'):
                try:
                    service.create(import_id, period)
                    st.rerun()
                except (StorageError, ValueError, PermissionError) as exc:
                    st.error(str(exc))
            elif not can_write:
                st.caption('Создать записку может редактор или администратор.')
            return
        reset_editor(view, import_id, period)
        state = st.session_state['draft_editor']

    draft = state['base']
    if message := st.session_state.pop('draft_notice', None):
        st.success(message)
    if not can_write:
        note('Режим чтения', 'Редактирование доступно сотрудникам с ролью редактора или администратора.')
    html(f'<div class="editor-meta"><span>Редакция <b>{draft["revision"]}</b> · '
         f'{safe(local_time(draft["updated_at"]))}</span><span>Сохранил: {safe(draft["updated_by"])}</span></div>')
    html('<div class="draft-save-status' + ('' if dirty() else ' is-clean') + '" role="status">' + ('Есть несохранённые изменения' if dirty() else 'Все изменения сохранены') + '</div>')
    actions = st.columns([1, 1.15, 1.2])
    if can_write and actions[0].button('Сохранить правки', type='primary', width='stretch'):
        changes = deepcopy(state['working']['items'])
        for change in changes:
            if version := state['accepted'].get(change['event_id']):
                change['accept_version'] = version
        try:
            with st.spinner('Сохраняем редакцию…'):
                saved = service.save(import_id, period, draft['revision'], state['working']['conclusions'], changes)
                # A lost read-back must not erase the unsaved copy or claim failure of a confirmed save.
            state['base'] = saved
            state['working'] = editable(saved)
            state['accepted'] = {}
            state['epoch'] = uuid4().hex
            state.pop('history', None)
            state.pop('preview', None)
            state.pop('remote', None)
            st.session_state['draft_guard_reset'] = uuid4().hex
            st.session_state['draft_notice'] = f"Редакция {saved['revision']} сохранена в общей базе."
            state['view'] = service.saved_view
            st.rerun()
        except (StorageError, ValueError, PermissionError) as exc:
            st.error(str(exc))
    if can_write and actions[1].button('Обновить материалы', width='stretch'):
        if dirty():
            st.warning('Сначала сохраните правки. Обновление добавляет новые материалы, сохраняя ваши тексты.')
        else:
            try:
                with st.spinner('Проверяем новые материалы…'):
                    updated = service.refresh(import_id, period, draft['revision'])
                additions = len(updated['items']) - len(draft['items'])
                discard_editor()
                st.session_state['draft_notice'] = f'Материалы обновлены. Добавлено: {additions}. Ручные тексты сохранены.'
                st.rerun()
            except (StorageError, ValueError, PermissionError) as exc:
                st.error(str(exc))
    if actions[2].button('Сравнить с общей версией', width='stretch'):
        try:
            with st.spinner('Читаем общую версию…'):
                state['remote'] = service.open(import_id, period)
        except (StorageError, ValueError, PermissionError) as exc:
            st.error(str(exc))
    if remote := state.get('remote'):
        latest = remote['draft']
        st.info(f"Общая версия: редакция {latest['revision']}, сохранена {local_time(latest['updated_at'])}.")
        left, right = st.columns(2)
        for col, title, value in [(left, 'В этой вкладке', state['working']), (right, 'В общей базе', editable(latest))]:
            with col, st.expander(title, expanded=True):
                st.text(value['conclusions'] or 'Выводы не заполнены.')
                st.dataframe([{'Включён':i['included'],'Заголовок':i['title'],'Описание':i['description']} for i in value['items']], hide_index=True, width='stretch')
        st.download_button('Скачать мои правки перед заменой', json.dumps(state['working'], ensure_ascii=False, indent=2),
                           file_name='draft-edits.json', mime='application/json', on_click='ignore')
        consent = st.checkbox('Заменить правки в этой вкладке общей версией', key='draft_replace_consent')
        if st.button('Загрузить общую версию', disabled=not consent):
            reset_editor(remote, import_id, period)
            st.session_state.pop('draft_replace_consent', None)
            st.rerun()

    included = sum(i['included'] for i in state['working']['items'])
    st.caption(f"Включено материалов: {included} из {len(draft['items'])}")
    pending = state['view']['issues']
    if pending:
        st.warning(f'Требуют проверки: {len(pending)}. Различия показаны внутри соответствующих материалов.')
    with st.container(key='draft-workspace'):
        materials, conclusions, preview, history = st.tabs(['Материалы', 'Выводы аналитика', 'Предпросмотр', 'История редакций'])
        with materials:
            items = {i['event_id']: i for i in state['working']['items']}
            sources = {i['event_id']: i['source'] for i in draft['items']}
            problem = {i['event_id']: i for i in pending}
            if items:
                groups = {'news': 'Новости сайтов', 'telegram': 'Telegram', 'products': 'Продукция и предложения'}
                available = [k for k in groups if any(s['kind'] == k for s in sources.values())]
                group = st.radio('Группа материалов', available, horizontal=True, key='draft_group_'+state['epoch'],
                    format_func=lambda k: f'{groups[k]} · {sum(s["kind"] == k for s in sources.values())}')
                choices = [i for i in items if sources[i]['kind'] == group]
                selector_key = 'draft_selected_'+state['epoch']+'_'+group
                if st.session_state.get(selector_key) not in choices:
                    remembered = state.setdefault('selections', {}).get(group)
                    st.session_state[selector_key] = remembered if remembered in choices else choices[0]
                def remember_selection():
                    state.setdefault('selections', {})[group] = st.session_state[selector_key]
                selected = st.selectbox('Материал для редактирования', choices, key=selector_key,
                                       format_func=lambda i: items[i]['title'], on_change=remember_selection)
                position = choices.index(selected)
                previous, progress, following = st.columns([1, 2.5, 1])
                def choose(value):
                    st.session_state[selector_key] = value
                    state.setdefault('selections', {})[group] = value
                previous.button('← Предыдущий', disabled=position == 0, on_click=choose,
                                args=(choices[max(position-1, 0)],), width='stretch')
                following.button('Следующий →', disabled=position == len(choices)-1, on_click=choose,
                                 args=(choices[min(position+1, len(choices)-1)],), width='stretch')
                progress.markdown(f'**{groups[group]} · Материал {position+1} из {len(choices)}**')
                with st.expander('Список материалов группы', expanded=False):
                    for number, event_id in enumerate(choices, 1):
                        flags = (' · включён' if items[event_id]['included'] else ' · исключён')
                        flags += ' · требует проверки' if event_id in problem else ''
                        st.button(f'{number}. {items[event_id]["title"]}{flags}',
                                  key=f'jump_{state["epoch"]}_{event_id}', on_click=choose, args=(event_id,),
                                  type='primary' if event_id == selected else 'secondary', width='stretch')
                item, source = items[selected], sources[selected]
                html(f'<div class="current-material"><span>{safe(groups[group])} · {position+1} / {len(choices)}</span>'
                     f'<h3>{safe(item["title"])}</h3></div>')
                st.caption(source['date_label'] + ' · ' + source['competitor_name'] + ' · Дата и первоисточник не редактируются')
                for field, label in [('included','Включить в записку'),('title','Заголовок'),('description','Краткое описание')]:
                    key = f'draft_{state["epoch"]}_{selected}_{field}'
                    st.session_state.setdefault(key, item[field])
                    method = st.checkbox if field == 'included' else st.text_input if field == 'title' else st.text_area
                    kwargs = {'height': 170, 'max_chars':10000} if field == 'description' else {'max_chars':500} if field == 'title' else {}
                    method(label, key=key, disabled=not can_write, on_change=_change, args=(field,key,selected), **kwargs)
                target = url(source.get('provenance', {}).get('article_url') or source.get('url'))
                if target:
                    st.link_button('Открыть первоисточник ↗', target)
                if selected in problem:
                    issue = problem[selected]
                    with st.expander(issue['reason'], expanded=True):
                        _diff(issue)
                        if issue['can_accept']:
                            key = f'draft_{state["epoch"]}_{selected}_accept'
                            st.session_state.setdefault(key, selected in state['accepted'])
                            st.checkbox('Проверил изменения источника; сохранить мои заголовок и описание', key=key,
                                        disabled=not can_write, on_change=_change, args=('accept_version',key,selected))
                        else:
                            st.caption('Исключите материал из записки. Принять неподтверждённую версию нельзя.')
                with st.expander('Исходный текст'):
                    st.text(source['original_text'])
            else:
                st.info('В черновике пока нет материалов. Их можно добавить командой «Обновить материалы».')
        with conclusions:
            key = 'draft_conclusions_'+state['epoch']
            st.session_state.setdefault(key, state['working']['conclusions'])
            st.text_area('Выводы аналитика', key=key, height=300, max_chars=30000, disabled=not can_write,
                         placeholder='Добавьте выводы, риски и рекомендации по итогам периода…',
                         on_change=_change, args=('conclusions',key))
        with preview:
            st.caption('Предпросмотр и Word/ZIP используют одну сохранённую редакцию. Каждый выпуск остаётся в архиве.')
            if st.button('Показать сохранённую записку'):
                if dirty():
                    st.warning('Сначала сохраните правки, чтобы состав записки совпал с сохранённой редакцией.')
                else:
                    try:
                        with st.spinner('Проверяем материалы записки…'):
                            state['preview'] = service.preview(import_id, period, draft['revision'])
                    except (StorageError, ValueError, PermissionError) as exc:
                        state.pop('preview', None)
                        st.error(str(exc))
            if value := state.get('preview'):
                _preview(value)
            if can_write and st.button('Выпустить Word и ZIP',type='primary'):
                if dirty():
                    st.warning('Сначала сохраните правки, затем выпускайте записку.')
                else:
                    from .jobs import JobService
                    from .job_screen import launch
                    try:
                        JobService(settings,identity).export(import_id,period,draft['revision'])
                        launch(settings)
                        st.session_state['show_export_jobs']=True
                        st.rerun()
                    except (StorageError,PermissionError,ValueError) as exc:
                        st.error(str(exc))
            if st.session_state.get('show_export_jobs'):
                from .job_screen import render_jobs
                render_jobs(import_id,settings,identity,can_write,period=period)
        with history:
            if st.button('Показать историю редакций'):
                try:
                    state['history'] = service.history(import_id, period)
                except (StorageError, ValueError, PermissionError) as exc:
                    st.error(str(exc))
            if revisions := state.get('history'):
                st.dataframe([{'Редакция':r['revision'],'Сохранена':local_time(r['updated_at']),'Автор':r['updated_by']} for r in revisions],
                             hide_index=True, width='stretch')
                selected_revision = st.selectbox('Сохранённая редакция', [r['revision'] for r in revisions])
                if st.button('Открыть редакцию для сравнения'):
                    try:
                        state['historical'] = service.history(import_id, period, selected_revision)
                    except (StorageError, ValueError, PermissionError) as exc:
                        st.error(str(exc))
                if old := state.get('historical'):
                    st.caption(f"Историческая редакция {old['revision']}. Это сохранённый черновик, не выпущенный отчёт.")
                    st.text(old['conclusions'] or 'Выводы не заполнены.')
                    st.dataframe([{'Включён':i['included'],'Заголовок':i['title'],'Описание':i['description'],'Дата':i['source']['date_label']}
                                  for i in old['items']], hide_index=True, width='stretch')
