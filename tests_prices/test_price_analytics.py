import unittest
from pathlib import Path
import tempfile
from bs4 import BeautifulSoup

from price_monitor.details import attributes
from price_monitor.price_terms import parse_terms, price_views
from price_monitor.price_analytics import aggregate, model_statistics
from price_monitor.matching_normalize import normalize_sensor
from price_monitor.notations import REGISTRY
from price_monitor.sources import SOURCES


class AnalyticsChecks(unittest.TestCase):
    def setUp(self):
        self.sensor=normalize_sensor({'title':'Индуктивный датчик'})
        self.terms={'basis':'gross','rate':20.,'unit':'piece'}
        self.item={'rule_id':1,'manufacturer':'LANBAO','article':'A','our_currency':'RUB','price_checked_at':'2026-09-30',
                   '_price_terms':{'ours':self.terms}}

    def observation(self, price, date, **kw):
        return dict(rule_id=1,price=price,checked_at=date,status='priced',currency='RUB',
                    _specifications={'price_terms':self.terms},**kw)

    def test_vat_does_not_assume_rate_or_basis(self):
        self.assertEqual(price_views(120,parse_terms('120 руб.')),{'internet':120.,'gross':None,'net':None})
        self.assertEqual(price_views(120,parse_terms('120 руб. с НДС')),{'internet':120.,'gross':120.,'net':None})
        self.assertEqual(price_views(120,parse_terms('120 руб. с НДС 20% за шт.'))['net'],100)
        self.assertEqual(price_views(100,parse_terms('100 руб. без НДС 22% за шт.'))['gross'],122)
        self.assertEqual(parse_terms('с НДС / без НДС')['basis'],'unknown')
        self.assertIsNone(price_views(float('nan'),self.terms)['internet'])

    def test_model_change_and_saved_direct_delta(self):
        h=[self.observation(120,'2026-09-01'),self.observation(144,'2026-09-30')]
        s=model_statistics(self.item,self.sensor,h,'net',120,True)
        self.assertAlmostEqual(s['change'],20)
        self.assertAlmostEqual(s['gap'],20)
        self.assertEqual(s['delta'],20)
        self.assertIsNone(model_statistics(self.item,self.sensor,h,'net',120,False)['gap'])

    def test_unknown_unit_currency_and_historical_price_block_delta(self):
        h=[self.observation(144,'2026-09-30')]
        for item in ({**self.item,'our_currency':'USD'},{**self.item,'price_checked_at':'2026-10-01'},
                     {**self.item,'_price_terms':{'ours':{**self.terms,'unit':''}}}):
            self.assertIsNone(model_statistics(item,self.sensor,h,'net',120,True)['gap'])

    def test_one_point_missing_and_changed_currency_are_not_zero(self):
        a=self.observation(120,'2026-09-01'); b=self.observation(144,'2026-09-30'); b['currency']='USD'
        for history in ([],[a],[a,b]):
            self.assertIsNone(model_statistics(self.item,self.sensor,history)['change'])

    def test_vat_change_raw_series_not_compared_but_net_is(self):
        a=self.observation(120,'2026-09-01');b=self.observation(122,'2026-09-30')
        b['_specifications']={'price_terms':{**self.terms,'rate':22}}
        self.assertIsNone(model_statistics(self.item,self.sensor,[a,b])['change'])
        self.assertAlmostEqual(model_statistics(self.item,self.sensor,[a,b],'net')['change'],0)

    def test_manual_conditions_do_not_leak_to_new_or_historical_observation(self):
        self.item['_price_terms']['competitor']={'basis':'net','rate':20.,'unit':'piece','price_checked_at':'2026-09-01'}
        h=[self.observation(144,'2026-09-30')]
        self.assertEqual(model_statistics(self.item,self.sensor,h,'net')['net'],120)
        h=[self.observation(120,'2026-09-01')]
        self.assertEqual(model_statistics(self.item,self.sensor,h,'net')['net'],100)

    def test_aggregate_weights_models_and_shows_denominators(self):
        rows=[]
        for change in (10,30,None):
            row=model_statistics(self.item,self.sensor,[])
            row['change']=change;rows.append(row)
        result=aggregate(rows)[0]
        self.assertEqual(result['Среднее изменение, %'],20)
        self.assertEqual(result['Мин. изменение, %'],10)
        self.assertEqual(result['Макс. изменение, %'],30)
        self.assertEqual(result['С динамикой'],2)
        self.assertEqual(result['Моделей'],3)


