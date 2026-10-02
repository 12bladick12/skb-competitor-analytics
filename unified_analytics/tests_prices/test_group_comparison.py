from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from bs4 import BeautifulSoup
from openpyxl import Workbook

from price_monitor.own_prices import OwnPrices, read_prices
from price_monitor.storage import Store
from price_monitor.matching import Matcher
from price_monitor.matching_normalize import normalize_sensor
from price_monitor.comparison_groups import ComparisonIndex, own_record, competitor_record, delta_between, difference_view, price_info, history_points
from price_monitor.price_terms import parse_terms, extract_price_terms


def sensor(model='SKB-A',article='00123'):
    return normalize_sensor({'code':'code:'+article,'model':model,'article':article,'category':'Индуктивные датчики',
        'props':{'Типоразмер корпуса, мм':'Цилиндрический с резьбой, М18x1','Схема выхода':'PNP',
                 'Функция выхода':'NO','Способ установки':'Встраиваемый','Номинальное расстояние переключения, Sn, мм':'5'}})


def workbook(rows):
    w=Workbook();s=w.active;s.title='Цены';s.append(['Наименование','Артикул','Цена'])
    for row in rows:s.append(row)
    data=BytesIO();w.save(data);return data.getvalue()


class OwnPriceTests(unittest.TestCase):
    def test_incomplete_import_is_not_visible_as_a_current_price_list(self):
        metadata,rows=read_prices(workbook([['Model','00123',100]]),'p.xlsx','2026-10-01',Matcher([sensor()]))
        with TemporaryDirectory() as tmp:
            prices=OwnPrices(Store(Path(tmp)/'prices.sqlite3').catalog)
            prices.import_rows(metadata,rows)
            prices.repo.batch('UPDATE own_price_imports SET row_count=2 WHERE batch_id=%(batch)s',{'batch':metadata['batch_id']})
            self.assertEqual(prices.current('2026-10-01'),[])
            self.assertEqual(prices.imports(),[])
            prices.repo.batch('UPDATE own_price_imports SET row_count=1 WHERE batch_id=%(batch)s',{'batch':metadata['batch_id']})
            self.assertEqual(len(prices.current('2026-10-01')),1)
            self.assertEqual(len(prices.imports()),1)

    def test_exact_article_net_gross_and_idempotent_import(self):
        matcher=Matcher([sensor()])
        raw=workbook([['Name preserved','00123',100.05],['Service','other',200]])
        metadata,rows=read_prices(raw,'prices.xlsx','2026-10-01',matcher)
        self.assertEqual(rows[0]['article'],'00123')
        self.assertEqual(rows[0]['net_price'],'100.05')
        self.assertEqual(rows[0]['gross_price'],'122.06')
        self.assertEqual(rows[0]['catalog_id'],'code:00123')
        self.assertEqual(rows[1]['catalog_id'],'')
        self.assertEqual(rows[1]['unit'],'')
        self.assertEqual(metadata['matched_count'],1)
        with TemporaryDirectory() as tmp:
            prices=OwnPrices(Store(Path(tmp)/'prices.sqlite3').catalog)
            self.assertTrue(prices.import_rows(metadata,rows))
            self.assertFalse(prices.import_rows(metadata,rows))
            self.assertEqual(len(prices.current('2026-10-01')),2)
            self.assertEqual(len(prices.current('2026-09-30')),0)

    def test_reject_duplicates_missing_price_and_no_fuzzy_link(self):
        matcher=Matcher([sensor()])
        for data in ([['A','00123',1],['B','00123',2]],[['A','1',None]],[['A','1',0]]):
            with self.assertRaises(ValueError):read_prices(workbook(data),'p.xlsx','2026-10-01',matcher)
        _,rows=read_prices(workbook([['SKB-A','123',1]]),'p.xlsx','2026-10-01',matcher)
        self.assertEqual(rows[0]['catalog_id'],'')


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.own=own_record(sensor(),{'net_price':'100','gross_price':'122','vat_rate':'22','currency':'RUB',
                                     'unit':'piece','effective_date':'2026-10-01','source_name':'p.xlsx'})
        self.competitor=competitor_record({'rule_id':1,'manufacturer':'ТЕКО','article':'OTHER','last_price':'183',
            'last_currency':'RUB','price_checked_at':'2026-10-01T08:00:00+00:00',
            '_specifications':{'price_terms':{'basis':'gross','rate':22,'unit':'piece'}}})

    def test_selected_analogue_delta_no_direct_status_gate(self):
        delta,gap,note=delta_between(self.competitor,self.own)
        self.assertEqual(delta,61)
        self.assertAlmostEqual(gap,50)
        self.assertEqual(note,'')
        delta,gap,_=delta_between(self.own,self.competitor)
        self.assertEqual(delta,-61)
        self.assertAlmostEqual(gap,-100/3)

    def test_unknown_tax_explicit_raw_delta_currency_and_package_guards(self):
        self.competitor['_specifications']={}
        delta,gap,note=delta_between(self.competitor,self.own)
        self.assertIsNone(delta)
        self.assertIsNone(gap)
        self.assertIn('Недостаточно условий',note)
        delta,gap,note=delta_between(self.competitor,self.own,'internet')
        self.assertEqual(delta,83)
        self.assertIn('НДС не выровнен',note)
        self.competitor['last_currency']='USD'
        self.assertIsNone(delta_between(self.competitor,self.own)[0])
        self.competitor['last_currency']='RUB'
        self.competitor['_specifications']={'price_terms':{'basis':'gross','rate':22,'unit':'pack'}}
        self.assertIsNone(delta_between(self.competitor,self.own)[0])

    def test_difference_is_a_magnitude_with_direction(self):
        self.assertEqual(difference_view(-61,-100/3)['direction'],'Дешевле')
        self.assertEqual(difference_view(-61,-100/3)['amount'],61)
        self.assertGreater(difference_view(-61,-100/3)['percent'],0)
        self.assertEqual(difference_view(61,50),{'amount':61,'percent':50,'direction':'Дороже'})
        self.assertEqual(difference_view(0,0)['direction'],'Одинаковая цена')
        self.assertIsNone(difference_view(None,None)['amount'])

    def test_period_does_not_redefine_current_price_and_gaps_are_preserved(self):
        observations=[{'checked_at':'2026-09-01T00:00:00+00:00','status':'priced','price':'150','currency':'RUB'},
                      {'checked_at':'2026-09-02T00:00:00+00:00','status':'network_error'},
                      {'checked_at':'2026-09-03T00:00:00+00:00','status':'priced','price':'160','currency':'RUB'}]
        points=history_points(self.competitor,observations,'internet')
        self.assertEqual([p['segment'] for p in points],['0','1'])
        self.assertEqual(price_info(self.competitor)['raw'],183)
        self.assertEqual(len(history_points(self.own,[],'gross')),1)

    def test_recheck_creates_a_new_quote_without_backdating_vat(self):
        self.competitor['_automatic_price_terms']={'basis':'gross','rate':22,'unit':'piece',
            'verified_price':'183','verified_currency':'RUB','checked_at':'2026-10-02T08:00:00+00:00'}
        old=[{'checked_at':'2026-10-01T08:00:00+00:00','status':'priced','price':'183','currency':'RUB'}]
        points=history_points(self.competitor,old,'gross')
        self.assertEqual(len(points),1)
        self.assertEqual(points[0]['date'],'2026-10-02T08:00:00+00:00')
        self.assertEqual(price_info(self.competitor)['date'],'2026-10-02T08:00:00+00:00')

    def test_every_brand_is_compared_with_anchor_and_own_search_is_exact(self):
        source=sensor()
        records=[]
        for number,brand in enumerate(('ТЕКО','BESKONTA','МЕГА-К'),1):
            row={'rule_id':number,'manufacturer':brand,'article':'MODEL-'+str(number),'category':'Индуктивные датчики',
                 'props':{'Типоразмер корпуса, мм':'Цилиндрический с резьбой, М18x1','Схема выхода':'PNP',
                          'Функция выхода':'NO','Способ установки':'Встраиваемый',
                          'Номинальное расстояние переключения, Sn, мм':'5'}}
            records.append(row)
        index=ComparisonIndex(records,[source],{})
        anchor=index.search('00123')[0]
        self.assertTrue(anchor['is_ours'])
        alternatives=index.alternatives(anchor)
        self.assertEqual(set(alternatives),{'ТЕКО','BESKONTA','МЕГА-К'})
        self.assertEqual(len(index.search('MODEL-2')),1)

    def test_compact_catalog_preserves_matching_and_on_demand_evidence(self):
        from price_monitor.matching import evaluate
        from price_monitor.comparison_groups import IndexedMatch
        row={'rule_id':7,'manufacturer':'LANBAO','article':'LR18XBF08DPOY-E2',
             'category':'Индуктивные датчики',
             'props':{'Способ установки':'Встраиваемый','Длина корпуса, мм':'63'}}
        full=competitor_record(row)['sensor']
        index=ComparisonIndex([row],[sensor()],{})
        compact=index.records['competitor:7']['sensor']
        self.assertEqual(compact.values,full.values)
        self.assertEqual(compact.conflicts,full.conflicts)
        self.assertEqual(compact.decoding['requires_review'],full.decoding['requires_review'])
        self.assertFalse(compact.raw)
        self.assertNotIn('fields',compact.decoding)
        restored=normalize_sensor(index.records['competitor:7'])
        self.assertEqual(restored.values,full.values)
        self.assertEqual(restored.decoding,full.decoding)
        original=evaluate(full,sensor())
        cached=IndexedMatch(full,original)
        self.assertEqual(cached.status,original.status)
        self.assertEqual(cached.fields,original.fields)
        self.assertEqual(cached.rows(),original.rows())
        self.assertNotIn('fields',vars(cached))
        archived={**row,'_catalog_details_hash':'original','_specifications':{'description':'Keep source evidence'}}
        lean=ComparisonIndex([archived],[sensor()],{},reference_loader=lambda _:archived)
        reference=lean.records['competitor:7']
        self.assertNotIn('props',reference)
        self.assertNotIn('description',reference['_specifications'])
        matches=lean.alternatives(reference)
        self.assertTrue(matches)
        self.assertEqual(matches['СКБ Индукция'][0][1].fields,original.fields)


