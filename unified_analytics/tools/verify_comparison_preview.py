"""Exercise the real local preview, with site rechecks mocked only in this test."""
from pathlib import Path
from unittest.mock import patch
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest
import price_monitor.group_comparison_ui

def main():
    checks=[]
    with patch('price_monitor.group_comparison_ui.automatic_refresh',return_value=None):
        app=AppTest.from_file(str(ROOT/'tools/comparison_preview.py'))
        app.run(timeout=60)
        assert not app.exception,[x.value for x in app.exception]
        deadline=time.monotonic()+30
        while time.monotonic()<deadline and not any(x.key=='catalog_filter_diameter' for x in app.selectbox):
            time.sleep(.1)
            app.run(timeout=60)
            assert not app.exception,[x.value for x in app.exception]
        assert any(x.key=='catalog_filter_diameter' for x in app.selectbox),'Characteristics did not load'
        app.multiselect(key='catalog_search_selection').set_value(['competitor:6503']).run(timeout=60)
        assert not app.exception,[x.value for x in app.exception]
        choices=[c for c in app.checkbox if c.key and c.key.startswith('group_visible_')]
        assert len(choices)>=5,len(choices)
        checks.append({'test':'competitor_search_all_brands','visible_models':len(choices)})
        baseline=next(s for s in app.selectbox if s.key and s.key.startswith('group_baseline_'))
        assert str(baseline.value).startswith('ours:'),baseline.value
        original=baseline.value
        options=list(baseline.options)
        own_model=options[baseline.index].split(' · ',1)[1]
        before=len(choices)
        first=next(c for c in choices if c.value)
        first.uncheck().run(timeout=60)
        assert not app.exception,[x.value for x in app.exception]
        after=[c for c in app.checkbox if c.key and c.key.startswith('group_visible_')]
        assert len(after)==before and not app.checkbox(key=first.key).value
        checks.append({'test':'checkbox_hides_only_series','rows_unchanged':before})
        baseline=next(s for s in app.selectbox if s.key and s.key.startswith('group_baseline_'))
        competitor=next(x for x in baseline.options if not x.startswith('СКБ Индукция'))
        baseline.select(competitor).run(timeout=60)
        assert not app.exception,[x.value for x in app.exception]
        checks.append({'test':'competitor_as_delta_baseline'})
        app.radio(key='group_price_basis').set_value('net').run(timeout=60)
        assert not app.exception,[x.value for x in app.exception]
        checks.append({'test':'net_price_mode'})
        app.multiselect(key='catalog_search_selection').set_value([original]).run(timeout=60)
        assert not app.exception,[x.value for x in app.exception]
        assert app.multiselect(key='catalog_search_selection').value==[original]
        checks.append({'test':'own_marking_search'})
        app.multiselect(key='catalog_search_selection').set_value(['competitor:6503',original]).run(timeout=60)
        assert not app.exception,[x.value for x in app.exception]
        assert len([s for s in app.selectbox if s.key and s.key.startswith('group_baseline_')])==2
        checks.append({'test':'multiple_comparisons'})
        app.multiselect(key='catalog_search_selection').set_value([]).run(timeout=60)
        assert not app.exception
        assert any('Выберите модели' in x.value for x in app.info)
        checks.append({'test':'empty_selection'})
    (ROOT/'data/comparison_preview/ui_checks.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(checks,ensure_ascii=False))

if __name__=='__main__':main()
