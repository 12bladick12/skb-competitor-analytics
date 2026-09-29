"""Shared durable catalog operations, using short atomic PostgreSQL batches.

The same SQL operations also work with the local SQLite store. HTTP collection
never runs inside a database transaction. Specifications are content-addressed.
"""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
import sqlite3
import time

from .catalog_schema import document
from .models import utcnow
from .monthly import MonthlyMemory, month_window, page_key, page_memory_statement


class CatalogRepository:
    def __init__(self, settings=None, path=None):
        self.settings=settings
        self.path=Path(path) if path else None

    def batch(self, statements, params=None):
        from .db_batches import batch, read_statements
        if isinstance(statements,str):statements=[statements]
        params=params or {}
        if self.settings:
            return batch(self.settings,statements,params)
        c=sqlite3.connect(self.path,timeout=20)
        c.row_factory=sqlite3.Row
        c.create_function('lower',1,lambda x:str(x).casefold() if x is not None else None,deterministic=True)
        c.execute('PRAGMA foreign_keys=ON')
        try:
            c.execute('BEGIN' if read_statements(statements) else 'BEGIN IMMEDIATE')
            result=[]
            for sql in statements:
                cur=c.execute(re.sub(r'%\((\w+)\)s',r':\1',sql),params)
                if cur.description:result=[dict(row) for row in cur.fetchall()]
            c.commit()
            return result
        except Exception:
            c.rollback();raise
        finally:c.close()

    @staticmethod
    def authority():
        return """(EXISTS(SELECT 1 FROM external_sources e WHERE e.source=%(source)s
            AND e.enabled=1 AND e.owner=%(owner)s AND e.heartbeat>%(lease_after)s AND e.protocol>=2)
            OR (NOT EXISTS(SELECT 1 FROM external_sources e WHERE e.source=%(source)s AND e.enabled=1)
            AND EXISTS(SELECT 1 FROM worker_lease w WHERE w.owner=%(owner)s AND w.heartbeat>%(lease_after)s)))"""

    def params(self,run_id,source,owner):
        return {'run':run_id,'source':source,'owner':owner,'lease_after':time.time()-120,'now':utcnow()}

    def allowed(self):
        return self.authority()+" AND EXISTS(SELECT 1 FROM runs WHERE id=%(run)s AND state='running' AND cancel_requested=0)"

    def sources(self,run_id=None):
        return self.batch('SELECT * FROM catalog_sources'+(' WHERE run_id=%(run)s' if run_id is not None else " WHERE state IN ('pending','running')")+' ORDER BY run_id,source',{'run':run_id})

    def source(self,run_id,source):
        rows=self.batch('SELECT * FROM catalog_sources WHERE run_id=%(run)s AND source=%(source)s',{'run':run_id,'source':source})
        return rows[0] if rows else None

    def start(self,run_id,source,owner):
        p=self.params(run_id,source,owner)
        return bool(self.batch([
            "UPDATE catalog_pages SET state='pending' WHERE run_id=%(run)s AND source=%(source)s AND state='processing' AND "+self.allowed(),
            "UPDATE catalog_sources SET state='running',started_at=COALESCE(started_at,%(now)s) WHERE run_id=%(run)s AND source=%(source)s AND state IN ('pending','running') AND "+self.allowed()+" RETURNING source"],p))

    def cancelled(self,run_id,source,owner):
        return not self.batch('SELECT id FROM runs WHERE id=%(run)s AND '+self.allowed(),self.params(run_id,source,owner))

    def add_pages(self,run_id,source,links,owner):
        from .catalog import page_id
        # The external agent's short-connection mode also keeps upload batches
        # small on networks where long transfers are interrupted.
        batch_size = 4 if self.settings and not self.settings.get('reuse_connections',True) else 250
        for offset in range(0,len(links),batch_size):
            p={**self.params(run_id,source,owner),**month_window()}
            values=[]
            for index,(kind,url) in enumerate(links[offset:offset+batch_size]):
                p.update({f'id{index}':page_id(run_id,source,url),f'url{index}':url,f'kind{index}':kind,
                          f'memory{index}':page_key(source,url)})
                values.append(f'(%(id{index})s,%(url{index})s,%(kind{index})s,%(memory{index})s)')
            if values:self.batch(f'''WITH input(id,url,kind,memory_url) AS (VALUES {','.join(values)})
                INSERT INTO catalog_pages(id,run_id,source,url,kind,state,detail,checked_at)
                SELECT i.id,%(run)s,%(source)s,i.url,i.kind,
                    CASE WHEN m.url IS NOT NULL THEN 'cached' ELSE 'pending' END,
                    CASE WHEN m.url IS NOT NULL THEN 'Уже собрано в месяце '||%(month)s ELSE '' END,m.checked_at
                FROM input i LEFT JOIN monthly_page_memory m ON i.kind='product' AND m.source=%(source)s
                    AND m.url=i.memory_url AND m.revision=%(memory_revision)s
                    AND m.checked_at>=%(month_start)s AND m.checked_at<%(month_end)s
                WHERE {self.allowed()} ON CONFLICT(run_id,source,url) DO NOTHING''',p)

    def skip_remembered_pages(self,run_id,source,owner):
        p={**self.params(run_id,source,owner),**month_window()}
        match="""m.source=catalog_pages.source AND m.url=catalog_pages.url AND m.revision=%(memory_revision)s
            AND m.checked_at>=%(month_start)s AND m.checked_at<%(month_end)s"""
        self.batch(f'''UPDATE catalog_pages SET state='cached',http_status=NULL,
            detail='Уже собрано в месяце '||%(month)s,
            checked_at=(SELECT m.checked_at FROM monthly_page_memory m WHERE {match})
            WHERE run_id=%(run)s AND source=%(source)s AND kind='product' AND state='pending'
            AND EXISTS(SELECT 1 FROM monthly_page_memory m WHERE {match}) AND {self.allowed()}''',p)

    def claim(self,run_id,source,owner,prefer_navigation=False):
        kinds=('sitemap','listing','product') if prefer_navigation else ('product','listing','sitemap')
        # Each candidate uses catalog_queue_url; only three rows need sorting.
        candidates=[];choices=[]
        for priority,kind in enumerate(kinds):
            name=f'candidate_{priority}'
            candidates.append(f"{name} AS (SELECT id FROM catalog_pages WHERE run_id=%(run)s AND source=%(source)s AND state='pending' AND kind='{kind}' ORDER BY url LIMIT 1)")
            choices.append(f'SELECT id,{priority} priority FROM {name}')
        query='WITH '+','.join(candidates)+" UPDATE catalog_pages SET state='processing' WHERE id=(SELECT id FROM ("+' UNION ALL '.join(choices)+') candidates ORDER BY priority LIMIT 1) AND '+self.allowed()+' RETURNING *'
        rows=self.batch(query,self.params(run_id,source,owner))
        return rows[0] if rows else None

    def finish_page(self,page_id,owner,state,detail,http_status):
        rows=self.batch('SELECT run_id,source FROM catalog_pages WHERE id=%(id)s',{'id':page_id})
        if not rows:return
        p=self.params(rows[0]['run_id'],rows[0]['source'],owner)
        p.update(id=page_id,state=state,detail=detail,http=http_status)
        return bool(self.batch('UPDATE catalog_pages SET state=%(state)s,detail=%(detail)s,http_status=%(http)s,checked_at=%(now)s WHERE id=%(id)s AND '+self.allowed()+' RETURNING id',p))

    def block_source(self,run_id,source,owner,detail):
        p=self.params(run_id,source,owner);p['detail']=detail
        self.batch("UPDATE catalog_sources SET state='blocked',detail=%(detail)s,finished_at=%(now)s WHERE run_id=%(run)s AND source=%(source)s AND "+self.allowed(),p)

    def finish_source(self,run_id,source,owner):
        self.batch("""UPDATE catalog_sources SET state=CASE WHEN EXISTS(SELECT 1 FROM catalog_pages p
                WHERE p.run_id=%(run)s AND p.source=%(source)s AND p.state='failed') THEN 'partial' ELSE 'completed' END,
            finished_at=%(now)s WHERE run_id=%(run)s AND source=%(source)s
            AND NOT EXISTS(SELECT 1 FROM catalog_pages p WHERE p.run_id=%(run)s AND p.source=%(source)s AND p.state IN ('pending','processing'))
            AND """+self.allowed(),self.params(run_id,source,owner))

    def record_products(self,run_id,source,page_id,results,owner):
        if source=='sensoren' and self.settings and not self.settings.get('reuse_connections',True):
            from .sensoren_payload import save_catalog
            pages=self.batch('SELECT url FROM catalog_pages WHERE id=%(page)s',{'page':page_id})
            if not pages:return False
            return save_catalog(self.settings,run_id,page_id,results,owner,pages[0]['url'])
        p=self.params(run_id,source,owner);p['page']=page_id
        # A partly completed multi-execution page may need fetching again, but
        # executions already obtained this month must not create history points.
        collected=MonthlyMemory(self).recent_rule_keys([rule for rule,_ in results])
        fresh=[(rule,result) for rule,result in results if rule.key not in collected]
        sql=[]
        for i,(rule,result) in enumerate(fresh):
            prefix=f'p{i}_'
            fingerprint,canonical,details=document(result.details_json)
            values={**asdict(rule),**asdict(result),'key':rule.key,'fingerprint':fingerprint,
                'document':canonical,'ref':json.dumps({'ref':fingerprint}) if fingerprint else '{}',
                'category':details.get('category',''),'attributes_count':len(details.get('attributes',[])),
                'search':f'{rule.source} {rule.manufacturer} {rule.article} {result.title} {details.get("category","")}'.casefold()}
            p.update({prefix+k:v for k,v in values.items()})
            v=lambda key:f'%({prefix}{key})s'
            qid=f'(SELECT id FROM rules WHERE rule_key={v("key")})'
            jid=f'(SELECT id FROM jobs WHERE run_id=%(run)s AND rule_id={qid})'
            sql += [f'''INSERT INTO rules(rule_key,source,manufacturer,article,product_url,url_template,created_at)
                SELECT {v('key')},{v('source')},{v('manufacturer')},{v('article')},{v('product_url')},{v('url_template')},%(now)s
                WHERE {self.allowed()} ON CONFLICT(rule_key) DO NOTHING''',
                f"INSERT INTO jobs(run_id,rule_id,state) SELECT %(run)s,{qid},'processing' WHERE {self.allowed()} ON CONFLICT(run_id,rule_id) DO NOTHING"]
            if fingerprint:
                sql += [f"INSERT INTO product_documents(fingerprint,details_json) SELECT {v('fingerprint')},{v('document')} WHERE {self.allowed()} ON CONFLICT(fingerprint) DO NOTHING",
                    f'''INSERT INTO product_index(rule_id,title,category,search_text,details_hash,attributes_count,updated_at)
                    SELECT {qid},{v('title')},{v('category')},{v('search')},{v('fingerprint')},{v('attributes_count')},{v('checked_at')}
                    WHERE {self.allowed()} ON CONFLICT(rule_id) DO UPDATE SET title=excluded.title,category=excluded.category,
                    search_text=excluded.search_text,details_hash=excluded.details_hash,attributes_count=excluded.attributes_count,updated_at=excluded.updated_at''']
                if source=='teko':
                    from .details import manufacturer_from_details
                    detected=manufacturer_from_details(details)
                    p[prefix+'detected']=detected
                    p[prefix+'scope_state']='confirmed' if detected=='ТЕКО' else ('excluded' if detected else 'unverified')
                    sql.append(f'''INSERT INTO product_scope(rule_id,manufacturer,state,details_hash,checked_at)
                        SELECT {qid},{v('detected')},{v('scope_state')},{v('fingerprint')},{v('checked_at')}
                        WHERE {self.allowed()} ON CONFLICT(rule_id) DO UPDATE SET manufacturer=excluded.manufacturer,
                        state=excluded.state,details_hash=excluded.details_hash,checked_at=excluded.checked_at''')
            fields=['status','url','title','price','currency','availability','price_text','availability_text','detail','checked_at','http_status','response_hash']
            sql += [f'''INSERT INTO observations(job_id,{','.join(fields)},details_json)
                SELECT {jid},{','.join(v(k) for k in fields)},{v('ref')} WHERE {self.allowed()}
                ON CONFLICT(job_id) DO UPDATE SET {','.join(k+'=excluded.'+k for k in fields)},details_json=excluded.details_json
                WHERE observations.status NOT IN ('priced','on_request','no_price')''',
                f"UPDATE jobs SET state='done' WHERE id={jid} AND {self.allowed()}"]
        errors=any(result.status not in ('priced','on_request','no_price') for _,result in results)
        p['page_state']='failed' if errors else ('done' if fresh else 'cached')
        p['page_detail']='Есть исполнения с ошибкой распознавания' if errors else ('Уже собрано в текущем месяце' if not fresh else '')
        if results and not errors:
            page_rows=self.batch('SELECT url FROM catalog_pages WHERE id=%(page)s',p)
            urls={result.url for _,result in results}
            if page_rows:urls.add(page_rows[0]['url'])
            stamp=min(result.checked_at for _,result in results)
            for index,url in enumerate(sorted(urls)):
                statement,values=page_memory_statement(source,url,stamp,f'm{index}_',self.allowed())
                sql.append(statement);p.update(values)
        sql.append('UPDATE catalog_pages SET state=%(page_state)s,detail=%(page_detail)s,http_status=200,checked_at=%(now)s WHERE id=%(page)s AND '+self.allowed()+' RETURNING id')
        return bool(self.batch(sql,p))

    def progress(self,run_id):
        from .scope import VISIBLE
        return self.batch('''SELECT s.*,
            COALESCE(p.pages,0) pages, COALESCE(p.visited,0) visited,
            COALESCE(p.cards,0) cards, COALESCE(p.cards_visited,0) cards_visited,
            COALESCE(p.monthly_skipped,0) monthly_skipped,
            COALESCE(p.failures,0) failures, COALESCE(j.positions,0) positions,
            COALESCE(p.navigation_left,0) navigation_left, p.last_checked,
            h.phase work_phase,h.phase_started,h.activity_at,h.completed_at,h.recoveries,h.url work_url
            FROM catalog_sources s LEFT JOIN collector_health h ON h.source=s.source AND h.run_id=s.run_id
            LEFT JOIN (
                SELECT source,count(*) pages,
                    sum(CASE WHEN state IN ('done','skipped','failed','cached') THEN 1 ELSE 0 END) visited,
                    sum(CASE WHEN kind='product' THEN 1 ELSE 0 END) cards,
                    sum(CASE WHEN kind='product' AND state IN ('done','skipped','failed','cached') THEN 1 ELSE 0 END) cards_visited,
                    sum(CASE WHEN kind='product' AND state='cached' THEN 1 ELSE 0 END) monthly_skipped,
                    sum(CASE WHEN state='failed' THEN 1 ELSE 0 END) failures,
                    sum(CASE WHEN kind!='product' AND state IN ('pending','processing') THEN 1 ELSE 0 END) navigation_left,
                    max(checked_at) last_checked
                FROM catalog_pages WHERE run_id=%(run)s GROUP BY source
            ) p ON p.source=s.source LEFT JOIN (
                SELECT q.source,count(*) positions FROM jobs j JOIN rules q ON q.id=j.rule_id
                WHERE j.run_id=%(run)s AND '''+VISIBLE+''' GROUP BY q.source
            ) j ON j.source=s.source
            WHERE s.run_id=%(run)s ORDER BY s.source''',{'run':run_id})

    def issues(self,run_id,limit=100):
        return self.batch("SELECT source,url,detail,http_status FROM catalog_pages WHERE run_id=%(run)s AND state='failed' ORDER BY source,url LIMIT %(limit)s",{'run':run_id,'limit':limit})

    def resume(self,run_id):
        if self.settings:
            MonthlyMemory(self).require_workers({row['source'] for row in self.sources(run_id)})
        original=self.batch('SELECT created_at FROM runs WHERE id=%(run)s',{'run':run_id})
        if original and original[0]['created_at'][:7]!=month_window()['month']:
            raise ValueError('Этот запуск относится к другому месяцу. Создайте новый сбор для обновления цен и новой точки истории.')
        if self.batch("SELECT id FROM runs WHERE state IN ('queued','running')"):
            raise ValueError('Сначала завершите или остановите активный запуск')
        p={'run':run_id}
        # All statements share one lock and commit. Finished pages/observations stay.
        return self.batch([
            "UPDATE runs SET state='queued',cancel_requested=0,finished_at=NULL WHERE id=%(run)s AND state IN ('cancelled','completed_with_errors') AND EXISTS(SELECT 1 FROM catalog_sources WHERE run_id=%(run)s) AND NOT EXISTS(SELECT 1 FROM runs WHERE state IN ('queued','running')) RETURNING id",
            "UPDATE catalog_pages SET state='pending',detail='' WHERE run_id=%(run)s AND state IN ('processing','failed','cancelled') AND EXISTS(SELECT 1 FROM runs WHERE id=%(run)s AND state='queued')",
            "UPDATE catalog_sources SET state='pending',detail='',finished_at=NULL WHERE run_id=%(run)s AND state!='completed' AND EXISTS(SELECT 1 FROM runs WHERE id=%(run)s AND state='queued')",
            "DELETE FROM source_pauses WHERE run_id=%(run)s AND EXISTS(SELECT 1 FROM runs WHERE id=%(run)s AND state='queued')",
            "SELECT id FROM runs WHERE id=%(run)s AND state='queued'"],p)
