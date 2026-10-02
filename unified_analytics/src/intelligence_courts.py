"""Conservative official court adapter: unknown is never interpreted as closed."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from .intelligence_mentions import normalized
from .intelligence_web import SourceUnavailable

CASE_NUMBER = re.compile(r'(?<!\w)[АA]\d{1,3}-\d+/\d{4}(?!\d)')
ROLES = {'plaintiff': 'Истец', 'respondent': 'Ответчик', 'third': 'Третье лицо', 'other': 'Иное лицо'}


def card_links(html):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    for a in soup.select('a[href]'):
        url = urljoin('https://kad.arbitr.ru/', a['href'])
        if urlsplit(url).hostname == 'kad.arbitr.ru' and re.fullmatch(r'/[Cc]ard/[0-9a-fA-F-]{36}', urlsplit(url).path):
            if url not in links:
                links.append(url)
    # Unrecognized content is not an empty case register.
    if not links and not any(x in soup.get_text(' ', strip=True).casefold() for x in ('дела не найдены', 'ничего не найдено')):
        raise SourceUnavailable('court_search_structure_unrecognized')
    return links


def parse_card(page, entity):
    if urlsplit(page.url).scheme!='https' or urlsplit(page.url).hostname!='kad.arbitr.ru':
        raise SourceUnavailable('court_redirected_outside_official_source')
    soup = BeautifulSoup(page.text, 'html.parser')
    for tag in soup.select('script,style,nav,footer'):
        tag.decompose()
    text = normalized(soup.get_text(' ', strip=True))
    numbers = CASE_NUMBER.findall(text)
    heading = soup.select_one('h1, #case_number, .b-case-number')
    heading_numbers = CASE_NUMBER.findall(heading.get_text(' ', strip=True)) if heading else []
    number = heading_numbers[0] if len(set(heading_numbers)) == 1 else (numbers[0] if len(set(numbers)) == 1 else None)
    if not number:
        raise SourceUnavailable('court_number_ambiguous')
    if not re.search(r'(?<!\d)' + entity['inn'] + r'(?!\d)', text):
        raise SourceUnavailable('court_participant_inn_unconfirmed')
    parties = []
    for role, label in ROLES.items():
        for block in soup.select(f'[data-role="{role}"], .{role}'):
            quote = normalized(block.get_text(' ', strip=True))
            name = block.select_one('.name, [data-name]')
            inn = re.search(r'ИНН\s*:?\s*(\d{10}|\d{12})(?!\d)', quote)
            if name is not None:
                parties.append(dict(name=normalized(name.get_text(' ', strip=True)),
                                    inn=inn.group(1) if inn else None, role=label, evidence=quote))
    # No inference of outcome from claim amounts, a historical decision or silence.
    return {'case_number': number.replace('A', 'А'), 'url': page.url, 'competitor_inn': entity['inn'],
            'parties': parties, 'stage': 'unknown', 'stage_evidence': '', 'court': None,
            'subject': None, 'claimed_amount': None, 'awarded_amount': None, 'next_hearing': None,
            'events': [], 'original_text': text, 'identity_state': 'exact_inn',
            'needs_review': True, 'scope_note': 'ИНН найден в карточке; стадия и состав участников требуют проверки.'}


def validate_observation(value):
    """For an explicitly reviewed official-document import, not arbitrary JSON claims."""
    if not isinstance(value, dict):
        raise ValueError('Неверная карточка дела')
    url = urlsplit(value.get('url', ''))
    if url.scheme != 'https' or url.hostname != 'kad.arbitr.ru' or not re.fullmatch(r'/[Cc]ard/[0-9a-fA-F-]{36}', url.path):
        raise ValueError('Нужна ссылка на официальную карточку КАД')
    text = value.get('original_text', '')
    if not isinstance(text, str) or len(text) < 30 or len(text) > 250000:
        raise ValueError('Нет текста официального доказательства')
    if value.get('case_number') not in text or not re.fullmatch(CASE_NUMBER, value.get('case_number', '')):
        raise ValueError('Номер дела не подтверждён текстом')
    inn = value.get('competitor_inn', '')
    if not re.fullmatch(r'\d{10}', inn) or not re.search(r'(?<!\d)' + inn + r'(?!\d)', text):
        raise ValueError('ИНН конкурента не подтверждён')
    if not value.get('reviewer') or not value.get('reviewed_at'):
        raise ValueError('Для импорта требуется явная проверка сотрудником')
    datetime.fromisoformat(value['reviewed_at'])
    for party in value.get('parties', []):
        if (party.get('role') not in ROLES.values() or not party.get('name')
                or not party.get('evidence') or party['evidence'] not in text
                or party['name'] not in party['evidence']
                or party.get('inn') and party['inn'] not in party['evidence']):
            raise ValueError('Участник не подтверждён фрагментом судебного документа')
    if not any(p.get('inn') == inn for p in value.get('parties', [])):
        raise ValueError('Не установлена роль юридического лица конкурента')
    if value.get('stage') not in ('in_progress', 'closed', 'unknown'):
        raise ValueError('Неизвестная стадия')
    if value['stage'] != 'unknown' and (not value.get('stage_evidence') or value['stage_evidence'] not in text):
        raise ValueError('Стадия не подтверждена')
    for key in ('claimed_amount', 'awarded_amount', 'next_hearing', 'court', 'subject'):
        if value.get(key) is not None:
            quote = value.get(key + '_evidence', '')
            if not quote or quote not in text or str(value[key]) not in quote:
                raise ValueError('Нет подтверждения поля ' + key)
    for event in value.get('events', []):
        if (not event.get('evidence') or event['evidence'] not in text
                or not event.get('description') or event['description'] not in event['evidence']):
            raise ValueError('Судебное событие не подтверждено')
        date = datetime.fromisoformat(event['date']).date()
        if not (date.isoformat() in event['evidence'] or date.strftime('%d.%m.%Y') in event['evidence']):
            raise ValueError('Дата судебного события не подтверждена')
    return value


def update_case(previous, observation, checked_at):
    observation = dict(observation)
    stable = {k: v for k, v in observation.items() if k not in ('checked_at', 'last_attempt_at', 'evidence_ids', 'raw_asset_id')}
    digest = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    history = list((previous or {}).get('history', []))
    if not history or history[-1]['version'] != digest:
        history.append({'version': digest, 'observed_at': checked_at, 'observation': observation})
    return {**observation, 'version': digest, 'history': history,
            'first_seen_at': (previous or {}).get('first_seen_at', checked_at),
            'checked_at': checked_at, 'last_attempt_at': checked_at, 'access_state': 'success'}


def is_current(case, now=None):
    """Three-valued projection: failed/stale checks do not retain an assertion of current state."""
    now = now or datetime.now(timezone.utc)
    if case.get('access_state') != 'success' or case.get('needs_review', True):
        return None
    try:
        checked = datetime.fromisoformat(case['checked_at'])
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    if now - checked > timedelta(days=2) or checked > now + timedelta(minutes=5):
        return None
    return {'in_progress': True, 'closed': False}.get(case.get('stage'))
