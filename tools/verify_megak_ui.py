"""UI regression: manufacturer identity reaches matching and evidence is visible."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest


comparison='''
from price_monitor.matching_ui import render_comparison
class LibraryFixture:
    def export_products(self,**kwargs):
        return [dict(rule_id=501,article='PS2-12M63-4B11-K',manufacturer='МЕГА-К',source='megak',title='',category='',_specifications={},our_article='',our_price=None,our_currency='RUB',last_price=None,last_currency='RUB',price_checked_at=None)]
    def history(self,*args):return []
render_comparison(LibraryFixture(),lambda:None,lambda *args:None)
'''
app=AppTest.from_string(comparison,default_timeout=60).run()
assert not app.exception,list(app.exception)
assert app.radio(key='matching_group').value=='review'
decoded=next(f.value for f in app.dataframe if 'Из обозначения' in f.value.columns)
assert (decoded['Применение']=='Дополнено из обозначения').any()
assert decoded.loc[decoded['Характеристика']=='Диаметр корпуса, мм','Из обозначения'].iloc[0]=='12'
assert any(e.label=='Расшифровка обозначения МЕГА-К' for e in app.expander)

app=AppTest.from_string('from price_monitor.algorithms import render_algorithms\nrender_algorithms()',default_timeout=60).run()
assert not app.exception,list(app.exception)
for model,expected in [('PS2-18M68-8N11-C4-T3','-45'),('PS2-18M68-8N11-K-T9',None)]:
    app.text_input(key='megak_notation_example').set_value(model).run()
    assert not app.exception,list(app.exception)
    decoded=next(f.value for f in app.dataframe if 'Из обозначения' in f.value.columns)
    temp=decoded[decoded['Характеристика']=='Нижняя температура, °C']
    assert temp.empty if expected is None else temp['Из обозначения'].iloc[0]==expected
print('UI PASSED: manufacturer-gated enrichment in comparison; source evidence; interactive valid and unknown designations.')
