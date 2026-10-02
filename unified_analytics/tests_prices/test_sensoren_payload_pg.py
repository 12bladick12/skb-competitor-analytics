"""Real PostgreSQL checks in a disposable, isolated schema (also run in CI)."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
import unittest
import uuid

from price_monitor.models import Rule,Observation,utcnow
from price_monitor.sensoren_payload import SCHEMA,product_payload,utf8_parts


class PayloadEncodingTests(unittest.TestCase):
    def test_utf8_chunks_are_lossless_and_bounded(self):
        text=('Русский текст 😀 \\ " % '*500)
        pieces=list(utf8_parts(text))
        self.assertEqual(''.join(pieces),text)
        self.assertTrue(all(len(part.encode())<=2200 for part in pieces))


@unittest.skipUnless(os.getenv('SKB_TEST_POSTGRES_URL') or os.getenv('PRICE_TEST_SECRETS'), 'PostgreSQL integration settings absent')
class SensorenPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from price_monitor.postgres import schema_sql
        from price_monitor.storage import SCHEMA as APP_SCHEMA
        cls.schema='price_io_test_'+uuid.uuid4().hex[:12]
        cls.settings=None
        if os.getenv('PRICE_TEST_SECRETS'):
            from price_monitor.sensoren_local import settings_from_file
            cls.settings=settings_from_file(Path(os.environ['PRICE_TEST_SECRETS']))
            from price_monitor.db_settings import connection_settings
            cls.settings=connection_settings(cls.settings)
        cls.addClassCleanup(cls.cleanup_schema)
        # Only fixture DDL is retried; test mutations retain their real failures.
        from price_monitor.db_connection import DatabaseIOTimeout
        statements=['CREATE SCHEMA IF NOT EXISTS '+cls.schema]
        statements += [s for s in schema_sql(APP_SCHEMA).split(';') if s.strip()]
        statements += [s.replace('price_monitor,pg_temp',cls.schema+',pg_temp') for s in SCHEMA]
        for statement in statements:
            for attempt in range(3):
                try:cls.sql(statement);break
                except DatabaseIOTimeout:
                    if attempt==2:raise

    @classmethod
    def sql(cls,query,params=None):
        import psycopg
        from psycopg.rows import dict_row
        from price_monitor.db_connection import DeadlineConnection
        kwargs=dict(autocommit=True,prepare_threshold=None,connect_timeout=10,
                    cursor_factory=psycopg.ClientCursor,row_factory=dict_row)
        if cls.settings:c=DeadlineConnection.connect(**cls.settings,**kwargs)
        else:c=DeadlineConnection.connect(os.environ['SKB_TEST_POSTGRES_URL'],**kwargs)
        with c:
            c.io_timeout=15
            cur=c.execute('BEGIN; SET LOCAL search_path='+cls.schema+",public; SET LOCAL statement_timeout='10s'; SET LOCAL client_min_messages='warning'; "+query+'; COMMIT',params)
            result=[]
            while True:
                if cur.description:result=cur.fetchall()
                if not cur.nextset():break
            return result

    @classmethod
    def cleanup_schema(cls):
        if not re.fullmatch(r'price_io_test_[a-f0-9]{12}',cls.schema):raise ValueError('Unsafe test schema')
        cls.sql('DROP SCHEMA IF EXISTS '+cls.schema+' CASCADE')

    def setUp(self):
        self.sql('TRUNCATE rules,runs,external_sources,monthly_page_memory,product_documents,sensoren_payloads CASCADE')
        self.now=utcnow();self.owner='test-owner'
        self.rule=Rule('sensoren','LANBAO','TEST-1','https://sensoren.ru/product/test_lanbao_1/')
        self.page='a'*64
        self.sql("INSERT INTO runs(id,state,created_at) VALUES(1,'running',%(now)s); "
                 "INSERT INTO external_sources VALUES('sensoren',1,%(owner)s,%(heartbeat)s,2); "
                 "INSERT INTO catalog_sources(run_id,source,brands_json) VALUES(1,'sensoren','[\"LANBAO\"]'); "
                 "INSERT INTO catalog_pages(id,run_id,source,url,kind,state) VALUES(%(page)s,1,'sensoren',%(url)s,'product','processing')",
                 {'now':self.now,'owner':self.owner,'heartbeat':time.time(),'page':self.page,'url':self.rule.url})

    def payload(self,description='description'):
        details=json.dumps({'description':description,'category':'Sensors','attributes':[{'name':'Voltage','value':'24 V'}]},ensure_ascii=False)
        obs=Observation('priced',self.rule.url,title='TEST-1',price='12.50',currency='RUB',http_status=200,checked_at=self.now,details_json=details)
        return {'mode':'catalog','run':1,'page':self.page,'owner':self.owner,'now':self.now,
                'urls':[self.rule.url],'products':[product_payload(self.rule,obs)]}

    def stage(self,data,omit_last=False):
        key=str(uuid.uuid4());text=json.dumps(data,ensure_ascii=False,separators=(',',':'))
        parts=list(utf8_parts(text))
        self.sql('INSERT INTO sensoren_payloads(id,checksum,parts) VALUES(%(id)s,%(checksum)s,%(count)s)',
                 {'id':key,'checksum':hashlib.md5(text.encode()).hexdigest(),'count':len(parts)})
        for n,part in enumerate(parts[:-1] if omit_last else parts):
            self.sql('INSERT INTO sensoren_payload_parts VALUES(%(id)s,%(n)s,%(body)s)',{'id':key,'n':n,'body':part})
        return key

    def apply(self,key):return self.sql('SELECT sensoren_apply_payload(%(id)s) accepted',{'id':key})[0]['accepted']

    def test_large_snapshot_and_lost_ack_replay(self):
        key=self.stage(self.payload('Текст 😀 '*2500))
        self.assertTrue(self.apply(key));self.assertTrue(self.apply(key))
        self.assertEqual(self.sql('SELECT count(*) n FROM observations')[0]['n'],1)
        self.assertGreater(self.sql('SELECT octet_length(details_json) n FROM product_documents')[0]['n'],20000)
        self.assertEqual(self.sql('SELECT price,checked_at FROM observations')[0],{'price':'12.50','checked_at':self.now})
        self.assertEqual(self.sql('SELECT attributes_count FROM product_index')[0]['attributes_count'],1)
        self.assertEqual(self.sql('SELECT state FROM product_lifecycle')[0]['state'],'active')

    def test_same_month_does_not_add_observation(self):
        self.assertTrue(self.apply(self.stage(self.payload())))
        payload=self.payload();payload['products'][0]['observation']['price']='99.00'
        self.assertTrue(self.apply(self.stage(payload)))
        self.assertEqual(self.sql('SELECT count(*) n,max(price) price FROM observations')[0],{'n':1,'price':'12.50'})

    def test_next_month_keeps_previous_history(self):
        self.assertTrue(self.apply(self.stage(self.payload())))
        self.sql("UPDATE observations SET checked_at=to_char((now() AT TIME ZONE 'UTC')-interval '1 month','YYYY-MM-DD')||'T12:00:00+00:00'")
        page='b'*64
        self.sql("UPDATE runs SET state='completed' WHERE id=1; INSERT INTO runs(id,state,created_at) VALUES(2,'running',%(now)s); "
                 "INSERT INTO catalog_sources(run_id,source,brands_json) VALUES(2,'sensoren','[\"LANBAO\"]'); "
                 "INSERT INTO catalog_pages(id,run_id,source,url,kind,state) VALUES(%(page)s,2,'sensoren',%(url)s,'product','processing')",
                 {'now':self.now,'page':page,'url':self.rule.url})
        payload=self.payload();payload.update(run=2,page=page)
        payload['products'][0]['observation']['price']='99.00'
        self.assertTrue(self.apply(self.stage(payload)))
        self.assertEqual(self.sql('SELECT price FROM observations ORDER BY id'),[{'price':'12.50'},{'price':'99.00'}])

    def test_explicit_job_is_saved_with_specifications(self):
        self.sql("INSERT INTO rules(rule_key,source,manufacturer,article,product_url,url_template,created_at) "
                 "VALUES(%(key)s,'sensoren','LANBAO','TEST-1',%(url)s,'',%(now)s)",
                 {'key':self.rule.key,'url':self.rule.url,'now':self.now})
        job=self.sql("INSERT INTO jobs(run_id,rule_id,state) SELECT 1,id,'processing' FROM rules RETURNING id")[0]['id']
        payload=self.payload();payload.update(mode='job',job=job,product=payload['products'][0])
        self.assertTrue(self.apply(self.stage(payload)))
        self.assertEqual(self.sql('SELECT price FROM observations')[0]['price'],'12.50')
        self.assertEqual(self.sql('SELECT attributes_count FROM product_index')[0]['attributes_count'],1)

    def test_expired_lease_cannot_save(self):
        self.sql('UPDATE external_sources SET heartbeat=0')
        self.assertFalse(self.apply(self.stage(self.payload())))
        self.assertEqual(self.sql('SELECT count(*) n FROM observations')[0]['n'],0)

    def test_wrong_owner_and_cancel_do_not_write(self):
        payload=self.payload();payload['owner']='wrong'
        self.assertFalse(self.apply(self.stage(payload)))
        self.sql('UPDATE runs SET cancel_requested=1 WHERE id=1')
        self.assertFalse(self.apply(self.stage(self.payload())))
        self.assertEqual(self.sql('SELECT count(*) n FROM observations')[0]['n'],0)

    def test_unselected_brand_rolls_back(self):
        import psycopg
        payload=self.payload();payload['products'][0]['rule']['manufacturer']='Balluff'
        with self.assertRaises(psycopg.DatabaseError):self.apply(self.stage(payload))
        self.assertEqual(self.sql('SELECT count(*) n FROM rules')[0]['n'],0)

    def test_incomplete_upload_cannot_commit(self):
        import psycopg
        with self.assertRaises(psycopg.DatabaseError):self.apply(self.stage(self.payload(),omit_last=True))
        self.assertEqual(self.sql('SELECT count(*) n FROM observations')[0]['n'],0)

    def test_bad_observation_rolls_back_rule_and_job(self):
        import psycopg
        payload=self.payload();payload['products'][0]['observation']['http_status']='invalid'
        with self.assertRaises(psycopg.DatabaseError):self.apply(self.stage(payload))
        self.assertEqual(self.sql('SELECT count(*) n FROM rules')[0]['n'],0)
        self.assertEqual(self.sql('SELECT state FROM catalog_pages')[0]['state'],'processing')

    def test_sql_shaped_text_is_only_data(self):
        text="'); DROP TABLE rules; --"
        self.assertTrue(self.apply(self.stage(self.payload(text))))
        self.assertEqual(self.sql('SELECT count(*) n FROM rules')[0]['n'],1)
        self.assertEqual(self.sql("SELECT details_json::jsonb->>'description' value FROM product_documents")[0]['value'],text)

    def test_functions_do_not_elevate_or_allow_public_execution(self):
        row=self.sql("SELECT count(*) n FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname=%(schema)s AND (p.prosecdef OR EXISTS(SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE'))",{'schema':self.schema})
        self.assertEqual(row[0]['n'],0)


if __name__=='__main__':unittest.main()
