"""Editorial rules for portable facts. Never modify collected events."""
from collections import Counter
from copy import deepcopy
import re

from .library import period_events
from .coverage import monitored


class DraftConflict(ValueError):
    pass


def item_from(event):
    return {'event_id': event['id'], 'included': True, 'title': event['title'],
            'description': event['description'], 'version': event['version'], 'source': deepcopy(event)}


def confirmed(event, asset_ids=None):
    ids = event.get('evidence_ids') or []
    return (event.get('status') == 'confirmed' and bool(ids)
            and all(isinstance(i, str) and re.fullmatch(r'[a-f0-9]{64}', i) for i in ids)
            and (asset_ids is None or all(i in asset_ids for i in ids)))


def create_payload(library, period, asset_ids=None):
    return {'conclusions': '', 'items': [item_from(e) for e in period_events(library, period)
                                        if confirmed(e, asset_ids)]}


def issues(draft, library, asset_ids=None):
    result = []
    members = set(library['period'].get(draft['period'], {}).get('event_ids', []))
    for item in draft['items']:
        current = library['event'].get(str(item['event_id']))
        reason, can_accept = '', False
        if not current or not confirmed(current, asset_ids):
            reason = 'Материал больше не подтверждён или отсутствует доказательство'
        elif item['event_id'] not in members:
            reason = 'Материал больше не относится к периоду записки'
        elif current['version'] != item['version']:
            reason, can_accept = 'Изменился первоисточник', True
        if reason:
            result.append({'event_id': item['event_id'], 'included': item['included'], 'reason': reason,
                           'can_accept': can_accept, 'previous': item['source'], 'current': current})
    return result


def _text(value, maximum, name, required=False):
    if not isinstance(value, str) or len(value) > maximum or '\x00' in value or (required and not value.strip()):
        raise ValueError(f'Проверьте поле «{name}»: допустимо до {maximum} символов' + (', текст обязателен.' if required else '.'))
    return value


def save_payload(draft, library, conclusions, changes, asset_ids=None):
    result = {'conclusions': _text(conclusions, 30000, 'Выводы аналитика'), 'items': deepcopy(draft['items'])}
    if not isinstance(changes, list) or len(changes) > 2000:
        raise ValueError('Некорректный список изменений.')
    items = {i['event_id']: i for i in result['items']}
    seen = set()
    members = set(library['period'].get(draft['period'], {}).get('event_ids', []))
    for change in changes:
        if not isinstance(change, dict) or set(change) - {'event_id', 'included', 'title', 'description', 'accept_version'}:
            raise ValueError('Даты, ссылки и сведения о проверке нельзя редактировать.')
        event_id = change.get('event_id')
        if type(event_id) is not int or event_id not in items or event_id in seen:
            raise ValueError('Материал не принадлежит черновику или указан дважды.')
        seen.add(event_id)
        if type(change.get('included')) is not bool:
            raise ValueError('Некорректный признак включения материала.')
        item = items[event_id]
        item.update(included=change['included'], title=_text(change.get('title'), 500, 'Заголовок', True),
                    description=_text(change.get('description'), 10000, 'Краткое описание'))
        accepted = change.get('accept_version')
        if accepted:
            current = library['event'].get(str(event_id))
            if (not current or not confirmed(current, asset_ids) or event_id not in members
                    or accepted != current['version']):
                raise DraftConflict('Источник снова изменился. Откройте актуальную версию и проверьте различия.')
            # Acknowledgement changes only the immutable source snapshot, not manual text.
            item.update(source=deepcopy(current), version=current['version'])
    return result


def refresh_payload(draft, library, asset_ids=None):
    result = {'conclusions': draft['conclusions'], 'items': deepcopy(draft['items'])}
    existing = {i['event_id'] for i in result['items']}
    result['items'].extend(item_from(e) for e in period_events(library, draft['period'])
                           if e['id'] not in existing and confirmed(e, asset_ids))
    return result


def snapshot(draft, library, asset_ids=None):
    if any(i['included'] for i in issues(draft, library, asset_ids)):
        raise DraftConflict('Проверьте изменённые материалы или исключите неподтверждённые перед предварительным просмотром.')
    selected = [deepcopy(i) for i in draft['items'] if i['included']]
    # Use current equivalent evidence, while preserving reviewed editorial text.
    for item in selected:
        item['source'] = deepcopy(library['event'][str(item['event_id'])])
    period = library['period'][draft['period']]
    return {'draft_id': draft['id'], 'revision': draft['revision'], 'period': draft['period'],
            'conclusions': draft['conclusions'], 'items': selected, 'checks': deepcopy(monitored(period['checks'])),
            'counts': dict(Counter(i['source']['kind'] for i in selected)),
            'incomplete': any(c['status'] in ('partial', 'error') for c in period['checks'])}
