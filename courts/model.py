"""Portable records. Semantic facts, dates and provenance have separate identities."""
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from urllib.parse import urlsplit

KINDS = {'court': 'Судебные дела', 'enforcement': 'Исполнительные производства'}
DOMAINS = ('arbitral', 'general', 'magistrate', 'supreme', 'constitutional', 'enforcement', 'bankruptcy')
ROLES = {'plaintiff':'Истец', 'defendant':'Ответчик', 'applicant':'Заявитель',
         'third_party':'Третье лицо', 'creditor':'Кредитор', 'debtor':'Должник',
         'victim':'Потерпевший', 'interested':'Заинтересованное лицо'}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()).hexdigest()


def safe_url(value):
    try:
        p=urlsplit(value or '')
        return value if p.scheme in ('https','http') and p.hostname and not p.username and not p.password else ''
    except ValueError:
        return ''


def case_key(jurisdiction, court, number):
    if not all((jurisdiction, court, number)):
        raise ValueError('Не определены суд, вид производства или номер дела.')
    normalized=re.sub(r'\s+', '', number.upper()).replace('A','А')
    return digest([jurisdiction, court.casefold().strip(), normalized])


def participation(entity, participants):
    """Only structured party rows with identity and role evidence are admissible."""
    return [p for p in participants if p.get('inn') == entity['inn']
            and (not p.get('ogrn') or p['ogrn'] == entity['ogrn'])
            and p.get('role') in ROLES and p.get('role_evidence') and p.get('identity_evidence')]


def money(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not value.get('currency') or value.get('amount') is None:
        raise ValueError('Сумма и валюта должны быть указаны отдельно.')
    try:
        number=Decimal(str(value['amount']))
        if not number.is_finite() or number < 0:
            raise InvalidOperation()
    except InvalidOperation:
        raise ValueError('Некорректная денежная сумма.') from None
    return {**value, 'amount':str(number)}


@dataclass
class Page:
    source: str
    domain: str
    entity_id: str
    url: str
    status: str = 'partial'
    reason: str = ''
    cases: list = field(default_factory=list)
    events: list = field(default_factory=list)
    documents: list = field(default_factory=list)
    enforcement: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    objects: dict = field(default_factory=dict)
    next_cursor: dict | None = None
    complete: bool = False
    checked_at: str = field(default_factory=now)
    coverage_from: str | None = None
    coverage_to: str | None = None
    history_complete: bool = False
    known_cases_refreshed: bool = False
    diagnostics: dict = field(default_factory=dict)

    def validate(self):
        if self.status == 'success' and (not self.complete or self.next_cursor is not None):
            raise ValueError('Полнота не подтверждена: успешный статус недопустим.')
        if self.complete and self.status != 'success':
            raise ValueError('Неполный источник нельзя объявить полностью проверенным.')
        for key, value in self.objects.items():
            if hashlib.sha256(value).hexdigest() != key:
                raise ValueError('Повреждено доказательство.')


def normalize_event(raw, case, entity, checked_at):
    roles=participation(entity, case.get('participants', []))
    if not roles or not raw.get('evidence_ids') or not safe_url(raw.get('url')):
        return None
    occurred=raw.get('occurred_on')
    if occurred:
        occurred=date.fromisoformat(occurred).isoformat()
        if occurred > checked_at[:10]:
            return None  # Future hearings belong in the card, not completed-event news.
    if not raw.get('type') or not raw.get('title'):
        return None
    # Dates without published precision remain unknown; detection never supplies them.
    fact={k:deepcopy(raw.get(k)) for k in ('type','title','description','occurred_on','document_number','outcome','text_digest')}
    ident=raw.get('canonical_event_id') or digest([case['id'], fact])
    return {**deepcopy(raw), 'id':ident, 'case_id':case['id'], 'entity_id':entity['id'],
            'brand':entity['brand'], 'occurred_on':occurred, 'detected_at':checked_at,
            'checked_at':checked_at, 'version':digest(fact), 'roles':[p['role'] for p in roles],
            'confirmed':True}


def news_event(event, case, entity):
    if not event.get('confirmed') or not event.get('occurred_on'):
        return None
    key='court:'+digest([event['id'],entity['id']])
    description=event.get('description') or event['title']
    return {'id':key,'court_event_id':event['id'],'case_id':case['id'],'entity_id':entity['id'],
            'enforcement_id':event.get('enforcement_id'),
            'competitor_code':entity['brand'],'competitor_name':entity['brand_name'],
            'kind':'enforcement' if event.get('enforcement_id') else 'court',
            'url':event['url'],'title':case['number']+' · '+event['title'],
            'description':description,'original_text':description,
            'published_at':event['occurred_on'],'detected_at':event['detected_at'],
            'date_label':date.fromisoformat(event['occurred_on']).strftime('%d.%m.%Y'),
            'date_basis':'published','status':'confirmed','reason':'',
            'version':event['version'],'evidence_ids':event['evidence_ids'],
            'provenance':{'article_url':event['url'],'source_url':event['url'],
                          'official_url':case.get('official_url') or '', 'date_evidence':event.get('date_evidence','Дата документа')},
            'evidence':{'case_number':case['number'],'roles':event['roles']}}
