import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock,patch

from price_monitor.catalog_storage import CatalogRepository
from price_monitor.library import Library
from price_monitor.storage import Store


class ComparisonCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo=Store(Path(self.tmp.name)/'prices.db').catalog
        self.seed()

    def seed(self):
        self.repo.batch([
            "INSERT INTO rules(id,rule_key,source,manufacturer,article,product_url,url_template,created_at) VALUES(1,'r1','sensoren','LANBAO','LR18XBF08DPOY-E2','https://sensoren.ru/product/test/','','2026-10-01')",
            "INSERT INTO runs(id,state,created_at) VALUES(1,'completed','2026-09-01'),(2,'completed','2026-10-01')",
            "INSERT INTO jobs(id,run_id,rule_id,state) VALUES(1,1,1,'done'),(2,2,1,'done')",
            "INSERT INTO observations(id,job_id,status,url,title,price,currency,availability,price_text,availability_text,detail,checked_at,response_hash,details_json) VALUES(1,1,'priced','https://sensoren.ru/product/test/','model','100','RUB','','','','','2026-09-01','','{\"ref\":\"old\"}')",
            "INSERT INTO observations(id,job_id,status,url,title,price,currency,availability,price_text,availability_text,detail,checked_at,response_hash,details_json) VALUES(2,2,'network_error','https://sensoren.ru/product/test/','model',NULL,NULL,'','','','','2026-10-01','','{}')",
            "INSERT INTO product_documents(fingerprint,details_json) VALUES('old',%(old)s),('current',%(current)s)",
            "INSERT INTO product_index(rule_id,title,category,search_text,details_hash,attributes_count,updated_at) VALUES(1,'model','Inductive sensor','model','current',1,'2026-10-01')",
        ], {'old':json.dumps({'price_terms':{'basis':'net','rate':20,'unit':'piece'}}),
            'current':json.dumps({'attributes':[{'name':'Output type','value':'PNP'}], 'price_terms':{'basis':'gross','rate':22,'unit':'piece'}})})

    def test_separates_current_characteristics_and_last_quote_without_writes(self):
        original=self.repo.batch
        queries=[]
        def query(statements,params=None):
            self.assertIsInstance(statements,str)
            self.assertTrue(statements.lstrip().startswith('SELECT '))
            queries.append(statements)
            return original(statements,params)
        self.repo.batch=query
        result=Library(self.repo).comparison_products()
        self.assertEqual(len(result),1)
        row=result[0]
        self.assertEqual((row['status'],row['last_price'],row['price_checked_at']),('network_error','100','2026-09-01'))
        self.assertEqual(row['_specifications']['price_terms']['basis'],'gross')
        self.assertEqual(row['_price_snapshot_terms']['basis'],'net')
        self.assertEqual(row['_specifications']['attributes'][0]['value'],'PNP')
        self.assertLessEqual(len(queries),4)

    def test_reuses_current_document_and_matches_export(self):
        self.repo.batch("UPDATE observations SET details_json='{\"ref\":\"current\"}' WHERE id=1")
        library=Library(self.repo)
        current=library.comparison_products()[0]
        exported=library.export_products()[0]
        for field in ('rule_id','article','last_price','price_checked_at','_price_snapshot_terms','_specifications'):
            self.assertEqual(current[field],exported[field])

    def test_reference_restores_original_snapshot_after_card_update(self):
        library=Library(self.repo)
        row=library.comparison_products()[0]
        original=row['_specifications']
        row['_specifications']={}
        self.repo.batch("UPDATE product_index SET details_hash='old' WHERE rule_id=1")
        self.assertEqual(library.comparison_reference(row)['_specifications'],original)

    def test_search_paths_do_not_load_observations_and_quotes_are_selected_separately(self):
        original=self.repo.batch
        calls=[]
        def measured(sql,params=None):
            calls.append(sql)
            return original(sql,params)
        self.repo.batch=measured
        library=Library(self.repo)
        names=list(library.iter_search_identities())
        self.assertEqual([r['article'] for r in names],['LR18XBF08DPOY-E2'])
        self.assertFalse(any('product_documents' in q or 'observations' in q or 'jobs' in q for q in calls))
        calls.clear()
        cards=list(library.iter_search_products())
        self.assertEqual(cards[0]['_specifications']['attributes'][0]['value'],'PNP')
        self.assertFalse(any('observations' in q or 'jobs' in q for q in calls))
        self.assertNotIn('last_price',cards[0])
        quotes=library.comparison_prices([1,1,999])
        self.assertEqual(set(quotes),{1,999})
        self.assertEqual(quotes[1]['status'],'network_error')
        self.assertEqual(quotes[1]['last_price'],'100')
        self.assertEqual(quotes[1]['_price_snapshot_terms']['basis'],'net')
        self.assertNotIn('_specifications',quotes[1])
        self.assertNotIn('_catalog_details_hash',quotes[1])
        self.assertIsNone(quotes[999]['last_price'])

    def test_catalog_yields_first_page_before_reading_the_rest(self):
        def row(identifier):
            return {'rule_id':identifier,'current_details_json':'{}','current_details_hash':'',
                    'last_price_details_json':'{}','geometry_fingerprint':None,'geometry_fields':None,
                    'geometry_reviewer':None,'geometry_updated_at':None}
        repository=Mock()
        pages=[]
        def batch(sql,params=None):
            if 'after' not in (params or {}):return []
            pages.append(params['after'])
            return [row(1),row(2)] if params['after']==0 else [row(3)]
        repository.batch.side_effect=batch
        with patch('price_monitor.library.COMPARISON_PAGE_SIZE',2):
            stream=Library(repository).iter_comparison_products()
            repository.batch.assert_not_called()
            self.assertEqual(next(stream)['rule_id'],1)
            self.assertEqual(pages,[0])
            self.assertEqual(next(stream)['rule_id'],2)
            self.assertEqual(pages,[0])
            self.assertEqual(next(stream)['rule_id'],3)
            self.assertEqual(pages,[0,2])
            self.assertEqual(list(stream),[])

    def test_large_quote_payload_is_not_retained_in_search_rows(self):
        payload=json.dumps({'description':'x'*100000,'price_terms':{'basis':'net','rate':20}})
        self.repo.batch('UPDATE observations SET details_json=%(payload)s WHERE id=1',{'payload':payload})
        row=next(Library(self.repo).iter_comparison_products())
        self.assertNotIn('last_price_details_json',row)
        self.assertNotIn('_price_payload',row)
        self.assertEqual(row['_price_snapshot_terms']['basis'],'net')

    def test_visibility_is_applied_before_paging_without_skipping_later_models(self):
        self.repo.batch([
            "INSERT INTO rules(id,rule_key,source,manufacturer,article,product_url,url_template,created_at) VALUES(2,'r2','teko','ТЕКО','hidden','','','2026-10-02'),(3,'r3','teko','ТЕКО','visible','','','2026-10-02'),(4,'r4','teko','ТЕКО','stale','','','2026-10-02')",
            "INSERT INTO product_index(rule_id,details_hash,updated_at) VALUES(2,'current','2026-10-02'),(3,'current','2026-10-02'),(4,'current','2026-10-02')",
            "INSERT INTO product_scope(rule_id,manufacturer,state,details_hash,checked_at) VALUES(2,'OTHER','confirmed','current','2026-10-02'),(3,'ТЕКО','confirmed','current','2026-10-02'),(4,'ТЕКО','confirmed','old','2026-10-02')",
        ])
        with patch('price_monitor.library.COMPARISON_PAGE_SIZE',1):
            rows=list(Library(self.repo).iter_comparison_products())
        self.assertEqual([row['rule_id'] for row in rows],[1,3])
        self.assertEqual([row['rule_id'] for row in Library(self.repo).iter_search_identities()],[1,3])
        self.assertEqual([row['rule_id'] for row in Library(self.repo).iter_search_products()],[1,3])


if __name__=='__main__':unittest.main()
