"""Read-only Focus snapshot for invited Streamlit users.

The arbitration endpoint supplies aggregates, not individual arbitration cases.
Never infer a zero or a complete history from an empty provider response.
"""

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import streamlit as st

from .access import current_access
from .neon_http import NeonHTTP


LOCAL = timezone(timedelta(hours=5))
MISSING = 'Не передано'
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_PARTS = 1024
METRIC_LABELS = {
    'plaintiff': 'Истец', 'defendant': 'Ответчик',
    'claimant': 'Истец', 'respondent': 'Ответчик',
    'cases': 'Дела', 'caseCount': 'Количество дел',
    'count': 'Количество', 'sum': 'Сумма требований',
    'amount': 'Сумма', 'total': 'Всего',
    'y1': '12 месяцев', 'y3': '3 года',
    'year1': '12 месяцев', 'year3': '3 года',
}
METRIC_WORDS = {
    'petitioner': 'истец', 'defendant': 'ответчик', 'all': 'все дела',
    'count': 'количество дел', 'sum': 'сумма требований',
    'supply': 'споры о поставке', 'won': 'выигранные', 'lost': 'проигранные',
    'partly': 'частично удовлетворённые', 'other': 'прочие',
    'year': 'год', 'month': 'месяц',
}


def load_snapshot(config):
    """Fetch the latest published snapshot from the active library import."""
    store = NeonHTTP(config)
    rows = store.query('''
        SELECT r.key,r.payload FROM skb_analytics.records r
        JOIN skb_analytics.state s ON s.import_id=r.import_id AND s.key='active'
        WHERE r.kind='court_focus_snapshot'
        ORDER BY r.key DESC LIMIT 1
    ''')
    if not rows:
        return None
    manifest = rows[0].get('payload')
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    if not isinstance(manifest, dict) or manifest.get('schema_version') != 1:
        raise ValueError('Invalid Focus snapshot')
    if manifest.get('storage') != 'parts-v1':
        # Compatibility with the initial in-row snapshot format.
        return manifest
    key = rows[0].get('key')
    count = manifest.get('chunk_count')
    digest = manifest.get('sha256')
    if (not isinstance(key, str) or not key or not isinstance(count, int)
            or isinstance(count, bool) or not 1 <= count <= MAX_PARTS
            or not isinstance(digest, str) or len(digest) != 64):
        raise ValueError('Invalid Focus snapshot manifest')
    parts = store.query('''
        SELECT r.key,r.payload FROM skb_analytics.records r
        JOIN skb_analytics.state s ON s.import_id=r.import_id AND s.key='active'
        WHERE r.kind='court_focus_part'
          AND LEFT(r.key, length($1)+1) = $1 || ':'
        ORDER BY r.key
    ''', [key])
    if len(parts) != count:
        raise ValueError('Incomplete Focus snapshot')
    chunks = []
    size = 0
    for index, row in enumerate(parts):
        item = row.get('payload')
        if isinstance(item, str):
            item = json.loads(item)
        if (row.get('key') != f'{key}:{index:03d}' or not isinstance(item, dict)
                or item.get('index') != index or isinstance(item.get('index'), bool)
                or not isinstance(item.get('chunk'), str)):
            raise ValueError('Invalid Focus snapshot part')
        try:
            chunk = base64.b64decode(item['chunk'], validate=True)
        except (ValueError, base64.binascii.Error):
            raise ValueError('Invalid Focus snapshot part') from None
        size += len(chunk)
        if size > MAX_SNAPSHOT_BYTES:
            raise ValueError('Focus snapshot too large')
        chunks.append(chunk)
    raw = b''.join(chunks)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Invalid Focus snapshot checksum')
    payload = json.loads(raw.decode('utf-8'))
    if (not isinstance(payload, dict) or payload.get('schema_version') != 1
            or payload.get('as_of') != manifest.get('as_of')
            or payload.get('fetched_at') != manifest.get('fetched_at')):
        raise ValueError('Invalid Focus snapshot content')
    return payload


def shown(value):
    if value is None or value == '' or value == [] or value == {}:
        return MISSING
    if isinstance(value, bool):
        return 'Да' if value else 'Нет'
    if isinstance(value, (dict, list)):
        return ', '.join(shown(part) for part in value) if isinstance(value, list) else ', '.join(
            f'{METRIC_LABELS.get(key, key)}: {shown(part)}' for key, part in value.items())
    return str(value)


def local_time(value):
    if not value:
        return MISSING
    try:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(LOCAL).strftime('%d.%m.%Y %H:%M ЕКБ')
    except ValueError:
        return shown(value)


def field(label, value):
    st.write(f'**{label}:** {shown(value)}')


def metric_label(key):
    """Readable label without guessing the meaning of unknown API categories."""
    parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+', key)
    if not parts:
        return key
    words = []
    index = 0
    while index < len(parts):
        if index + 1 < len(parts) and parts[index].isdigit() and parts[index + 1].lower() == 'year':
            words.append('за 12 месяцев' if parts[index] == '1' else f'за {parts[index]} года')
            index += 2
            continue
        words.append(METRIC_WORDS.get(parts[index].lower(), parts[index]))
        index += 1
    return ' · '.join(words)


