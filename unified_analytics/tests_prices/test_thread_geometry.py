import unittest

from price_monitor.enrichment import characteristics
from price_monitor.housing_references import housing_reference
from price_monitor.matching import evaluate
from price_monitor.matching_normalize import normalize_sensor
from price_monitor.notations import decode, field_source


def sensor(brand, model, **extra):
    return normalize_sensor(dict(manufacturer=brand,article=model,category='Индуктивные датчики',**extra))


class ThreadGeometryTests(unittest.TestCase):
    def test_lanbao_connector_is_not_housing_thread(self):
        for code,length in [('LR08BN02DNO-E2',68.),('LR08BF15DPO-E2',65.),
                            ('LR08BN02DNO-E1',52.),('LR08BF15DNO-E1',49.)]:
            with self.subTest(code=code):
                s=sensor('LANBAO',code)
                self.assertEqual((s.values['diameter'],s.values['pitch'],s.values['length']),(8.,1.,length))
                self.assertIn('-E21.pdf' if code.endswith('E2') else '-E11.pdf',s.raw['pitch'][0]['source_url'])
        self.assertEqual(sensor('LANBAO','LR08BN02DNO-E2').values['connector'],'m12')

    def test_screenshot_pair_is_mechanically_incompatible(self):
        reference=sensor('LANBAO','LR08BN02DNO-E2')
        own=sensor('LANBAO','LR08BN02DNO-E2')
        own.values['pitch']=.5
        result=evaluate(reference,own)
        self.assertEqual(result.status,'incompatible')
        self.assertEqual(next(x for x in result.fields if x['key']=='pitch')['outcome'],'incompatible')

    def test_lanbao_series_and_lengths_are_scoped(self):
        for code,pitch,length in [('LR12XBF02DPO',1.,51.),('LR12XBN04DPO',1.,55.),
                                 ('LR12XBN04DPO-E2',1.,67.),('LR18XBN08DPO-E2',1.,71.),
                                 ('LR30XBN15DPR-E2',1.5,75.)]:
            s=sensor('LANBAO',code)
            self.assertEqual((s.values['pitch'],s.values['length']),(pitch,length),code)
            self.assertIn('Таблица корпуса',field_source(s,'pitch'))
        # Overview rounds 51.5 to 52; do not introduce an ambiguous cable length.
        self.assertNotIn('length',sensor('LANBAO','LR18XBF05DPO').values)

    def test_extended_mounting_and_source_stay_distinct(self):
        s=sensor('LANBAO','LR18XBF08DPOY-E2')
        self.assertEqual((s.values['pitch'],s.values['mount']),(1.,'quasi-flush'))
        self.assertTrue(s.decoding['source_url'].endswith('LR18XB-Y-DC-34-E2.pdf'))
        self.assertIn('flextronicbg.com',s.decoding['fields']['pitch']['source_url'])
        self.assertTrue(s.decoding['requires_review'])

    def test_unlisted_lanbao_bodies_do_not_inherit_geometry(self):
        for code in ('LR18XBF05DPO-9999','LR18XGBF05DPO','LR18XCF05DPO',
                     'LR18XBF05DPOW1-E2','LR18XBF05DPOB-E2','LR18XBF99DPO',
                     'LR18XBF05DPO-E3','LR18XBF05DLO-E2-EX','LR30XBF15DPRY-E2'):
            self.assertIsNone(housing_reference('LANBAO',code),code)

    def test_lanbao_two_wire_pitch_does_not_copy_three_wire_length(self):
        for code in ('LR12XBF02DLC-E2','LR18XBN08ATO','LR30XBN22ATCY-E2'):
            s=sensor('LANBAO',code)
            self.assertIn('pitch',s.values)
            self.assertNotIn('length',s.values)

    def test_autonics_electrical_families_are_separate(self):
        for code,pitch in [('PR12-2AC',1.),('PRT08-1.5DO',1.),('PRCML30-15AO',1.5)]:
            fields=decode('Autonics',code)['fields']
            self.assertEqual(fields['pitch']['value'],pitch)
            self.assertNotIn('output',fields)
            self.assertNotIn('vmin',fields)
        for code in ('PRL12-2AC','PRCM T08-1.5XO','PR18-5AC-EX','PR18-10AC','PRAT08-1.5DO'):
            self.assertIsNone(housing_reference('Autonics',code.replace(' ','')),code)

    def test_exact_pepperl_models_only(self):
        self.assertEqual(sensor('Pepperl+Fuchs','NBB0,8-5GM25-E0-V3').values['pitch'],.5)
        self.assertEqual(sensor('Pepperl+Fuchs','NBB0.8-5GM25-E0-V3').values['pitch'],.5)
        self.assertEqual(sensor('Pepperl+Fuchs','NCB15-30GM50-Z4-V1').values['pitch'],1.5)
        self.assertNotIn('pitch',sensor('Pepperl+Fuchs','NBB0,8-5GM25-E0-V3-UNKNOWN').values)
        self.assertIsNone(housing_reference('SICK','NBB0,8-5GM25-E0-V3'))

    def test_exact_order_ids_and_aliases(self):
        for brand,code,pitch in [('Balluff','BES M08ME1-USC20B-S04G',1.),('ifm','IFC204',1.),
                                 ('SICK','IM04-01BPSVU2K',.5),('SICK','6058031',.5)]:
            self.assertEqual(sensor(brand,code).values['pitch'],pitch)
        self.assertNotIn('pitch',sensor('ifm','IFC205').values)
        special=sensor('ТЕКО','Датчик контроля минимальной скорости IV0B AC41B-49P-6-LZS4')
        self.assertEqual((special.values['pitch'],special.values['length']),(1.,84.))
        self.assertIn('speed',special.special)
        self.assertTrue(special.decoding['requires_review'])

    def test_unknown_and_connector_only_threads_stay_unknown(self):
        for props in ({'Размер корпуса':'M8'}, {'Размер корпуса':'M18x70'},
                      {'Резьба разъема':'M12x1'}, {'Шаг резьбы, мм':'0'}, {'Шаг резьбы, мм':'-1'}):
            self.assertNotIn('pitch',normalize_sensor({'props':props}).values,props)

    def test_explicit_housing_formats(self):
        for name,value in [('Thread size','M4 x 0.5'),('Обозначение резьбы','М4х0,5'),
                           ('Размер корпуса','M4*0.5*30')]:
            s=normalize_sensor({'props':{name:value}})
            self.assertEqual((s.values['diameter'],s.values['pitch']),(4.,.5))

    def test_multiple_housing_threads_are_not_reduced_to_first(self):
        for value in ('M16x1/M20x1.5','M16/M20','M8x0.5 / M8x1'):
            s=normalize_sensor({'props':{'Размер резьбы корпуса':value}})
            self.assertNotIn('pitch',s.values)
            self.assertIn('pitch',s.conflicts)

    def test_card_conflict_is_kept_and_prevents_direct_match(self):
        s=sensor('LANBAO','LR12XBF02DPO',props={'Обозначение резьбы':'M12x0.5'})
        self.assertEqual(s.values['pitch'],.5)
        self.assertEqual(s.decoding['fields']['pitch']['application'],'conflict')
        self.assertTrue(s.decoding['requires_review'])

    def megak_record(self,description='Индуктивный бесконтактный датчик в цилиндрическом латунном корпусе с резьбой М8х1 длиной 33 мм.'):
        return dict(manufacturer='МЕГА-К',article='PS2-08M33-2B11-K',category='Индуктивные датчики',
                    product_url='https://mega-k.com/products/ps2-08m33-2b11-k',_specifications={'description':description})

    def test_megak_explicit_description_has_its_own_evidence(self):
        record=self.megak_record()
        s=normalize_sensor(record)
        self.assertEqual(s.values['pitch'],1.)
        self.assertIn('Описание корпуса',field_source(s,'pitch'))
        payload=characteristics(record)
        entry=next(x for x in payload['attributes'] if x['key']=='pitch')
        self.assertEqual(entry['source_url'],record['product_url'])
        self.assertIn('М8х1',entry['evidence'])

    def test_megak_does_not_use_other_products_or_connector_thread(self):
        for description in ('Похожий датчик в корпусе с резьбой М8х1.',
                            'Индуктивный бесконтактный датчик. Разъём М12х1.',
                            'Индуктивный бесконтактный датчик в цилиндрическом корпусе с резьбой М12х1 длиной 33 мм.'):
            self.assertNotIn('pitch',normalize_sensor(self.megak_record(description)).values)
        record=self.megak_record();record['product_url']='https://example.com/similar'
        self.assertNotIn('pitch',normalize_sensor(record).values)

    def test_catalog_geometry_survives_durable_enrichment(self):
        payload=characteristics(dict(manufacturer='LANBAO',article='LR12XBF02DPO',category='Индуктивные датчики'))
        entry=next(x for x in payload['attributes'] if x['key']=='pitch')
        self.assertIn('#page=9',entry['source_url'])
        self.assertIn('стр.',entry['evidence'])


if __name__ == '__main__':unittest.main()
