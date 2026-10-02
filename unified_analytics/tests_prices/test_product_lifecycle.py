import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from price_monitor.storage import Store
from price_monitor.models import Rule,Observation,utcnow
from price_monitor.product_state import ProductState
from price_monitor.passport_fields import validate
from price_monitor.passport_review import approve,confirmed
from price_monitor.library import Library
from price_monitor.matching_normalize import normalize_sensor

ROOT=Path(__file__).resolve().parents[1]
URL='https://mega-k.com/products/ps2-36m70-12b11-k'
PDF='https://mega-k.com/userfiles/passports/PS2-36M70-12B11-K.pdf'
ARTICLE='PS2-36M70-12B11-K'



class ProductLifecycleTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'data').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'data')
        self.store=Store(Path(self.tmp.name)/'db');self.repo=self.store.catalog
        self.run=self.store.enqueue_catalog({'megak':['МЕГА-К']})
        self.owner='router2-test';self.store.acquire(self.owner);self.store.claim_run(self.owner)
        self.repo.start(self.run,'megak',self.owner)
        self.repo.add_pages(self.run,'megak',[('product',URL)],self.owner)
        self.page=self.repo.claim(self.run,'megak',self.owner)
        self.rule=Rule('megak','МЕГА-К',ARTICLE,URL)
        self.details={'category':'Индуктивные датчики','attributes':[]}
        self.obs=Observation('priced',URL,title='Индуктивный датчик '+ARTICLE,price='12',currency='RUB',http_status=200,details_json=json.dumps(self.details))
        self.repo.record_products(self.run,'megak',self.page['id'],[(self.rule,self.obs)],self.owner)
        self.rid=self.repo.batch('SELECT id FROM rules')[0]['id']


    def tearDown(self):self.store.close();self.tmp.cleanup()

    def complete(self):
        self.repo.batch("INSERT INTO catalog_integrity VALUES(%(run)s,'megak','complete','[]',%(now)s,'') ON CONFLICT(run_id,source) DO NOTHING",{'run':self.run,'now':utcnow()})

    def field(self,**kwargs):
        return dict(name='active_length_mm',value=12,unit='mm',page=1,bbox=[.1,.1,.5,.5],evidence='12 on dimension line',model=ARTICLE,datum='tip to active boundary',**kwargs)

    def test_new_url_must_be_verified_before_merging(self):
        url='https://mega-k.com/products/moved'
        rule=Rule('megak','МЕГА-К',ARTICLE,url)
        obs=Observation('priced',url,http_status=200)
        client=Mock();client.fetch_document.return_value=(URL,200,'old page')
        self.assertEqual(ProductState(self.repo).relocated([(rule,obs)],url,client)[0][0].url,url)
        client.fetch_document.return_value=(url,200,'redirected')
        self.assertEqual(ProductState(self.repo).relocated([(rule,obs)],url,client)[0][0].key,self.rule.key)

    def test_partial_variant_table_cannot_confirm_disappearance(self):
        rule=Rule('beskonta','BESKONTA','A','https://beskonta.ru/product/a/')
        obs=Observation('priced',rule.url,http_status=200,details_json='{}')
        self.assertFalse(ProductState.complete_variants([(rule,obs)]))
        obs.details_json=json.dumps({'variant_set_complete':True})
        self.assertTrue(ProductState.complete_variants([(rule,obs)]))

    def test_review_is_per_execution_current_version_only_and_not_historical(self):
        fp=self.save();approve(self.repo,self.rid,fp,[self.field()],'Engineer',True)
        rows=Library(self.repo).products()[1]
        current=Library(self.repo).with_specifications(rows)[0]
        self.assertEqual(normalize_sensor(current).values['active_length_mm'],12)
        old=Library(self.repo).with_specifications([{**rows[0],'details_json':self.obs.details_json}])[0]
        self.assertNotIn('_confirmed_geometry',old)
        self.repo.batch("UPDATE passport_products SET current_fingerprint='other'")
        self.assertEqual(confirmed(self.repo,[self.rid]),{})
        with self.assertRaises(ValueError):approve(self.repo,self.rid,fp,[self.field()],'Engineer')

    def test_review_applicability_required_and_dimensions_not_interchanged(self):
        fp=self.save();self.repo.batch("UPDATE passport_links SET applicability='review'")
        with self.assertRaises(ValueError):approve(self.repo,self.rid,fp,[self.field()],'Engineer')
        field=self.field();other={**field,'name':'immersion_length_mm','value':40,'datum':'shoulder to tip'}
        approve(self.repo,self.rid,fp,[field,other],'Engineer',True)
        self.assertEqual([v['value'] for v in confirmed(self.repo,[self.rid])[self.rid]['fields']],[12,40])

    def test_invalid_manual_geometry_is_rejected(self):
        for change in ({'bbox':[0,0,2,1]},{'page':2},{'datum':''},{'value':float('nan')}):
            with self.assertRaises(ValueError):validate({'fields':[{**self.field(),**change}],'notes':''},1)

    def test_incomplete_manifest_and_errors_never_archive(self):
        state=ProductState(self.repo)
        state.verify(self.run,'megak',URL,404,[],self.owner)
        self.assertEqual(self.repo.batch('SELECT missing_count FROM product_lifecycle')[0]['missing_count'],0)
        self.complete()
        for status in (403,429,500):state.verify(self.run,'megak',URL,status,[],self.owner)
        self.assertNotEqual(self.repo.batch('SELECT state FROM product_lifecycle')[0]['state'],'archived')

    def test_two_spaced_404_and_return_preserve_history(self):
        self.complete();state=ProductState(self.repo)
        state.verify(self.run,'megak',URL,404,[],self.owner)
        state.verify(self.run,'megak',URL,404,[],self.owner)
        self.assertEqual(self.repo.batch('SELECT missing_count FROM product_lifecycle')[0]['missing_count'],1)
        self.repo.batch("UPDATE product_lifecycle SET last_missing_check='2026-01-01'")
        state.verify(self.run,'megak',URL,410,[],self.owner)
        self.assertEqual(self.repo.batch('SELECT state FROM product_lifecycle')[0]['state'],'archived')
        state.verify(self.run,'megak',URL,200,[(self.rule,self.obs)],self.owner)
        self.assertEqual(self.repo.batch('SELECT state FROM product_lifecycle')[0]['state'],'active')
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM observations')[0]['n'],1)

    def test_redirect_preserves_rule_price_history_and_alias(self):
        new='https://mega-k.com/products/revised-location'
        rule=Rule('megak','МЕГА-К',ARTICLE,new)
        obs=Observation('priced',new,http_status=200,price='13',details_json=json.dumps(self.details))
        self.repo.record_products(self.run,'megak',self.page['id'],[(rule,obs)],self.owner)
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM rules')[0]['n'],1)
        self.assertEqual(self.repo.batch('SELECT current_url FROM product_lifecycle')[0]['current_url'],new)
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM observations')[0]['n'],1)

    def save(self):
        fp='a'*64
        self.repo.batch([
            "INSERT INTO passport_products(rule_id,current_fingerprint) VALUES(%(id)s,%(fp)s)",
            "INSERT INTO passport_files(fingerprint,object_key,byte_size,page_count,kind,language,created_at) VALUES(%(fp)s,'archive.pdf',12,1,'datasheet','en',%(now)s)",
            "INSERT INTO passport_links VALUES(%(id)s,%(fp)s,%(url)s,'confirmed','historical evidence',%(now)s)"],
            {'id':self.rid,'fp':fp,'now':utcnow(),'url':PDF})
        return fp