def list_section(label, items, fields, *, empty='Сведения не переданы или не получены; отсутствие записей не подтверждено.'):
    st.markdown(f'**{label}**')
    if not isinstance(items, list) or not items:
        st.caption(empty)
        return
    for index, item in enumerate(items, 1):
        if isinstance(item, dict):
            with st.expander(shown(item.get('number') or item.get('name') or item.get('caseNumber') or f'Запись {index}')):
                for key, title in fields:
                    field(title, item.get(key))
                extra = {key: value for key, value in item.items() if key not in dict(fields)}
                if extra:
                    with st.expander('Остальные полученные поля'):
                        st.json(extra, expanded=False)
        else:
            st.write(shown(item))


def render_metrics(metrics):
    st.markdown('#### Арбитражные дела · сводные показатели')
    st.caption('Показатели Фокуса за 12 месяцев и 3 года. API не передаёт в этом ответе список отдельных арбитражных дел, события и документы.')
    if not isinstance(metrics, dict) or not metrics:
        st.info('Сводные показатели не переданы. Это не означает отсутствие дел.')
        return
    rows = []
    for key, raw in metrics.items():
        # A flag-only object means the provider did not supply a numeric value.
        value = raw.get('data') if isinstance(raw, dict) else raw
        if value is not None and value != '' and value != {}:
            rows.append({'Показатель': metric_label(key), 'Значение': shown(value),
                         'Актуальность': ('Неактуален' if isinstance(raw, dict) and raw.get('isActual') is False
                                         else 'Не указана' if not isinstance(raw, dict) or raw.get('isActual') is None
                                         else 'Актуален')})
    if rows:
        st.dataframe(rows, hide_index=True, width='stretch')
    else:
        st.info('Числовые значения в ответе не переданы.')
    with st.expander('Все показатели API и признаки актуальности'):
        st.caption('Поля без значения сохранены в снимке и показаны здесь как «Не передано».')
        for key, raw in metrics.items():
            value = raw.get('data') if isinstance(raw, dict) else raw
            actuality = raw.get('isActual') if isinstance(raw, dict) else None
            st.write(f'**{metric_label(key)} ({key}):** {shown(value)} · актуальность: {shown(actuality)}')
            if isinstance(raw, dict):
                extras = {k: v for k, v in raw.items() if k not in ('data', 'isActual')}
                if extras:
                    st.json(extras, expanded=False)


def render_general_cases(cases):
    st.markdown('#### Дела судов общей юрисдикции')
    st.caption('Подробные записи этой категории не являются арбитражными делами.')
    if not isinstance(cases, list) or not cases:
        st.info('Карточки дел не переданы. Это не подтверждает отсутствие дел.')
        return
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            continue
        title = shown(case.get('number') or case.get('caseId') or f'Дело {index}')
        with st.expander(title):
            for key, label in (
                ('caseId', 'Идентификатор'), ('number', 'Номер'), ('startDate', 'Дата регистрации'),
                ('type', 'Тип'), ('category', 'Категория'), ('region', 'Код региона'), ('courtName', 'Суд'),
                ('judgeFio', 'Судья'), ('litigation', 'Предмет'), ('decision', 'Результат'),
                ('decisionDate', 'Дата решения')):
                field(label, case.get(key))
            list_section('Участники', case.get('participants'),
                         [('name', 'Наименование'), ('role', 'Роль'), ('roleRaw', 'Роль в источнике'),
                          ('inn', 'ИНН'), ('ogrn', 'ОГРН')])
            list_section('Движение дела', case.get('actions'),
                         [('date', 'Дата'), ('name', 'Событие'), ('result', 'Результат')])
            extras = {k: v for k, v in case.items() if k not in {
                'caseId', 'number', 'startDate', 'type', 'category', 'region', 'courtName', 'judgeFio',
                'litigation', 'decision', 'decisionDate', 'participants', 'actions'}}
            if extras:
                with st.expander('Остальные полученные поля дела'):
                    st.json(extras, expanded=False)