class ManufacturerChecks(unittest.TestCase):
    def test_lanbao_lr12_does_not_inherit_lr18_parameters(self):
        sensor=normalize_sensor({'manufacturer':'LANBAO','article':'LR12XBN08DPOY-E2'})
        self.assertEqual((sensor.values['diameter'],sensor.values['sn'],sensor.values['length'],sensor.values['frequency']),(12.,8.,71.,500.))
        self.assertFalse(normalize_sensor({'manufacturer':'LANBAO','article':'LR12XBF08DPOY-E2'}).decoding['supported'])

    def test_registry_covers_every_collected_brand(self):
        self.assertEqual(set(REGISTRY),{brand for spec in SOURCES.values() for brand in spec.brands})

    def test_lanbao_quasi_flush_not_conflated_and_conflicts_preserved(self):
        sensor=normalize_sensor({'manufacturer':'LANBAO','article':'LR18XBF08DPOY-E2',
                                 'props':{'Способ установки':'Встраиваемый','Длина корпуса, мм':'63','Расстояние срабатывания':'8'}})
        self.assertEqual(sensor.values['mount'],'flush')
        self.assertEqual(sensor.decoding['fields']['mount']['value'],'quasi-flush')
        self.assertEqual(sensor.decoding['fields']['mount']['application'],'conflict')
        self.assertTrue(sensor.decoding['requires_review'])
        self.assertNotIn('pitch',sensor.values)

    def test_lanbao_unknown_suffix_or_other_brand_gets_no_defaults(self):
        for brand,model in [('LANBAO','LR18XBF08DPOY-E2-OLD'),('Balluff','LR18XBF08DPOY-E2')]:
            sensor=normalize_sensor({'manufacturer':brand,'article':model})
            self.assertEqual(sensor.values,{})
            self.assertFalse(sensor.decoding['supported'])

    def test_sensor_conditional_length_not_real_length(self):
        sensor=normalize_sensor({'manufacturer':'СЕНСОР','article':'ВБИ-М18-80Р-2111'})
        self.assertEqual(sensor.values['diameter'],18)
        self.assertEqual(sensor.values['output'],'PNP')
        self.assertNotIn('length',sensor.values)
        self.assertNotIn('sn',sensor.values)
        self.assertTrue(sensor.decoding['requires_review'])

    def test_lanbao_connectors_not_wires(self):
        sensor=normalize_sensor({'manufacturer':'LANBAO','article':'unknown','props':{'Число контактов, pin:':'4'}})
        self.assertEqual(sensor.values['pin_count'],4)
        self.assertNotIn('wire_count',sensor.values)

    def test_sensoren_collects_full_and_summary_properties_without_hiding_conflicts(self):
        html='''<ul class="characteristics-all"><li><span>Монтаж:</span><span>Невстраиваемый</span></li>
            <li>Выход: PNP</li></ul><div class="product-info__all-characteristics"><ul>
            <li>Монтаж: Встраиваемый</li><li>Выход: PNP</li></ul></div>'''
        props=attributes('sensoren',BeautifulSoup(html,'html.parser'))
        self.assertEqual(len(props),3)
        self.assertIn('mount',normalize_sensor({'props':props}).conflicts)


class StorageChecks(unittest.TestCase):
    def test_terms_persist_and_model_switch_clears_them_and_all_runs_are_accessible(self):
        from price_monitor.storage import Store
        from price_monitor.library import Library
        from price_monitor.models import Rule
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'audit.sqlite3')
            store.enqueue([Rule('sensoren','LANBAO','A','https://sensoren.ru/product/a/')])
            library=Library(store.catalog)
            rid=library.products()[1][0]['rule_id']
            library.save_comparisons([{'rule_id':rid,'our_article':'A','our_price':'100'}])
            term={'basis':'gross','rate':20.,'unit':'piece'}
            library.save_price_terms(rid,term,term)
            self.assertEqual(library.price_terms()[rid]['ours'],term)
            library.save_comparisons([{'rule_id':rid,'our_article':'B','our_price':None}])
            self.assertNotIn(rid,library.price_terms())
            with store.connect() as c:
                for n in range(205):c.execute("INSERT INTO runs(state,created_at) VALUES('completed',?)",(f'2026-09-01T00:00:{n:03}',))
            self.assertEqual(store.runs_count(),206)
            self.assertEqual(len(store.runs(limit=50,offset=200)),6)
