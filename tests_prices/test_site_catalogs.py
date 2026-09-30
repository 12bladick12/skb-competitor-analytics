import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from bs4 import BeautifulSoup
from price_monitor.catalog import seeds,classify,discover,process_catalog
from price_monitor.catalog_outbox import CatalogOutbox
from price_monitor.details import manufacturer,parse_catalog_product
from price_monitor.storage import Store
from price_monitor.worker import Worker
from price_monitor.transport import FetchError

ROOT=Path(__file__).resolve().parents[1]


class SiteCatalogTests(unittest.TestCase):
    def test_individual_entry_points_and_forbidden_navigation(self):
        self.assertEqual(seeds('beskonta',['BESKONTA'])[0],('sitemap','https://beskonta.ru/sitemap-1.xml'))
        self.assertEqual(seeds('sensor',['СЕНСОР']),[('sitemap','https://sensor-com.ru/sitemap/main.xml')])
        for source,url in [('beskonta','https://beskonta.ru/catalog/all/?p=11'),
                           ('sensor','https://sensor-com.ru/catalog/arhiv-produktsii/'),
                           ('teko','https://teko-com.ru/catalog/datchiki/filter/clear/apply/')]:
            self.assertIsNone(classify(source,url,[]))
        self.assertEqual(classify('teko','https://teko-com.ru/catalog/datchiki/?PAGEN_1=2',[]),'listing')

    def test_sensor_sitemap_keeps_goods_and_discards_news_and_analogs(self):
        xml='<sitemapindex>'+''.join('<sitemap><loc>https://sensor-com.ru/sitemap/'+n+'</loc></sitemap>' for n in ['Goods0.xml','Goods1.xml','category.xml','articles.xml','analogs.xml'])+'</sitemapindex>'
        links=discover('sensor','sitemap',xml,'https://sensor-com.ru/sitemap/main.xml',['СЕНСОР'])
        self.assertEqual([u.rsplit('/',1)[-1] for _,u in links],['Goods0.xml','Goods1.xml','category.xml'])

    @staticmethod
    def teko_page(brand='АО НПК «ТЕКО»',name='SM2',visible=''):
        return '<h1>Магнитная система ТЕКО SM2</h1><div class="catalog-detail"><div class="catalog-detail-price"><div>549 ₽</div></div></div>'+visible+'<script type="application/ld+json">'+json.dumps({'@type':'Product','name':name,'brand':brand},ensure_ascii=False)+'</script>'

    def test_teko_structured_brand_and_price(self):
        page=self.teko_page();url='https://teko-com.ru/catalog/product/sm2-/'
        results,reason=parse_catalog_product('teko',page,url,['ТЕКО'])
        self.assertEqual(reason,'');self.assertEqual(len(results),1)
        self.assertEqual(results[0][0].article,'SM2')
        self.assertEqual(results[0][1].price,'549.00')
        self.assertEqual(json.loads(results[0][1].details_json)['manufacturer'],'ТЕКО')

    def test_teko_structured_recommendation_and_foreign_brand_not_accepted(self):
        self.assertEqual(manufacturer('teko',BeautifulSoup(self.teko_page(name='Other'),'html.parser')),'')
        self.assertEqual(manufacturer('teko',BeautifulSoup(self.teko_page(brand={'name':'Autonics'}),'html.parser')),'Autonics')
        visible='<div class="product-item-detail-properties"><div class="one_prop"><div class="name">Бренд</div><div class="value">SICK</div></div></div>'
        self.assertEqual(manufacturer('teko',BeautifulSoup(self.teko_page(visible=visible),'html.parser')),'SICK')

    def test_robots_denied_is_terminal_skip_and_other_pages_continue(self):
        (ROOT/'data').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as tmp:
            store=Store(Path(tmp)/'db');run=store.enqueue_catalog({'sensor':['СЕНСОР']})
            owner='router2-test';store.acquire(owner);store.claim_run(owner)
            client=Mock();client.fetch_document.side_effect=FetchError('robots_denied','URL запрещён правилами robots.txt')
            process_catalog(store.catalog,run,'sensor',owner,threading.Event(),lambda *a,**k:client)
            self.assertEqual(store.catalog.source(run,'sensor')['state'],'completed')
            self.assertEqual(store.catalog.batch('SELECT state FROM catalog_pages')[0]['state'],'skipped')
            client.fetch_document.assert_called_once();store.close()

    def test_redirect_from_removed_teko_product_is_skipped(self):
        (ROOT/'data').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as tmp:
            store=Store(Path(tmp)/'db');run=store.enqueue_catalog({'teko':['ТЕКО']})
            owner='router2-test';store.acquire(owner);store.claim_run(owner)
            url='https://teko-com.ru/catalog/product/sm1/'
            client=Mock()
            def fetch(value,html_only=False):
                if value==url:return 'https://teko-com.ru/catalog/',200,'<h1>Каталог</h1>'
                return value,200,'<urlset><url><loc>'+url+'</loc></url></urlset>' if value.endswith('.xml') else '<html/>'
            client.fetch_document.side_effect=fetch
            process_catalog(store.catalog,run,'teko',owner,threading.Event(),lambda *a,**k:client)
            row=store.catalog.batch('SELECT state,detail FROM catalog_pages WHERE url=%(url)s',{'url':url})[0]
            self.assertEqual(row['state'],'skipped');self.assertIn('перенаправлена',row['detail'])
            self.assertEqual(store.results(run_id=run),[]);store.close()

    def test_source_failures_are_bounded_isolated_and_sanitized(self):
        store=Mock();worker=Worker(store);worker.shutdown=Mock();worker.shutdown.is_set.return_value=False;worker.shutdown.wait.return_value=False
        with tempfile.TemporaryDirectory(dir=ROOT/'data') as tmp,patch('price_monitor.catalog_outbox.CatalogOutbox',return_value=CatalogOutbox(tmp)),\
             patch('price_monitor.catalog.process_catalog',side_effect=RuntimeError('secret connection string')) as process:
            worker.process_catalog_source(11,'sensor')
        self.assertEqual(process.call_count,3)
        store.catalog.block_source.assert_called_once()
        self.assertNotIn('secret',store.catalog.block_source.call_args.args[-1])
        worker.shutdown.set.assert_not_called()
        self.assertEqual(worker.progress['sensor'].snapshot()['recoveries'],3)


if __name__=='__main__':unittest.main()
