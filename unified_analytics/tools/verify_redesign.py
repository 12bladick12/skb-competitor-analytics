"""Isolated navigation and analytics UI check; never uses production storage."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def prepare():
    from verify_matching_ui import prepare as prepare_fixtures
    prepare_fixtures()
    target=ROOT/'data/matching_validation/redesign_app.py'
    target.write_text('''from pathlib import Path
import os,sys,runpy
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ['PRICE_MONITOR_DB']=str(Path(__file__).with_name('ui_fixtures.sqlite3'))
import streamlit as st
st.html('<div class="preview-banner"><strong>Предпросмотр · вариант 02</strong><span>Тестовые товары и цены · рабочая база не подключена</span></div>')
runpy.run_path(str(ROOT/'streamlit_app.py'),run_name='__main__',init_globals={'PRICE_CLOUD_MODE':False})
''',encoding='utf-8')
    return target


def check(target):
    from streamlit.testing.v1 import AppTest
    app=AppTest.from_file(str(target),default_timeout=90).run()
    def valid():
        assert not app.exception,[x.value for x in app.exception]
        assert not app.error,[x.value for x in app.error]
    valid()
    assert app.title[0].value=='Конкурентная аналитика'
    app.button(key='portal_open_prices').click().run();valid()
    for page in ('collect','products','compare','runs'):
        assert app.button(key='portal_open_'+page)
    app.button(key='portal_open_compare').click().run();valid()
    for basis in ('gross','net','internet'):
        app.selectbox(key='analytics_basis').set_value(basis).run();valid()
    app.radio(key='analytics_scope').set_value('Вся база товаров').run();valid()
    app.query_params['price_section']='runs';app.run();valid()
    app.query_params['price_section']='products';app.run();valid()
    app.button(key='workspace_home').click().run();valid()
    app.button(key='portal_open_prices').click().run();valid()
    assert app.button(key='portal_open_collect')
    app.query_params['price_section']='sources';app.run();valid()
    assert any('Системы обозначений' in h.value for h in app.subheader)
    app.session_state['draft_editor']={'base':{'conclusions':'saved','items':[]},'working':{'conclusions':'unsaved','items':[]},'accepted':{}}
    app.query_params['workspace']='news';app.run();valid()
    app.button(key='workspace_home').click().run();valid()
    assert app.session_state['pending_workspace']=='home'
    assert app.query_params['workspace']==['news']
    next(b for b in app.button if b.label=='Перейти без сохранения').click().run();valid()
    assert app.button(key='portal_open_prices')
    print('Redesign UI passed: home, 4 price sections, 3 tax views, full-catalog summary, products, runs, sources, return navigation.')


if __name__=='__main__':
    target=prepare()
    if '--prepare-only' not in sys.argv:check(target)
    print(target)
