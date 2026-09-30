import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from bs4 import BeautifulSoup

from price_monitor.storage import Store
from price_monitor.models import Rule,Observation,utcnow
from price_monitor.passports import Passports
from price_monitor.product_state import ProductState
from price_monitor.passport_sources import candidates,classify_pdf,exact_model_in_text,revision_date,VERSION
from price_monitor.passport_transport import Download,DocumentClient,LocalFiles,SupabaseFiles
from price_monitor.passport_processing import download_job
from price_monitor.passport_recognition import validate,LocalRecognizer
from price_monitor.passport_review import approve,confirmed
from price_monitor.library import Library
from price_monitor.matching_normalize import normalize_sensor

ROOT=Path(__file__).resolve().parents[1]
URL='https://mega-k.com/products/ps2-36m70-12b11-k'
PDF='https://mega-k.com/userfiles/passports/PS2-36M70-12B11-K.pdf'
ARTICLE='PS2-36M70-12B11-K'


class PassportTests(unittest.TestCase):
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
        self.details={'category':'Индуктивные датчики','attributes':[], 'documents':[{'url':PDF,'name':'Паспорт','kind':'passport'}], 'document_parser_version':VERSION}
        self.obs=Observation('priced',URL,title='Индуктивный датчик '+ARTICLE,price='12',currency='RUB',http_status=200,details_json=json.dumps(self.details))
        self.repo.record_products(self.run,'megak',self.page['id'],[(self.rule,self.obs)],self.owner)
        self.rid=self.repo.batch('SELECT id FROM rules')[0]['id']
        self.docs=Passports(self.repo);self.files=LocalFiles(Path(self.tmp.name)/'files')

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def document(self,extra=''):
        import pymupdf
        with pymupdf.open() as pdf:
            p=pdf.new_page();p.insert_text((40,40),'Technical datasheet\n'+ARTICLE+'\nDimensions 12 mm\n'+extra)
            return pdf.tobytes()

    def job(self):
        self.docs.schedule();return self.docs.claim('download','docs')

    def save(self):
        job=self.job();raw=self.document();fp,key=self.files.put(raw)
        self.docs.register(job,'docs',fp,key,len(raw),1,{'kind':'datasheet','language':'en','applicability':'confirmed','reason':'full model'},PDF,'etag','date')
        self.docs.finish(job['id'],'docs');return fp

    def complete(self):
        self.repo.batch("INSERT INTO catalog_integrity VALUES(%(run)s,'megak','complete','[]',%(now)s,'') ON CONFLICT(run_id,source) DO NOTHING",{'run':self.run,'now':utcnow()})

    def field(self,**kwargs):
        return dict(name='active_length_mm',value=12,unit='mm',page=1,bbox=[.1,.1,.5,.5],evidence='12 on dimension line',model=ARTICLE,datum='tip to active boundary',**kwargs)

    def test_scoped_documents_base_query_and_fake_png(self):
        html=f'''<base href="https://mega-k.com/"><a href="price.pdf">Прайс</a>
          <div id="properties"><div class="properties-item-row">Технический паспорт<a href="userfiles/passports/{ARTICLE}.pdf"></a></div></div>'''
        found=candidates('megak',BeautifulSoup(html,'html.parser'),URL,'МЕГА-К')
        self.assertEqual([r['url'] for r in found],[PDF])
        html='<div class="manufacturer-docs"><a href="/local/ajax/file_download.php?id=481492">Паспорт</a><a href="/test.png">PDF</a><a href="/certificate.pdf">Сертификат</a></div>'
        found=candidates('teko',BeautifulSoup(html,'html.parser'),'https://teko-com.ru/catalog/product/test/','ТЕКО')
        self.assertEqual(len(found),1);self.assertTrue(found[0]['url'].endswith('?id=481492'))

    def test_exact_model_and_series_applicability(self):
        self.assertTrue(exact_model_in_text(ARTICLE,'Header\n'+ARTICLE+'\nTechnical'))
        self.assertFalse(exact_model_in_text(ARTICLE,ARTICLE+'-EX'))
        self.assertEqual(classify_pdf('Technical datasheet PS2 series',ARTICLE)['applicability'],'review')
        self.assertFalse(classify_pdf('Certificate of conformity Technical '+ARTICLE,ARTICLE)['accepted'])

    def test_revision_is_explicit_and_never_download_or_manufacture_date(self):
        self.assertEqual(revision_date('Manufactured 2026-09-30; Downloaded 2026-10-01'),'')
        self.assertEqual(revision_date('Revision date: 2025-11-03; дата редакции: 15.02.2026'),'2026-02-15')

    def test_storage_400_missing_bucket_creates_private_bucket(self):
        storage=SupabaseFiles({'url':'https://example.supabase.co','service_key':'sb_secret_test'})
        self.assertNotIn('Authorization',storage.session.headers)
        storage.session=Mock()
        storage.session.get.return_value=Mock(status_code=400,json=lambda:{'error':'Bucket not found'})
        storage.session.post.return_value=Mock(status_code=200,ok=True,json=lambda:{'public':False})
        storage.ensure()
        self.assertFalse(storage.session.post.call_args.kwargs['json']['public'])
        storage.session.get.return_value=Mock(status_code=200,ok=True,json=lambda:{'public':True})
        with self.assertRaises(ValueError):storage.ensure()

    def test_global_recognition_serializes_different_documents(self):
        self.save()
        first=self.docs.claim('recognize','one')
        self.docs.enqueue(self.rid,'recognize',{'fingerprint':'b'*64})
        self.assertIsNone(self.docs.claim('recognize','two'))
        self.docs.finish(first['id'],'one')
        self.assertIsNotNone(self.docs.claim('recognize','two'))

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

    def test_monthly_schedule_and_input_change_without_duplicate_prices(self):
        self.assertEqual(self.docs.schedule(),1)
        self.assertEqual(self.docs.schedule(),0)
        self.repo.batch("UPDATE passport_products SET scheduled_period='2026-01'")
        self.docs.schedule()
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM passport_jobs')[0]['n'],1)
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM observations')[0]['n'],1)

    def test_leases_restart_and_old_owner_cannot_finish(self):
        job=self.job();self.assertIsNone(self.docs.claim('download','second'))
        self.repo.batch('UPDATE passport_jobs SET lease_until=0')
        new=self.docs.claim('download','second');self.assertEqual(new['id'],job['id'])
        self.docs.finish(job['id'],'docs')
        self.assertEqual(self.repo.batch('SELECT state FROM passport_jobs')[0]['state'],'processing')
        self.docs.finish(new['id'],'second')
        self.assertEqual(self.repo.batch('SELECT state FROM passport_jobs')[0]['state'],'done')

    def test_file_dedup_and_recognition_once(self):
        fp=self.save();raw=self.files.get(fp)
        self.assertEqual(self.files.put(raw)[0],fp)
        self.assertEqual(len(list(self.files.root.glob('*.pdf'))),1)
        self.assertEqual(self.repo.batch("SELECT count(*) n FROM passport_jobs WHERE kind='recognize'")[0]['n'],1)

    def test_download_304_uses_file_and_keeps_revision(self):
        fp=self.save();self.repo.batch("UPDATE passport_products SET checked_at='2026-01-01'")
        jid=self.docs.enqueue(self.rid,'download',{'period':utcnow()[:7]},key='monthly2');job=self.docs.claim('download','docs')
        client=Mock();client.fetch.return_value=Download(PDF,304,b'',{})
        with patch('price_monitor.passport_processing.Resolver.official_candidates',return_value=iter([])):
            download_job(self.docs,self.files,job,'docs',lambda *a,**k:client)
        client.fetch.assert_called_once_with(PDF,'etag','date')
        self.assertEqual(self.docs.product(self.rid)['current_fingerprint'],fp)
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM passport_files')[0]['n'],1)

    def test_changed_file_same_url_creates_revision(self):
        old=self.save();self.repo.batch("UPDATE passport_products SET checked_at='2026-01-01'")
        self.docs.enqueue(self.rid,'download',{'period':utcnow()[:7]},key='monthly3');job=self.docs.claim('download','docs')
        client=Mock();client.fetch.return_value=Download(PDF,200,self.document('Revision B'),{'ETag':'new'})
        with patch('price_monitor.passport_processing.Resolver.official_candidates',return_value=iter([])):
            download_job(self.docs,self.files,job,'docs',lambda *a,**k:client)
        self.assertNotEqual(self.docs.product(self.rid)['current_fingerprint'],old)
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM passport_files')[0]['n'],2)

    def test_fake_pdf_and_no_document_do_not_remove_price(self):
        job=self.job();client=Mock();client.fetch.return_value=Download(PDF,200,b'\x89PNG\r\n',{})
        with patch('price_monitor.passport_processing.Resolver.official_candidates',return_value=iter([])):
            download_job(self.docs,self.files,job,'docs',lambda *a,**k:client)
        self.assertEqual(self.docs.product(self.rid)['document_state'],'unavailable')
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM passport_files')[0]['n'],0)
        self.assertEqual(self.repo.batch('SELECT price FROM observations')[0]['price'],'12')

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

    def test_invalid_geometry_and_remote_recognition_are_rejected(self):
        for change in ({'bbox':[0,0,2,1]},{'page':2},{'datum':''},{'value':float('nan')}):
            with self.assertRaises(ValueError):validate({'fields':[{**self.field(),**change}],'notes':''},1)
        with self.assertRaises(ValueError):LocalRecognizer('https://external.example')
        with self.assertRaises(ValueError):LocalRecognizer(model='cloud-model')

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


if __name__=='__main__':unittest.main()
