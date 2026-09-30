"""Real PostgreSQL queue/claim/approval checks in a disposable test schema."""
import json
import os
import unittest
import test_sensoren_payload_pg as pg_fixture
from price_monitor.catalog_storage import CatalogRepository
from price_monitor.passports import Passports
from price_monitor.passport_review import approve,confirmed


@unittest.skipUnless(os.getenv('SKB_TEST_POSTGRES_URL') or os.getenv('PRICE_TEST_SECRETS'),'PostgreSQL integration settings absent')
class PassportPostgresTests(unittest.TestCase):
    sql=classmethod(pg_fixture.SensorenPersistenceTests.sql.__func__)
    setUpClass=classmethod(pg_fixture.SensorenPersistenceTests.setUpClass.__func__)
    cleanup_schema=classmethod(pg_fixture.SensorenPersistenceTests.cleanup_schema.__func__)

    def test_catalog_queue_lease_registration_and_review(self):
        self.sql("""INSERT INTO rules(rule_key,source,manufacturer,article,product_url,url_template,created_at)
            VALUES('test','sensoren','ifm','IG0001','https://sensoren.ru/product/ig0001/','','2026-09-30')""")
        rid=self.sql('SELECT id FROM rules')[0]['id']
        repo=CatalogRepository();repo.batch=lambda statements,params=None:self.sql(';'.join(statements) if isinstance(statements,list) else statements,params)
        docs=Passports(repo)
        repo.batch('INSERT INTO passport_products(rule_id) VALUES(%(id)s)',{'id':rid})
        docs.enqueue(rid,'download',{})
        job=docs.claim('download','one')
        self.assertIsNone(docs.claim('download','two'))
        self.assertTrue(docs.renew(job['id'],'one'))
        fp='a'*64
        self.assertTrue(docs.register(job,'one',fp,fp+'.pdf',12,1,
            {'kind':'passport','language':'en','applicability':'review','reason':'series'},'https://media.ifm.com/test.pdf'))
        docs.finish(job['id'],'one')
        field={'name':'active_length_mm','value':12,'unit':'mm','page':1,'bbox':[0,0,1,1],
               'evidence':'drawing','datum':'tip to boundary','model':'IG0001'}
        approve(repo,rid,fp,[field],'Integration test',True)
        self.assertEqual(confirmed(repo,[rid])[rid]['fields'][0]['value'],12)
        self.assertEqual(docs.product(rid)['current_fingerprint'],fp)
