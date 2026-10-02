"""Isolated UI verification; synthetic fixtures never enter the working database."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json, os, sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from price_monitor.matching import load_catalog
from price_monitor.storage import Store
from price_monitor.catalog_schema import document


def prepare():
    target=ROOT/'data/matching_validation';target.mkdir(parents=True,exist_ok=True)
    db_path=target/'ui_fixtures.sqlite3'
    store=Store(db_path)
    catalog,_=load_catalog();base=next(p for p in catalog['products'] if p['model']=='ИВ05-NO-PNP(Л63)')
    now=datetime.now(timezone.utc)
    with store.connect() as c:
        # Only this explicitly named test database is initialized. Idempotent
        # inserts preserve any saved selections during a browser verification.
        for i,kind in enumerate(('DIRECT','CLOSE','REVIEW','CAPACITIVE'),1):
            props=dict(base['props'])
            if kind=='CLOSE':props['Способ подключения']='Клеммник'
            if kind=='REVIEW':props['Типоразмер корпуса, мм']='Цилиндрический с резьбой, M12'
            category='Ёмкостные датчики' if kind=='CAPACITIVE' else 'Индуктивные датчики'
            payload={'category':category,'attributes':[{'name':k,'value':str(v),'group':''} for k,v in props.items()]}
            fingerprint,encoded,_=document(json.dumps(payload,ensure_ascii=False))
            article='TEST-'+kind;url='https://mega-k.com/products/test-'+kind.lower()
            c.execute('INSERT INTO rules VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',(i,article,'megak','МЕГА-К',article,url,'',now.isoformat()))
            c.execute('INSERT INTO product_documents VALUES(?,?) ON CONFLICT(fingerprint) DO NOTHING',(fingerprint,encoded))
            c.execute('INSERT INTO product_index VALUES(?,?,?,?,?,?,?) ON CONFLICT(rule_id) DO NOTHING',(i,category+' · тестовая карточка',category,article,fingerprint,len(props),now.isoformat()))
            c.execute('INSERT INTO comparison_items VALUES(?,?,?,?,?,?) ON CONFLICT(rule_id) DO NOTHING',(i,base['model'] if kind=='DIRECT' else '', '2500' if kind=='DIRECT' else None,'RUB','Тестовая запись для проверки интерфейса',now.isoformat()))
            for day in (10,0):
                run_id=100+i*2+int(day==0);when=(now-timedelta(days=day)).isoformat()
                c.execute('INSERT INTO runs(id,state,created_at) VALUES(?,?,?) ON CONFLICT(id) DO NOTHING',(run_id,'completed',when))
                c.execute('INSERT INTO jobs(id,run_id,rule_id,state) VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING',(run_id,run_id,i,'done'))
                c.execute('''INSERT INTO observations(id,job_id,status,url,title,price,currency,availability,price_text,availability_text,detail,checked_at,http_status,response_hash,details_json)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',(run_id,run_id,'priced',url,'Тестовая карточка','3200' if day else '3300','RUB','in_stock','Тестовая цена','','Тестовый ряд для проверки интерфейса',when,200,'fixture',json.dumps({'ref':fingerprint})))
    preview=target/'preview_app.py'
    preview.write_text('''from pathlib import Path
import os,sys,runpy
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
os.environ['PRICE_MONITOR_DB']=str(Path(__file__).with_name('ui_fixtures.sqlite3'))
import streamlit as st
from cloud.presentation import apply_theme,brand,masthead
st.set_page_config(page_title='Подбор датчиков · локальная проверка',layout='wide')
apply_theme();masthead()
with st.sidebar:brand()
st.caption('ЛОКАЛЬНАЯ ПРОВЕРКА · Тестовые карточки и цены, отдельная база. Справочник наших моделей — из предоставленного каталога.')
runpy.run_path(str(ROOT/'price_monitor/ui.py'),init_globals={'CLOUD_MODE':False})
''',encoding='utf-8')
    return preview


def check(preview):
    from streamlit.testing.v1 import AppTest
    app=AppTest.from_file(str(preview),default_timeout=90)
    app.query_params['workspace']='prices';app.query_params['price_section']='compare'
    app.run()
    assert not app.exception,list(app.exception)
    assert not app.error,[x.value for x in app.error]
    group=app.radio(key='matching_group')
    assert group.value=='direct',group.value
    for target in ('close','review','all'):
        app.radio(key='matching_group').set_value(target).run()
        assert not app.exception,list(app.exception)
        assert not app.error,[x.value for x in app.error]
    app.radio(key='matching_group').set_value('close').run()
    apply=next(b for b in app.button if b.label=='Сохранить сопоставление')
    apply.click().run()
    assert not app.exception,list(app.exception)
    assert not app.error,[x.value for x in app.error]
    from price_monitor.library import Library
    rows=Library(Store(preview.with_name('ui_fixtures.sqlite3')).catalog).products(rule_id=2)[1]
    assert rows[0]['our_article'] and rows[0]['our_price'] is None
    app.query_params['price_section']='sources';app.query_params['source_tab']='algorithms';app.run()
    assert not app.exception,list(app.exception)
    assert not app.error,[x.value for x in app.error]
    assert any(t.label=='Алгоритмы подбора' for t in app.tabs)
    assert app.selectbox(key='matching_algorithm_group').value=='inductive'
    print('UI checks passed: three match groups, all, save choice without stale price, algorithms tab.')

if __name__=='__main__':
    preview=prepare()
    if '--prepare-only' not in sys.argv:check(preview)
    print('Preview entrypoint:',preview)
