import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

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
        self.assertEqual(len(queries),2)

    def test_reuses_current_document_and_matches_export(self):
        self.repo.batch("UPDATE observations SET details_json='{\"ref\":\"current\"}' WHERE id=1")
        library=Library(self.repo)
        current=library.comparison_products()[0]
        exported=library.export_products()[0]
        for field in ('rule_id','article','last_price','price_checked_at','_price_snapshot_terms','_specifications'):
            self.assertEqual(current[field],exported[field])


if __name__=='__main__':unittest.main()
