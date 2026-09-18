from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import hashlib
import json
import re
import sqlite3

from src.briefs import russian_brief
from src.utils import normalize_text
from src.report_content import prepare_report_data, evidence_exists
from src.utils import parse_report_period, build_date_range, report_period_key
from src.verified_monitor import sources_for

LOCAL = timezone(timedelta(hours=5))


def month(value=None, date_from=None, date_to=None):
    """Resolve an exact inclusive interval; YYYY-MM remains a legacy alias."""
    if date_from is not None or date_to is not None:
        if value or not date_from or not date_to:
            raise ValueError('Укажите обе даты диапазона, без одновременного выбора месяца')
        value=f'{date_from}__{date_to}'
    value = value or datetime.now(LOCAL).strftime('%Y-%m')
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])|\d{4}-\d{2}-\d{2}__\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Укажите месяц или диапазон дат')
    period = parse_report_period(value, now=datetime.now(LOCAL).replace(tzinfo=None))
    if period.start > datetime.now(LOCAL).replace(tzinfo=None):
        raise ValueError('Нельзя выбрать будущий период')
    if period.granularity == 'range' and period.end.date() > datetime.now(LOCAL).date():
        raise ValueError('Дата окончания не может быть в будущем')
    return value, period


def version(event):
    # Transport markup, snapshot filenames and observation time are not edits.
    keys=('id','competitor_code','kind','url','title','original_text','status','reason','date_basis','published_at')
    content={k:event.get(k) for k in keys}
    for key in ('title','original_text'):
        content[key]=normalize_text(content.get(key) or '')
    evidence=json.loads(event.get('evidence_json') or '{}')
    content['provenance']={k:evidence.get(k) for k in ('official_url','source_url','article_url','date_evidence')}
    if event.get('date_basis')=='observed_change':
        content['detected_at']=event.get('detected_at')
    return hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def date_label(event):
    raw = event['published_at'] or event['detected_at']
    date = datetime.fromisoformat(raw)
    if event['date_basis'] == 'observed_change':
        date = date.replace(tzinfo=timezone.utc).astimezone(LOCAL)
        return date.strftime('%d.%m.%Y') + ' (обнаружено, Екатеринбург)'
    if event['date_basis'] == 'announcement_month':
        return date.strftime('%m.%Y') + ' (месяц анонса)'
    return date.strftime('%d.%m.%Y')


class Facts:
    def __init__(self, settings, competitors):
        self.settings, self.competitors = settings, competitors

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.settings.database.as_uri() + '?mode=ro', uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        try:
            yield conn
        finally:
            conn.close()

    def raw_event(self, event_id):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM verified_events WHERE id=?', (event_id,)).fetchone()
            return dict(row) if row else None

    def public_event(self, event, translate=False):
        value = dict(event)
        value['version'] = version(event)
        value['date_label'] = date_label(event)
        value['description'] = russian_brief(event['original_text'], self.settings.translation_enabled if translate else False)
        value['evidence_available'] = evidence_exists(event)
        value.pop('evidence_json', None)
        value['evidence_url'] = f"/api/v1/events/{event['id']}/evidence"
        return value

    def projection(self, key):
        _, period = month(key)
        with self.connect() as conn:
            events, checks, links = prepare_report_data(SimpleNamespace(conn=conn), self.competitors, period)
            if period.end.date() >= datetime.now(LOCAL).date():
                # An ongoing month's coverage is reported as of the latest run,
                # not as an impossible claim about time after that run ended.
                row = conn.execute('SELECT MAX(period_end) cutoff FROM collection_runs WHERE period_start<=? AND period_end>=? AND period_end<=?',
                    (period.start.isoformat(),period.start.isoformat(),period.end.isoformat())).fetchone()
                if row and row['cutoff']:
                    from src.evidence_store import EvidenceStore
                    cutoff = period.model_copy(update={'end':datetime.fromisoformat(row['cutoff'])})
                    checks = EvidenceStore(SimpleNamespace(conn=conn),initialize=False).coverage(cutoff,[c.code for c in self.competitors])
        lookup = {(c['competitor_code'],c['kind'],c['url']): c for c in checks}
        complete = []
        for competitor in self.competitors:
            sources = [(s['kind'],s['url']) for s in sources_for(competitor)]
            if competitor.telegram and competitor.telegram.enabled and competitor.telegram.url:
                sources.append(('telegram', competitor.telegram.url))
            for kind,url in sources:
                check = lookup.get((competitor.code,kind,url)) or dict(competitor_code=competitor.code,
                    kind=kind,url=url,status='partial' if url else 'not_configured',reason='Ещё не проверен' if url else 'Публичный канал не подтверждён',last_success_at=None)
                complete.append({**check, 'competitor_name':competitor.name})
        return events, complete, links

    def list_events(self, key, competitor='', kind='', query=''):
        events, _, _ = self.projection(key)
        query = query.casefold()
        return [e for e in events if (not competitor or e['competitor_code']==competitor)
                and (not kind or e['kind']==kind)
                and (not query or query in (e['title']+' '+e['original_text']).casefold())]

    def runs(self, run_id=None):
        with self.connect() as conn:
            if run_id is None:
                return [dict(r) for r in conn.execute('SELECT * FROM collection_runs ORDER BY id DESC LIMIT 100')]
            row = conn.execute('SELECT * FROM collection_runs WHERE id=?',(run_id,)).fetchone()
            if not row:
                raise LookupError('Сбор не найден')
            return {**dict(row),'checks':[dict(r) for r in conn.execute('SELECT * FROM source_checks WHERE run_id=? ORDER BY id',(run_id,))]}
