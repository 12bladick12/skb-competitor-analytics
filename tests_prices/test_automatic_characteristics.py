import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from price_monitor.storage import Store
from price_monitor.catalog_schema import document
from price_monitor.enrichment import Enrichment,prepare,current_version
from price_monitor.library import Library
from price_monitor.product_exports import export_tables
from price_monitor.matching_normalize import normalize_sensor


class AutomaticCharacteristicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'test.db');self.repo=self.store.catalog
        self.seed()

    def seed(self):
        self.raw={'category':'Индуктивные датчики','attributes':[
            {'name':'Монтаж','value':'Незаподлицо'},{'name':'Расстояние срабатывания, мм','value':'2'},
            {'name':'Напряжение питания, В','value':'12-24 DC'}]}
        self.fp,self.canonical,_=document(json.dumps(self.raw))
        self.repo.batch(["INSERT INTO rules VALUES(1,'key','sensoren','LANBAO','LR08BN02DPC','https://sensoren.ru/product/a/','','2026-09-29')",
            "INSERT INTO product_documents VALUES(%(fp)s,%(raw)s)",
            "INSERT INTO product_index VALUES(1,'LR08BN02DPC','Индуктивные датчики','',%(fp)s,3,'2026-09-29')"],{'fp':self.fp,'raw':self.canonical})
        self.service=Enrichment(self.repo);self.lib=Library(self.repo)

    def tearDown(self):self.store.close();self.tmp.cleanup()

    def record(self):
        return dict(rule_id=1,source='sensoren',manufacturer='LANBAO',article='LR08BN02DPC',title='LR08BN02DPC',category='Индуктивные датчики',_specifications=self.raw)

    def test_background_backfills_without_opening_a_product(self):
        self.assertEqual(self.service.process_batch(),1)
        self.assertEqual(self.service.process_batch(),0)
        saved=self.repo.batch('SELECT * FROM product_enrichment')[0]
        values={p['key']:p['typed_value'] for p in json.loads(saved['payload_json'])['attributes']}
        self.assertEqual((values['diameter'],values['pitch'],values['output'],values['function']),(8.,1.,'PNP','NC'))
        self.assertEqual(values['load'],150.)
        self.assertEqual(self.repo.batch('SELECT * FROM product_index')[0]['updated_at'],'2026-09-29')
        self.assertEqual(self.repo.batch('SELECT details_json FROM product_documents')[0]['details_json'],self.canonical)

    def test_current_read_saves_and_reuses_without_rewriting(self):
        rows=self.lib.with_specifications([self.record()]);self.assertGreater(rows[0]['automatic_attributes_count'],0)
        first=self.repo.batch('SELECT checked_at FROM product_enrichment')[0]
        with patch('price_monitor.enrichment.characteristics',side_effect=AssertionError('should reuse persisted fields')):
            self.lib.with_specifications([self.record()])
        self.assertEqual(first,self.repo.batch('SELECT checked_at FROM product_enrichment')[0])

    def test_export_contains_added_fields_and_their_evidence(self):
        rows=self.lib.with_specifications([self.record()]);products,properties=export_tables(rows)
        added=next(p for p in properties if p['Характеристика']=='Функция выхода')
        self.assertEqual(added['Значение'],'NC');self.assertTrue(added['Источник дозаполнения'].startswith('https://'))
        self.assertEqual(products[0]['Автоматически дозаполнено'],rows[0]['automatic_attributes_count'])
        self.assertNotIn('_enrichment',products[0])

    def test_historical_observation_never_updates_current_projection(self):
        self.lib.with_specifications([{**self.record(),'details_json':self.canonical}])
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM product_enrichment')[0]['n'],0)

    def test_stale_computation_cannot_overwrite_new_card(self):
        old=prepare(self.record())
        self.repo.batch("UPDATE product_index SET details_hash='new' WHERE rule_id=1")
        self.service.save_prepared([old])
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM product_enrichment')[0]['n'],0)

    def test_new_card_value_replaces_derived_field_on_next_batch(self):
        self.service.process_batch()
        newer={**self.raw,'attributes':self.raw['attributes']+[{'name':'Схема выхода','value':'NPN'}]}
        fp,canonical,_=document(json.dumps(newer))
        self.repo.batch(['INSERT INTO product_documents VALUES(%(fp)s,%(raw)s)',
                        'UPDATE product_index SET details_hash=%(fp)s WHERE rule_id=1'],{'fp':fp,'raw':canonical})
        self.assertEqual(self.service.process_batch(),1)
        data=json.loads(self.repo.batch('SELECT payload_json FROM product_enrichment')[0]['payload_json'])
        self.assertNotIn('output',{p['key'] for p in data['attributes']});self.assertIn('output',data['conflicts'])

    def test_rule_upgrade_invalidates_persisted_results(self):
        self.service.process_batch()
        with patch('price_monitor.enrichment.VERSION','automatic-new'):
            self.assertEqual(self.service.process_batch(),1)
            self.assertEqual(self.repo.batch('SELECT version FROM product_enrichment')[0]['version'],current_version())

    def test_background_megak_description_keeps_official_source(self):
        url='https://mega-k.com/products/ps2-08m33-2b11-k'
        raw={'category':'Индуктивные датчики','description':
             'Индуктивный бесконтактный датчик в цилиндрическом латунном корпусе с резьбой М8х1 длиной 33 мм.'}
        fp,canonical,_=document(json.dumps(raw))
        self.repo.batch(["UPDATE rules SET source='megak',manufacturer='МЕГА-К',article='PS2-08M33-2B11-K',product_url=%(url)s WHERE id=1",
            "INSERT INTO product_documents VALUES(%(fp)s,%(raw)s)",
            "UPDATE product_index SET title='PS2-08M33-2B11-K',details_hash=%(fp)s WHERE rule_id=1"],
            {'url':url,'fp':fp,'raw':canonical})
        self.assertEqual(self.service.process_batch(),1)
        payload=json.loads(self.repo.batch('SELECT payload_json FROM product_enrichment')[0]['payload_json'])
        pitch=next(p for p in payload['attributes'] if p['key']=='pitch')
        self.assertEqual((pitch['typed_value'],pitch['source_url']),(1.,url))
        self.assertEqual(self.service.process_batch(),0)

    def test_older_worker_cannot_overwrite_higher_revision(self):
        data=prepare(self.record());data['revision']+=1;self.service.save_prepared([data])
        old=prepare(self.record());old['payload']='{}';self.service.save_prepared([old])
        self.assertNotEqual(self.repo.batch('SELECT payload_json FROM product_enrichment')[0]['payload_json'],'{}')

    def test_unknown_models_are_checkpointed_without_inventing_properties(self):
        self.repo.batch("UPDATE rules SET article='LR08BN02DPC-UNKNOWN' WHERE id=1")
        self.assertEqual(self.service.process_batch(),1);self.assertEqual(self.service.process_batch(),0)
        self.assertEqual(self.repo.batch('SELECT filled_count FROM product_enrichment')[0]['filled_count'],0)

    def test_mixed_deployment_cannot_label_old_rules_as_current(self):
        with patch('price_monitor.notations.VERSION','notation-registry-2026-10-01-v2'):
            with self.assertRaises(RuntimeError):self.service.process_batch()
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM product_enrichment')[0]['n'],0)
        with patch('price_monitor.megak_notation.VERSION','megak-ps-vb-2026-09-30-v1'):
            with self.assertRaises(RuntimeError):self.service.process_batch()
        from price_monitor.matching_normalize import Sensor
        stale=Sensor('old','LR08BN02DPC',family='inductive',decoding={'brand':'LANBAO','version':'old'})
        with patch('price_monitor.matching_normalize.normalize_sensor',return_value=stale):
            with self.assertRaises(RuntimeError):self.service.process_batch()
        self.assertEqual(self.repo.batch('SELECT count(*) n FROM product_enrichment')[0]['n'],0)

    def test_correct_function_prevents_matching_no_as_nc(self):
        from price_monitor.matching import evaluate
        reference=normalize_sensor(self.record())
        candidate=normalize_sensor({'manufacturer':'LANBAO','article':'LR08BN02DPO','category':'Индуктивные датчики'})
        self.assertEqual(evaluate(reference,candidate).status,'incompatible')


if __name__=='__main__':unittest.main()