class AutomaticTermsTests(unittest.TestCase):
    def test_tax_phrasing_and_conflict(self):
        self.assertEqual(parse_terms('Цена включает: с учётом НДС 22% / шт.')['basis'],'gross')
        self.assertEqual(parse_terms('Без учета НДС 22%')['basis'],'net')
        self.assertEqual(parse_terms('Не включая НДС 22%')['basis'],'net')
        self.assertEqual(parse_terms('НДС не включён')['basis'],'net')
        self.assertIsNone(parse_terms('НДС 20% и НДС 22%')['rate'])

    def test_vat_next_to_price_not_delivery_or_recommendations(self):
        soup=BeautifulSoup('''<div class="product-info__all-order"><div class="product-info__all-order-price">
            <b class="price">122 руб.</b></div><p>Цена с НДС 22% / шт.</p></div>
            <aside>Доставка без НДС</aside>''','html.parser')
        term=extract_price_terms('sensoren',soup,'https://sensoren.ru/product/test/')
        self.assertEqual((term['basis'],term['rate'],term['unit']),('gross',22,'piece'))

    def test_cost_label_elsewhere_in_product_scope(self):
        soup=BeautifulSoup('''<article itemtype="https://schema.org/Product"><div class="details-payment">
            <div class="details-payment-price"><span class="price-current">1683 руб.</span></div></div>
            <ul><li>Стоимость с НДС</li></ul></article>''','html.parser')
        term=extract_price_terms('megak',soup,'https://mega-k.com/products/test')
        self.assertEqual(term['basis'],'gross')
        self.assertIsNone(term['rate'])


if __name__=='__main__':unittest.main()
