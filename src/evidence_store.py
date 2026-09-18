"""Auditable event projection. Legacy observations are never trusted implicitly."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .utils import utcnow_naive


def canonical_url(url: str) -> str:
    p = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    # Paths and meaningful query parameters can be case-sensitive.
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(sorted(query)), ''))


def now() -> str:
    return utcnow_naive().isoformat(timespec='seconds')


class EvidenceStore:
    def __init__(self, storage, initialize=True):
        self.conn = storage.conn
        if not initialize:
            return
        self.conn.executescript('''
        CREATE TABLE IF NOT EXISTS collection_runs (
          id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          period_start TEXT NOT NULL, period_end TEXT NOT NULL, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_checks (
          id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, competitor_code TEXT NOT NULL,
          kind TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
          checked_at TEXT NOT NULL, last_success_at TEXT, cursor TEXT NOT NULL DEFAULT '',
          pages INTEGER NOT NULL DEFAULT 0, items INTEGER NOT NULL DEFAULT 0,
          official_evidence_url TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS source_checks_lookup ON source_checks(competitor_code,kind,url,id);
        CREATE TABLE IF NOT EXISTS verified_events (
          id INTEGER PRIMARY KEY, competitor_code TEXT NOT NULL, competitor_name TEXT NOT NULL,
          kind TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
          original_text TEXT NOT NULL, published_at TEXT, detected_at TEXT NOT NULL,
          date_basis TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
          evidence_json TEXT NOT NULL, content_hash TEXT NOT NULL, checked_at TEXT NOT NULL,
          UNIQUE(competitor_code,kind,url));
        CREATE TABLE IF NOT EXISTS event_versions (
          id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, content_hash TEXT NOT NULL,
          recorded_at TEXT NOT NULL, payload_json TEXT NOT NULL,
          UNIQUE(event_id,content_hash));
        CREATE TABLE IF NOT EXISTS legacy_evidence_audits (
          entity TEXT NOT NULL, entity_id INTEGER NOT NULL, status TEXT NOT NULL,
          reason TEXT NOT NULL, event_id INTEGER, checked_at TEXT NOT NULL,
          PRIMARY KEY(entity,entity_id));
        CREATE TABLE IF NOT EXISTS product_baselines (
          competitor_code TEXT NOT NULL, url TEXT NOT NULL, observed_at TEXT NOT NULL,
          cards_json TEXT NOT NULL, evidence_path TEXT NOT NULL,
          PRIMARY KEY(competitor_code,url));
        CREATE TABLE IF NOT EXISTS event_link_checks (
          run_id INTEGER NOT NULL, event_id INTEGER NOT NULL, url TEXT NOT NULL,
          checked_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
          PRIMARY KEY(run_id,event_id));
        ''')
        self.conn.commit()

    def start(self, period):
        # The caller owns the common run lock: an older running row can only
        # belong to an interrupted process, never to a concurrent collector.
        self.conn.execute("UPDATE collection_runs SET status='interrupted',finished_at=? WHERE status='running'",(now(),))
        cur = self.conn.execute('INSERT INTO collection_runs(started_at,period_start,period_end,status) VALUES(?,?,?,?)',
                                (now(), period.start.isoformat(), period.end.isoformat(), 'running'))
        self.conn.commit()
        return cur.lastrowid

    def check(self, run_id, code, kind, url, status, reason='', cursor='', pages=0, items=0, official_evidence_url=''):
        last = self.conn.execute('SELECT last_success_at FROM source_checks WHERE competitor_code=? AND kind=? AND url=? ORDER BY id DESC LIMIT 1',
                                 (code, kind, url)).fetchone()
        success = now() if status == 'success' else (last[0] if last else None)
        self.conn.execute('''INSERT INTO source_checks(run_id,competitor_code,kind,url,status,reason,checked_at,last_success_at,cursor,pages,items,official_evidence_url)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (run_id, code, kind, url, status, reason, now(), success, cursor, pages, items, official_evidence_url))
        self.conn.commit()

    def finish(self, run_id):
        failed = self.conn.execute("SELECT count(*) FROM source_checks WHERE run_id=? AND status IN ('error','partial')", (run_id,)).fetchone()[0]
        failed += self.conn.execute("SELECT count(*) FROM event_link_checks WHERE run_id=? AND status<>'success'",(run_id,)).fetchone()[0]
        status = 'partial' if failed else 'success'
        self.conn.execute('UPDATE collection_runs SET finished_at=?,status=? WHERE id=?', (now(), status, run_id))
        self.conn.commit()
        return status

    def record(self, item):
        item = dict(item)
        item['url'] = canonical_url(item['url'])
        item.setdefault('status', 'confirmed')
        item.setdefault('reason', '')
        evidence = item.get('evidence', {})
        if item['status'] == 'confirmed':
            from .crawler_base import CrawlerBase
            paths = evidence.get('snapshots', [])
            if not paths or any(not Path(p).is_file() for p in paths):
                item.update(status='rejected', reason='missing_evidence_snapshot')
            elif not item.get('title') or len(item.get('original_text', '')) < 20:
                item.update(status='rejected', reason='missing_content')
            elif not evidence.get('official_url') or not evidence.get('date_evidence'):
                item.update(status='rejected', reason='missing_source_or_date_evidence')
            elif item['title'].strip().casefold() in {'not found','page not found','404','404 not found','страница не найдена'} or CrawlerBase.invalid_content(Path(paths[0]).read_text(encoding='utf-8',errors='replace')):
                item.update(status='rejected',reason='error_page_is_not_publication')
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True)
        # Observation time is not an editorial revision.
        stable = {k: v for k, v in item.items() if k not in {'evidence', 'checked_at', 'detected_at'}}
        digest = hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous = self.conn.execute('SELECT * FROM verified_events WHERE competitor_code=? AND kind=? AND url=?',
                                     (item['competitor_code'], item['kind'], item['url'])).fetchone()
        if item['status']=='confirmed':
            self.conn.execute("UPDATE verified_events SET status='superseded',reason='event_kind_reclassified' WHERE competitor_code=? AND url=? AND kind<>?",(item['competitor_code'],item['url'],item['kind']))
        values = (item['competitor_code'], item['competitor_name'], item['kind'], item['url'], item['title'], item.get('summary', ''),
                  item['original_text'], item.get('published_at'), item.get('detected_at', now()), item['date_basis'], item['status'],
                  item['reason'], json.dumps(evidence, ensure_ascii=False), digest, now())
        self.conn.execute('''INSERT INTO verified_events(competitor_code,competitor_name,kind,url,title,summary,original_text,published_at,
              detected_at,date_basis,status,reason,evidence_json,content_hash,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(competitor_code,kind,url) DO UPDATE SET title=excluded.title,summary=excluded.summary,
              original_text=excluded.original_text,published_at=excluded.published_at,date_basis=excluded.date_basis,
              status=excluded.status,reason=excluded.reason,evidence_json=excluded.evidence_json,
              content_hash=excluded.content_hash,checked_at=excluded.checked_at''', values)
        row = self.conn.execute('SELECT id FROM verified_events WHERE competitor_code=? AND kind=? AND url=?', (item['competitor_code'], item['kind'], item['url'])).fetchone()
        self.conn.execute('INSERT OR IGNORE INTO event_versions(event_id,content_hash,recorded_at,payload_json) VALUES(?,?,?,?)',
                          (row[0], digest, now(), payload))
        self.conn.commit()
        return row[0], previous is None

    def audit(self, entity, ident, status, reason, event_id=None):
        self.conn.execute('INSERT OR REPLACE INTO legacy_evidence_audits VALUES(?,?,?,?,?,?)',
                          (entity, ident, status, reason, event_id, now()))
        self.conn.commit()

    def events(self, period, codes):
        if not codes:
            return []
        date_key = "COALESCE(published_at,strftime('%Y-%m-%dT%H:%M:%S',detected_at,'+5 hours'))"
        rows = self.conn.execute(f'''SELECT * FROM verified_events WHERE status='confirmed'
                 AND {date_key} BETWEEN ? AND ?
                 AND competitor_code IN ({','.join('?' for _ in codes)})
                 ORDER BY {date_key} DESC,competitor_code,url''',
                 (period.start.isoformat(), period.end.isoformat(), *codes)).fetchall()
        result=[dict(row) for row in rows]
        if period.granularity=='range':
            from datetime import datetime
            from dateutil.relativedelta import relativedelta
            # A month-only announcement cannot be assigned to a particular day.
            def within(event):
                if event['date_basis']!='announcement_month':
                    return True
                start=datetime.fromisoformat(event['published_at']).replace(day=1,hour=0,minute=0,second=0)
                end=start+relativedelta(months=1,seconds=-1)
                return period.start<=start and period.end>=end
            result=[e for e in result if within(e)]
        return result

    def coverage(self, period, codes):
        rows = self.conn.execute('''SELECT s.* FROM source_checks s JOIN collection_runs r ON r.id=s.run_id
            WHERE r.period_start<=? AND r.period_end>=?
            ORDER BY s.id DESC''', (period.start.isoformat(), period.end.isoformat())).fetchall()
        seen, result = set(), []
        for row in rows:
            key = (row['competitor_code'], row['kind'], row['url'])
            if key not in seen and key[0] in codes:
                result.append(dict(row)); seen.add(key)
        return result
