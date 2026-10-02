import unittest
from price_monitor.inductive_designations import autonics, beskonta, lanbao, pepperl_fuchs, teko
from price_monitor.notations import decode
from price_monitor.matching_normalize import normalize_sensor


class DesignationTests(unittest.TestCase):
    def test_autonics_standard_and_nonflush(self):
        a=autonics('PR12-2DN')['values'];b=autonics('PR18-8DP2')['values']
        self.assertEqual((a['pitch'],a['output'],a['function'],a['frequency']),(1.,'NPN','NO',1500.))
        self.assertEqual((b['mount'],b['function'],b['frequency']),('non-flush','NC',350.))
        self.assertNotIn('length',a)

    def test_autonics_connector_material_exception(self):
        a=autonics('PRCM08-1.5DP')['values']
        self.assertEqual((a['material'],a['connector'],a['pin_count']),('stainless','m12',4.))
        self.assertEqual(autonics('PRW18-5DP')['values']['connection'],'cable+connector')

    def test_unknown_combinations_never_get_series_defaults(self):
        for code in ('PR18-5DP-EX','PR18-10DP','PRACM08-1.5DP','PRCML08-1.5DP','CR18-5DP'):
            self.assertIsNone(autonics(code),code)
        self.assertIsNone(beskonta('SIS-18N75-NO-PNP-5-ST-Y99'))
        self.assertIsNone(lanbao('LR18XBF05DPO-9999'))

    def test_beskonta_base_length_and_ip_are_not_guessed(self):
        a=beskonta('SIS-18N75-NO-PNP-5-ST')['values']
        self.assertEqual((a['mount'],a['material'],a['pitch']),('non-flush','stainless',1.))
        self.assertNotIn('length',a);self.assertNotIn('ip',a)
        self.assertNotIn('pitch',beskonta('SIS-30V55-NO-PNP-10-TF')['values'])

    def test_beskonta_explicit_modifications_and_connection(self):
        a=beskonta('SIS-18N55-P12-NC-AC-5-TF-IP-C1')['values']
        self.assertEqual((a['output'],a['voltage_type'],a['vmax']),('2-wire','AC',250.))
        self.assertEqual((a['ip'],a['tmin'],a['tmax'],a['connector']),(('68',),-45.,65.,'m12'))
        self.assertIsNone(beskonta('SIS-18N75-NO-PNP-5-ST-C1-H1'))

    def test_beskonta_other_families_cannot_cross(self):
        for code in ('SES-18N75-NO-PNP-5-ST','SEP-12V12-NO-PNP-FP','STR-18N75-NO-PNP-5-ST'):
            self.assertIsNone(beskonta(code))

    def test_lanbao_no_generic_defaults(self):
        a=lanbao('LR18XBF05DPO-E2')['values']
        self.assertEqual((a['diameter'],a['sn'],a['output'],a['pin_count']),(18.,5.,'PNP',4.))
        for field in ('length','pitch','material','tmin','load','frequency','ip'):self.assertNotIn(field,a)

    def test_lanbao_quasi_flush_exact_table_wins(self):
        self.assertNotIn('mount',lanbao('LR18XBF08DPOY-E2')['values'])
        a=decode('LANBAO','LR18XBF08DPOY-E2')
        self.assertEqual(a['fields']['mount']['value'],'quasi-flush')
        self.assertTrue(a['requires_review'])

    def test_lanbao_temperature_and_special_execution(self):
        a=lanbao('LR18XBF05DPOW1-E2')['values']
        self.assertEqual((a['tmin'],a['tmax']),(-40.,70.))
        self.assertEqual(lanbao('LR18XBF05DPOB-E2')['special'],['pressure'])
        self.assertEqual(lanbao('LR18XBF05DPOJ-E2')['special'],['speed'])

    def test_card_conflict_remains_visible(self):
        s=normalize_sensor({'manufacturer':'Autonics','article':'PR12-2DN','category':'inductive',
                            'props':{'Output type':'PNP','Mounting':'flush','Sensing distance':'2'}})
        self.assertEqual(s.values['output'],'PNP')
        self.assertEqual(s.decoding['fields']['output']['application'],'conflict')
        self.assertTrue(s.decoding['requires_review'])

    def test_family_conflict_stops_enrichment(self):
        s=normalize_sensor({'manufacturer':'Autonics','article':'PR12-2DN','category':'capacitive'})
        self.assertEqual(s.family,'capacitive');self.assertNotIn('diameter',s.values)
        self.assertTrue(s.decoding['requires_review'])

    def test_unconfirmed_code_requires_review(self):
        s=normalize_sensor({'manufacturer':'Autonics','article':'PR12-2DN'})
        self.assertTrue(s.decoding['requires_review'])
        self.assertTrue(s.raw['diameter'][0]['source_url'].startswith('https://www.autonics.com/'))

    def test_pepperl_thread_length_is_not_body_length(self):
        a=pepperl_fuchs('NBB5-18GM50-E2-V1')['values']
        self.assertEqual((a['diameter'],a['output'],a['connector']),(18.,'PNP','m12'))
        self.assertNotIn('length',a);self.assertNotIn('vmax',a);self.assertNotIn('pitch',a)
        self.assertEqual(pepperl_fuchs('NBB0,6-3M22-E0-0,3M-V3')['values']['connection'],'cable+connector')
        self.assertIsNone(pepperl_fuchs('NBB5-18GM50-E2-V1-Y99'))

    def test_teko_body_index_is_not_diameter(self):
        a=teko('ISBA2A-31P-2-LZ')['values']
        self.assertEqual((a['output'],a['sn'],a['body_type']),('PNP',2.,'threaded'))
        for f in ('diameter','pitch','length','tmin','load'):self.assertNotIn(f,a)
        self.assertEqual(teko('ISBA2A-31P-2-LZ-C1')['values']['tmin'],-45.)
        self.assertIsNone(teko('ISBA2A-31P-2-LZ-EX'))

    def test_source_aliases_and_labelled_length(self):
        s=normalize_sensor({'props':{'Обозначение резьбы':'М18х1','Габаритный размер, мм':'M18 / L = 61',
            'Вид подключения':'Кабель','Температура окружающей среды':'-30…+70 °С',
            'Максимальный ток коммутационного элемента, мА':'200','Монтажное исполнение':'Неутапливаемое'}})
        self.assertEqual((s.values['pitch'],s.values['length'],s.values['load']),(1.,61.,200.))
        self.assertEqual((s.values['mount'],s.values['tmin']),('non-flush',-30.))

    def test_unlabelled_length_is_not_thread_pitch(self):
        s=normalize_sensor({'props':{'Размер корпуса':'М18x70'}})
        self.assertNotIn('pitch',s.values)

    def test_general_principle_description_is_not_analog_execution(self):
        s=normalize_sensor({'category':'Индуктивные датчики','props':{'Тип выхода':'PNP'},
             '_specifications':{'description':'Генератор преобразует изменение в аналоговый сигнал, затем в логический.'}})
        self.assertNotIn('analog',s.special)
        s=normalize_sensor({'category':'Индуктивные датчики','props':{'Аналоговый выход по току, мА':'4…20'}})
        self.assertIn('analog',s.special)

    def test_reference_is_exact_and_manufacturer_scoped(self):
        from price_monitor.inductive_references import exact_reference
        self.assertEqual(exact_reference('Balluff','BES005N')['values']['function'],'NC')
        self.assertEqual(exact_reference('SICK','1040764')['values']['length'],65.)
        self.assertIsNone(exact_reference('ifm','IGT201'))
        self.assertIsNone(exact_reference('SICK','BES005N'))

    def test_known_sensor_protection_is_not_unknown_modification(self):
        a=decode('СЕНСОР','ВБИ-М18-76В-1121-С')
        self.assertFalse(a['requires_review'])
        self.assertTrue(decode('СЕНСОР','ВБИ-М18-76В-1121-XYZ')['requires_review'])

    def test_ac_rated_current_range_preserves_maximum(self):
        s=normalize_sensor({'props':{'Коммутируемый ток [AC]':'5…250 мА','Схема подключения':'2х-пров.'}})
        self.assertEqual(s.values['load'],250.);self.assertEqual(s.values['output'],'2-wire')

    def test_coverage_distinguishes_raw_and_derived(self):
        from price_monitor.characteristic_audit import audit_records
        fields=[]
        summary,issues=audit_records([{'manufacturer':'Autonics','article':'PR12-2DN','category':'inductive'}],fields)
        row=next(x for x in fields if x['Характеристика']=='Диаметр корпуса, мм')
        self.assertEqual((row['Из карточек'],row['После дозаполнения']),(0,1))
        self.assertEqual(summary[0]['Индуктивных'],1)
        self.assertTrue(issues[0]['Проверить обозначение'])

    def test_fractional_lanbao_distance_needs_exact_series_table(self):
        self.assertIsNone(lanbao('LR08XBF15DPO'))

    def test_explicit_stainless_grade_is_not_carbon_steel(self):
        s=normalize_sensor({'props':{'Материал корпуса сенсора':'ST — сталь 12Х18Н10Т'}})
        self.assertEqual(s.values['material'],'stainless')


if __name__=='__main__':unittest.main()
