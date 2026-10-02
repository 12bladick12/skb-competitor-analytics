"""Authenticated external mentions and current court register."""
from datetime import datetime, timedelta, timezone
import json

import streamlit as st

from src.intelligence_registry import ENTITIES
from src.intelligence_courts import is_current
from .drive_store import StorageError


REASONS = {
    'search_api_not_configured': 'Широкий поиск не подключён. Проверяются только найденные ранее и заданные ссылки.',
    'robots_denied': 'Автоматический доступ запрещён правилами источника.',
    'robots_unavailable': 'Не удалось проверить правила доступа к источнику.',
    'court_search_structure_unrecognized': 'Формат ответа картотеки не распознан; полнота поиска неизвестна.',
    'court_participant_inn_unconfirmed': 'ИНН конкурента не подтверждён в карточке дела.',
    'card_requires_review': 'Требуется сверить участников и текущую стадию дела.',
    'article_budget_reached': 'Достигнут лимит публикаций за запуск; проверка неполная.',
    'case_budget_reached': 'Достигнут лимит дел за запуск; проверка неполная.',
    'needs_review': 'Не подтверждены принадлежность компании, дата или содержание.',
    'pdf_reader_not_installed': 'Не установлен модуль чтения PDF.',
    'confirmed': 'Принадлежность компании, содержание и дата публикации подтверждены.',
    'duplicate': 'Повтор уже сохранённого текста; отдельное событие не создано.',
    'outside_period': 'Дата публикации находится за пределами выбранного периода.',
    'date_unconfirmed': 'Дата публикации не подтверждена; материал не включён в сводку.',
    'official_site': 'Ссылка ведёт на собственный сайт конкурента.',
    'review_evidence_mismatch': 'Документ изменился или текст сверки не совпадает. Подтверждение не применено.',
}


def explain(reason):
    if reason.startswith('review_not_applied:'):
        return 'Подтверждение не применено: ' + explain(reason.split(':',1)[1])
    if reason.startswith('access_restricted_http_') or reason == 'access_challenge':
        return 'Источник ограничил доступ. Отсутствие результатов не означает отсутствие дел или публикаций.'
    return REASONS.get(reason, reason)


def selected_mentions(library, period, code='', include_review=True):
    from web.facts import month
    _, interval = month(period)
    start, end = interval.start.date().isoformat(), interval.end.date().isoformat()
    rows = []
    for row in library.get('intel_mention', {}).values():
        if code and row['competitor_code'] != code:
            continue
        date = row.get('published_at')
        if date and not start <= date[:10] <= end:
            continue
        if row['status'] in ('duplicate', 'outside_period', 'official_site'):
            continue
        if not include_review and row['status'] != 'confirmed':
            continue
        rows.append(row)
    return sorted(rows, key=lambda r: (r['status']=='confirmed', r.get('published_at') or r.get('detected_at', '')), reverse=True)


