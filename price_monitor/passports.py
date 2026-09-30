"""Durable, independent document queues. No network calls in transactions."""
from __future__ import annotations

import hashlib
import json
import time

from .models import utcnow
from .passport_sources import VERSION


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Passports:
    def __init__(self, repository):
        self.repo = repository

    def product(self, rule_id):
        rows = self.repo.batch('''SELECT q.*,i.title,i.category,i.updated_at card_checked_at,
            d.details_json,p.state document_state,p.current_fingerprint,p.source_url,p.etag,p.last_modified,
            p.checked_at document_checked_at,p.note document_note,l.state product_state,l.checked_at lifecycle_checked_at
            FROM rules q LEFT JOIN product_index i ON i.rule_id=q.id
            LEFT JOIN product_documents d ON d.fingerprint=i.details_hash
            LEFT JOIN passport_products p ON p.rule_id=q.id
            LEFT JOIN product_lifecycle l ON l.rule_id=q.id WHERE q.id=%(id)s''', {'id': rule_id})
        return rows[0] if rows else None

    def enqueue(self, rule_id, kind, payload, key=None):
        if kind not in ('download','manual'):raise ValueError('Поддерживается только получение паспортов')
        job = digest([kind, key if key is not None else [rule_id, payload]])
        self.repo.batch('''INSERT INTO passport_jobs(id,rule_id,kind,payload_json,created_at,updated_at)
            VALUES(%(job)s,%(id)s,%(kind)s,%(payload)s,%(now)s,%(now)s) ON CONFLICT(id) DO NOTHING''',
            {'job': job, 'id': rule_id, 'kind': kind, 'payload': json.dumps(payload, ensure_ascii=False), 'now': utcnow()})
        return job

    def schedule(self, source=None, limit=200):
        """Backfill old catalog data and resume after partial writes without adding prices."""
        from .scope import VISIBLE
        now = utcnow()
        params = {'now': now, 'month': now[:7], 'limit': limit, 'source': source or ''}
        rows = self.repo.batch('''SELECT q.id,i.details_hash FROM rules q
            JOIN product_index i ON i.rule_id=q.id JOIN product_documents d ON d.fingerprint=i.details_hash
            LEFT JOIN passport_products p ON p.rule_id=q.id LEFT JOIN product_lifecycle l ON l.rule_id=q.id
            WHERE ''' + VISIBLE + ''' AND (%(source)s='' OR q.source=%(source)s)
            AND COALESCE(l.state,'active')<>'archived'
            AND (p.rule_id IS NULL OR p.scheduled_input<>i.details_hash OR p.scheduled_period<>%(month)s)
            ORDER BY COALESCE(p.checked_at,''),q.id LIMIT %(limit)s''', params)
        statements=[];batch_params={}
        for index,row in enumerate(rows):
            # The job key contains the month and input revision. A missing file is
            # searched again next month, not on every intermediate collection run.
            payload = {'input_hash': row['details_hash'], 'period': now[:7], 'version': VERSION}
            job = digest(['download', [row['id'], payload]])
            sql=['''INSERT INTO passport_products(rule_id,input_hash) VALUES(%(id)s,'')
                ON CONFLICT(rule_id) DO NOTHING''', '''INSERT INTO passport_jobs(id,rule_id,kind,payload_json,created_at,updated_at)
                VALUES(%(job)s,%(id)s,'download',%(payload)s,%(now)s,%(now)s) ON CONFLICT(id) DO NOTHING''',
                'UPDATE passport_products SET scheduled_period=%(period)s,scheduled_input=%(input)s WHERE rule_id=%(id)s']
            values={'id':row['id'],'job':job,'payload':json.dumps(payload),'now':now,'period':now[:7],'input':row['details_hash']}
            import re
            prefix=f'r{index}_'
            statements.extend(re.sub(r'%\((\w+)\)s',lambda m:'%('+prefix+m[1]+')s',s) for s in sql)
            batch_params.update({prefix+k:v for k,v in values.items()})
            if len(statements)>=30 or index==len(rows)-1:
                self.repo.batch(statements,batch_params);statements=[];batch_params={}
        return len(rows)

    def claim(self, kind, owner, lease=900, rule_id=None):
        if kind not in ('download','manual'):raise ValueError('Поддерживается только получение паспортов')
        # UPDATE ... RETURNING + predicate recheck protects both SQLite and PG
        # against two workers claiming the same task after a competing commit.
        now = int(time.time())
        eligible = "kind=%(kind)s AND ((state IN ('pending','retry') AND not_before<=%(time)s) OR (state='processing' AND lease_until<%(time)s))"
        eligible += " AND (%(rule)s IS NULL OR rule_id=%(rule)s)"
        rows = self.repo.batch(f'''UPDATE passport_jobs SET state='processing',owner=%(owner)s,
            lease_until=%(lease)s,attempts=attempts+1,updated_at=%(now)s
            WHERE id=(SELECT id FROM passport_jobs WHERE {eligible} ORDER BY created_at,id LIMIT 1)
            AND ({eligible}) RETURNING *''',
            {'kind': kind, 'owner': owner, 'time': now, 'lease': now + lease, 'now': utcnow(), 'rule':rule_id})
        return rows[0] if rows else None

    def renew(self, job, owner, lease=900):
        return bool(self.repo.batch('''UPDATE passport_jobs SET lease_until=%(lease)s WHERE id=%(id)s
            AND owner=%(owner)s AND state='processing' AND lease_until>=%(time)s RETURNING id''',
            {'id': job, 'owner': owner, 'lease': int(time.time()) + lease, 'time': int(time.time())}))

    @staticmethod
    def guard():
        return "EXISTS(SELECT 1 FROM passport_jobs WHERE id=%(job)s AND owner=%(owner)s AND state='processing' AND lease_until>=%(time)s)"

    def finish(self, job, owner, state='done', detail='', delay=0):
        self.repo.batch('''UPDATE passport_jobs SET state=%(state)s,detail=%(detail)s,not_before=%(after)s,
            lease_until=0,updated_at=%(now)s WHERE id=%(job)s AND owner=%(owner)s AND state='processing'
            AND lease_until>=%(time)s''', {'job': job, 'owner': owner, 'state': state, 'detail': detail[:1500],
            'after': int(time.time()) + delay, 'now': utcnow(), 'time': int(time.time())})

    def state(self, job, owner, state, note='', **extra):
        payload = json.loads(job['payload_json'])
        params = {'id': job['rule_id'], 'job': job['id'], 'owner': owner, 'time': int(time.time()),
                  'now': utcnow(), 'state': state, 'note': note[:1500], 'input': payload.get('input_hash', '')}
        allowed = {'current_fingerprint', 'source_url', 'etag', 'last_modified'}
        updates = []
        for key, value in extra.items():
            if key not in allowed: raise ValueError('Unsupported document field')
            params[key] = value
            updates.append(f'{key}=%({key})s')
        return bool(self.repo.batch('''UPDATE passport_products SET state=%(state)s,note=%(note)s,
            checked_at=%(now)s,input_hash=%(input)s''' + (',' + ','.join(updates) if updates else '') +
            ' WHERE rule_id=%(id)s AND '+"(%(input)s='' OR EXISTS(SELECT 1 FROM product_index WHERE rule_id=%(id)s AND details_hash=%(input)s)) AND " + self.guard() + ' RETURNING rule_id', params))

    def register(self, job, owner, fingerprint, object_key, size, pages, classification, url, etag='', modified=''):
        now = utcnow()
        p = {'id': job['rule_id'], 'job': job['id'], 'owner': owner, 'time': int(time.time()), 'now': now,
             'fp': fingerprint, 'key': object_key, 'size': size, 'pages': pages, 'url': url,
             'kind': classification['kind'], 'language': classification['language'],
             'revision': classification.get('revision_text',''),
             'applies': classification['applicability'], 'reason': classification['reason'],
             'event': digest(['passport', job['rule_id'], fingerprint])}
        guard = self.guard()
        rows = self.repo.batch([f'''INSERT INTO passport_files(fingerprint,object_key,byte_size,page_count,kind,language,revision_text,created_at)
            SELECT %(fp)s,%(key)s,%(size)s,%(pages)s,%(kind)s,%(language)s,%(revision)s,%(now)s WHERE {guard}
            ON CONFLICT(fingerprint) DO NOTHING''', f'''INSERT INTO passport_links(rule_id,fingerprint,url,applicability,evidence,created_at)
            SELECT %(id)s,%(fp)s,%(url)s,%(applies)s,%(reason)s,%(now)s WHERE {guard}
            ON CONFLICT(rule_id,fingerprint,url) DO NOTHING''', f'''INSERT INTO product_events(id,rule_id,kind,detail,created_at)
            SELECT %(event)s,%(id)s,'passport_revision',%(fp)s,%(now)s WHERE {guard}
            ON CONFLICT(id) DO NOTHING''', f'SELECT %(fp)s fingerprint WHERE {guard}'], p)
        if not rows: return False
        self.state(job, owner, 'downloaded' if p['applies'] == 'confirmed' else 'review', p['reason'],
                   current_fingerprint=fingerprint, source_url=url, etag=etag, last_modified=modified)
        return True

    def progress(self):
        return self.repo.batch("SELECT kind,state,count(*) total FROM passport_jobs WHERE kind IN ('download','manual') GROUP BY kind,state ORDER BY kind,state")

    def heartbeat(self, owner, state, detail=''):
        self.repo.batch('''INSERT INTO passport_workers(owner,heartbeat,state,detail) VALUES(%(owner)s,%(time)s,%(state)s,%(detail)s)
            ON CONFLICT(owner) DO UPDATE SET heartbeat=excluded.heartbeat,state=excluded.state,detail=excluded.detail''',
            {'owner': owner, 'time': int(time.time()), 'state': state, 'detail': detail[:500]})