def render_entity(entity):
    st.subheader(shown(entity.get('brand_name') or entity.get('brand') or entity.get('name')))
    st.caption(shown(entity.get('name')) + ' · ИНН ' + shown(entity.get('inn')) + ' · ОГРН ' + shown(entity.get('ogrn')))
    statuses = entity.get('method_status') or {}
    if isinstance(statuses, dict):
        problems = [f'{name}: {shown(detail.get("reason") or detail.get("status"))}'
                    for name, detail in statuses.items() if isinstance(detail, dict)
                    and (detail.get('status') not in ('success', 'ok') or detail.get('complete') is False)]
        if problems:
            st.warning('Неполные или недоступные источники: ' + '; '.join(problems))
        with st.expander('Полнота запросов и источники'):
            if not statuses:
                st.caption('Сведения о полноте запросов не переданы.')
            for name, detail in statuses.items():
                if isinstance(detail, dict):
                    st.write(f'**{name}:** состояние {shown(detail.get("status"))}; полнота {shown(detail.get("complete"))}; страниц {shown(detail.get("pages"))}; записей {shown(detail.get("count"))}; причина {shown(detail.get("reason"))}')
    render_metrics(entity.get('arbitration_metrics'))
    list_section('Связанные истцы по арбитражной аналитике', entity.get('petitioners'),
                 [('name', 'Наименование'), ('inn', 'ИНН'), ('ogrn', 'ОГРН'),
                  ('principalActivity', 'Основная деятельность'), ('y3Count', 'Дел за 3 года'),
                  ('y3Sum', 'Сумма за 3 года'), ('lastCaseDate', 'Дата последнего дела')])
    render_general_cases(entity.get('general_cases'))
    st.markdown('#### Исполнительные производства ФССП')
    st.caption('Дата актуальности сведений ФССП: ' + shown(entity.get('fssp_update_date')))
    list_section('Производства', entity.get('enforcement'), [])
    st.markdown('#### Банкротство и регистрационные сведения')
    bankruptcy = entity.get('bankruptcy') or {}
    if isinstance(bankruptcy, dict):
        for key, label in [('stage', 'Стадия'), ('caseNumber', 'Номер дела'),
                           ('acceptanceDate', 'Дата принятия'), ('stageDate', 'Дата стадии')]:
            field(label, bankruptcy.get(key))
        list_section('Сообщения ЕФРСБ', bankruptcy.get('messages'), [])
        list_section('Стадии банкротства', bankruptcy.get('stages'), [])
        extras = {k: v for k, v in bankruptcy.items() if k not in {
            'stage', 'caseNumber', 'acceptanceDate', 'stageDate', 'messages', 'stages'}}
        if extras:
            with st.expander('Остальные полученные сведения о банкротстве'):
                st.json(extras, expanded=False)
    list_section('Намерения о банкротстве', entity.get('bankruptcy_intentions'), [])
    field('Статус организации', entity.get('status'))
    field('Адрес', entity.get('address'))
    list_section('Прежние наименования', entity.get('previous_names'),
                 [('short', 'Краткое'), ('full', 'Полное'), ('readable', 'В источнике'), ('date', 'Дата')])
    list_section('Правопредшественники', entity.get('predecessors'),
                 [('name', 'Наименование'), ('inn', 'ИНН'), ('ogrn', 'ОГРН'), ('date', 'Дата')])
    extras = {key: value for key, value in entity.items() if key not in {
        'brand', 'brand_name', 'name', 'inn', 'ogrn', 'arbitration_metrics', 'general_cases',
        'method_status', 'petitioners', 'fssp_update_date', 'enforcement', 'bankruptcy',
        'bankruptcy_intentions', 'status', 'address', 'previous_names', 'predecessors'}}
    if extras:
        with st.expander('Остальные полученные сведения об организации'):
            st.json(extras, expanded=False)


def render_arbitration(settings, identity):
    st.subheader('Арбитражные дела')
    try:
        config = settings()
        if not current_access(identity(), config).allowed:
            st.warning('Доступ к данным отозван. Обновите страницу.')
            return
        with st.spinner('Загружаем судебные сведения…'):
            snapshot = load_snapshot(config)
    except Exception:
        st.error('Судебные сведения временно недоступны. Повторите попытку позднее.')
        return
    if snapshot is None:
        st.info('Данные Контур.Фокуса ещё не опубликованы в приложении.')
        return
    st.caption('Источник: ' + shown(snapshot.get('source')) + ' · Срез на ' + shown(snapshot.get('as_of'))
               + ' · Получено ' + local_time(snapshot.get('fetched_at')))
    if snapshot.get('as_of') != datetime.now(LOCAL).date().isoformat():
        st.warning('Срез не датирован сегодняшним днём по Екатеринбургу. Свежие изменения могли не попасть в публикацию.')
    st.warning('Арбитраж: доступны только сводные показатели, без подробных карточек дел и полной истории процессуальных событий. Данные остальных категорий показаны отдельно; пустой ответ не доказывает отсутствие дел.')
    entities = snapshot.get('entities')
    if not isinstance(entities, list) or not entities:
        st.info('В опубликованном снимке сведения об организациях не переданы.')
    else:
        entities = [item for item in entities if isinstance(item, dict)]
        if not entities:
            st.info('Сведения об организациях имеют неподдерживаемый формат.')
            return
        labels = [shown(item.get('brand_name') or item.get('brand') or item.get('name')) + ' · ' + shown(item.get('inn'))
                  for item in entities]
        selected = st.selectbox('Организация', range(len(labels)), format_func=lambda index: labels[index])
        render_entity(entities[selected])
    limitations = snapshot.get('limitations')
    if isinstance(limitations, list) and limitations:
        with st.expander('Ограничения данных'):
            for item in limitations:
                st.write('• ' + shown(item))