def render_intelligence(library, page, period, access, settings, identity):
    from .screens import coverage_table, local_time, file_bytes, url
    from .job_screen import launch, render_jobs
    from .jobs import JobService
    court_mode = page == 'Судебные дела'
    st.subheader(page)
    st.caption('ТЕКО · BESKONTA / СОЧЕР · СЕНСОР · МЕГА-К. '
               + ('Реестр дел не ограничен месяцем сводки.' if court_mode else 'Дата обнаружения не подменяет дату публикации.'))
    options = {'': 'Все четыре конкурента', **{k: e['name'] for k, e in ENTITIES.items()}}
    code = st.selectbox('Конкурент', list(options), format_func=options.get, key='intel_code_' + page)
    checks = sorted(library.get('intel_check', {}).values(), key=lambda c: c['checked_at'], reverse=True)
    latest = {}
    for c in checks:
        if (not code or c['competitor_code'] == code) and c['kind'] == ('litigation' if court_mode else 'mentions'):
            latest.setdefault((c['competitor_code'], c['url']), c)
    if not latest:
        st.info('Источники этого раздела ещё не проверялись. Отсутствие записей не означает отсутствие событий.')
    with st.expander('Полнота и дата проверки источников', expanded=not latest):
        view = [{**c, 'reason': explain(c.get('reason', ''))} for c in latest.values()]
        coverage_table(view, diagnostic=access.role == 'admin')
    if access.role in ('admin', 'editor'):
        if st.button('Обновить упоминания и судебные дела', key='intel_run_' + page, type='primary'):
            try:
                JobService(settings, identity).collect(library['id'], period, scope='intelligence')
                launch(settings)
                st.rerun()
            except (StorageError, ValueError, PermissionError) as exc:
                st.error(str(exc))
        render_jobs(library['id'], settings, identity, True)
        st.caption('Первичный поиск за 90 дней доступен в «Сборе данных»: выберите диапазон дат.')
    if court_mode:
        if access.role in ('admin','editor'):
            with st.expander('Добавить сверенные сведения из судебной карточки'):
                st.caption('JSON с участниками, стадией и дословными основаниями. При обработке текст будет повторно сверен с официальной страницей; при недоступности суда подтверждение не выполняется.')
                uploaded=st.file_uploader('Проверенные карточки',type=['json'],key='intel_court_import')
                acknowledged=st.checkbox('Я сверил сведения с официальными судебными документами',key='intel_court_ack')
                if st.button('Передать на проверку и сохранить',disabled=not uploaded or not acknowledged):
                    try:
                        if uploaded.size>2*1024*1024:
                            raise ValueError('Файл должен быть не больше 2 МБ')
                        values=json.loads(uploaded.getvalue().decode('utf-8-sig'))
                        JobService(settings,identity).collect(library['id'],period,scope='intelligence',case_imports=values)
                        launch(settings)
                        st.rerun()
                    except (ValueError,TypeError,StorageError,PermissionError):
                        st.error('Не удалось принять карточки. Проверьте формат, ИНН, роли, даты и подтверждающие фрагменты.')
        mode = st.radio('Показать', ['Текущие и требующие уточнения', 'Все сохранённые дела'], horizontal=True)
        rows = [c for c in library.get('intel_case', {}).values() if not code or c['competitor_code'] == code]
        if mode.startswith('Текущие'):
            rows = [c for c in rows if is_current(c) is not False]
        if not rows:
            st.info('Подтверждённых карточек для выбранных условий пока нет. Смотрите состояние доступа к картотеке выше.')
        for case in sorted(rows, key=lambda c: c.get('checked_at', ''), reverse=True):
            current = is_current(case)
            label = 'Рассматривается' if current is True else 'Завершено' if current is False else 'Текущая стадия не подтверждена'
            with st.expander(case['competitor_name'] + ' · ' + case['case_number'] + ' · ' + label):
                st.caption('Последнее успешное чтение: ' + local_time(case.get('checked_at')) + '. '
                           'ИНН конкурента: ' + case['competitor_inn'])
                if current is None:
                    st.warning('Сохранённое состояние не подтверждает положение дел на сегодня. Требуется актуальная сверка.')
                st.write('Суд: ' + (case.get('court') or 'Не подтверждён'))
                st.write('Предмет: ' + (case.get('subject') or 'Не подтверждён'))
                st.write('Заявлено: ' + str(case.get('claimed_amount') if case.get('claimed_amount') is not None else 'Не указано') +
                         ' · Присуждено: ' + str(case.get('awarded_amount') if case.get('awarded_amount') is not None else 'Не указано'))
                st.write('Следующее заседание: ' + (case.get('next_hearing') or 'Не подтверждено'))
                st.dataframe([{'Организация': p['name'], 'ИНН': p.get('inn') or 'Не указан', 'Роль': p['role'],
                               'Основание': p.get('evidence', '')} for p in case.get('parties', [])], hide_index=True)
                if url(case['url']):
                    st.link_button('Карточка суда', case['url'])
                for entry in case.get('history', []):
                    st.caption('Версия от ' + local_time(entry['observed_at']))
                st.text(case.get('original_text', ''))
    else:
        include_review = st.checkbox('Показывать материалы для проверки', value=True)
        rows = selected_mentions(library, period, code, include_review)
        if not rows:
            st.info('Подтверждённых внешних публикаций в выбранной части базы нет. Полнота поиска показана выше.')
        for item in rows:
            from urllib.parse import urlsplit
            with st.expander(item['competitor_name'] + ' · ' + (item.get('title') or 'Материал с сайта '+str(urlsplit(item['url']).hostname))):
                st.caption('Дата публикации: ' + (item.get('published_at') or 'Не подтверждена') +
                           ' · Проверено: ' + local_time(item.get('checked_at')))
                if item['status'] != 'confirmed':
                    st.warning('Материал требует проверки и не включён в подтверждённую сводку.')
                if item.get('access_state') != 'success':
                    st.warning('Последняя попытка чтения не удалась. Ниже показана сохранённая версия.')
                if item.get('geography') and item['geography'] != 'РФ':
                    st.caption('География: ' + item['geography'])
                st.write(item.get('description', ''))
                if url(item.get('publisher_url')):
                    st.link_button('Открыть публикацию', item['publisher_url'])
                for org in item.get('organizations', []):
                    st.write(org['name'] + ': ' + org['quote'])
                st.caption('Упоминание организации само по себе не подтверждает отношения заказчика или поставщика.')
                st.text(item['original_text'][:20000])
                if item.get('raw_asset_id') and st.button('Загрузить сохранённый источник', key='intel_proof_' + item['id']):
                    try:
                        content = file_bytes(settings, identity, library['id'], item['raw_asset_id'])
                        if item.get('format') == 'pdf':
                            st.download_button('Скачать PDF', content, file_name=item['id'] + '.pdf', mime='application/pdf')
                        else:
                            st.code(content.decode('utf-8', errors='replace'), language='html')
                    except (StorageError, ValueError, PermissionError) as exc:
                        st.error(str(exc))
    with st.expander('Юридические лица и основание сопоставления'):
        st.dataframe([{'Бренд': e['name'], 'Юрлицо': e['legal_name'], 'ИНН': e['inn'],
                       'Источник': e['registry_url'], 'Проверено': e['identity_checked_at'],
                       'ЕГРЮЛ': 'Выписка пока не сверена'} for k, e in ENTITIES.items() if not code or k == code], hide_index=True)
