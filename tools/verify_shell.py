"""Browser regression checks; synthetic pages and the local preview only.

Prepare: python tools/verify_redesign.py --prepare-only
Start streamlit data/matching_validation/redesign_app.py on localhost:8517,
and tools/shell_refresh_app.py on localhost:8518, then run this script.
"""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'data'/'layout_check'
OUT.mkdir(parents=True, exist_ok=True)
URL = 'http://127.0.0.1:8517'


def dimensions(page):
    return page.evaluate("""() => {
      const main=document.querySelector('[data-testid=stMain]');
      const app=document.querySelector('[data-testid=stApp]');
      return {width:innerWidth,height:innerHeight,documentHeight:document.documentElement.scrollHeight,
        documentWidth:document.documentElement.scrollWidth,appBottom:app.getBoundingClientRect().bottom,
        mainHeight:main.clientHeight,mainScroll:main.scrollHeight,scrollTop:main.scrollTop};
    }""")


with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1366,'height':768})
    report={'pages':[]}
    for workspace,section,heading in [
        ('home','','Конкурентная аналитика'),('news','','Новости'),
        ('prices','home','Цены'),('prices','collect','Сбор цен'),
        ('prices','products','База товаров'),('prices','compare','Сравнение цен'),
        ('prices','runs','Запуски и результаты'),('prices','sources','Источники и правила сбора')]:
        page.goto(f'{URL}/?workspace={workspace}&price_section={section}',wait_until='domcontentloaded')
        page.get_by_role('heading',name=heading,exact=True).wait_for(timeout=60000)
        page.wait_for_function("document.querySelector('[data-testid=stApp]').dataset.testScriptState !== 'running'",timeout=60000)
        assert page.locator('[data-testid=stException]').count()==0
        for viewport in ({'width':1366,'height':768},{'width':1952,'height':946},{'width':390,'height':844}):
            page.set_viewport_size(viewport)
            page.wait_for_timeout(250)
            page.locator('[data-testid=stMain]').evaluate('(e)=>e.scrollTo(0,e.scrollHeight)')
            d=dimensions(page)
            assert abs(d['appBottom']-d['height'])<=1,d
            assert d['documentHeight']<=d['height']+1,d
            assert d['documentWidth']<=d['width']+1,d
            assert abs(d['mainScroll']-d['mainHeight']-d['scrollTop'])<=2,d
            report['pages'].append({'workspace':workspace,'section':section,**d})
        page.screenshot(path=str(OUT/f'local_{workspace}_{section or "home"}.png'))
        page.set_viewport_size({'width':1366,'height':768})

    # The shared theme must preserve results during actual slow fragment reruns.
    page.goto('http://127.0.0.1:8518',wait_until='domcontentloaded')
    page.get_by_text('Сохранённые данные').wait_for(timeout=60000)
    seen_running=seen_stale=False
    for _ in range(32):
        state=page.evaluate("""() => ({
          running:document.querySelector('[data-testid=stApp]')?.dataset.testScriptState,
          stale:[...document.querySelectorAll('[data-stale=true], [data-testid=stExpander] summary')].map(e=>getComputedStyle(e).opacity),
          visible:getComputedStyle(document.querySelector('.skb-refresh-status')).display,
          arrow:getComputedStyle(document.querySelector('.skb-refresh-icon'),'::before').animationName})""")
        if state['running']=='running':
            seen_running=True
            assert state['arrow']=='skb-refresh' and state['visible']=='flex',state
        else:
            assert state['visible']=='none',state
        if state['stale']:
            seen_stale=True
            assert all(opacity=='1' for opacity in state['stale']),state
        page.wait_for_timeout(250)
    assert seen_running and seen_stale,(seen_running,seen_stale)
    page.get_by_text('Сохранённые данные').wait_for()
    page.screenshot(path=str(OUT/'local_refresh.png'))
    page.emulate_media(reduced_motion='reduce')
    assert page.locator('.skb-refresh-icon').evaluate("e=>getComputedStyle(e,'::before').animationName")=='none'
    assert page.locator('#skb-refresh-status').count()==1
    report['refresh']={'readable_stale_data':True,'animated_arrow':True,'reduced_motion':True}

    # Simulate a same-origin Cloud wrapper with the reported gap and outer scroll.
    css=(ROOT/'cloud/shell.css').read_text(encoding='utf-8')
    js=(ROOT/'cloud/shell.js').read_text(encoding='utf-8')
    host=browser.new_page(viewport={'width':1366,'height':662})
    wrapper='''<html><style>html,body{margin:0}iframe{width:100%;height:calc(100vh - 80px);border:0}#spacer{height:160px}</style><body><div id="root"><iframe title="streamlitApp" src="/app"></iframe><div id="spacer"></div></div></body></html>'''
    inner='<html><style>'+css+'</style><body><div data-testid="stApp">App</div></body></html>'
    host.route('https://layout-check.streamlit.app/**',lambda route:route.fulfill(content_type='text/html',body=inner if route.request.url.endswith('/app') else wrapper))
    host.goto('https://layout-check.streamlit.app/')
    frame=host.frame_locator('iframe')
    frame.locator('[data-testid=stApp]').wait_for()
    before=host.locator('iframe').bounding_box()
    assert before['height']==582,before
    child=next(f for f in host.frames if f.url.endswith('/app'))
    child.evaluate(js)
    child.evaluate(js)  # Reruns must not multiply styles or listeners.
    for viewport in ({'width':1366,'height':662},{'width':1952,'height':946},{'width':390,'height':844}):
        host.set_viewport_size(viewport)
        host.wait_for_timeout(100)
        assert host.locator('iframe').bounding_box()['height']==viewport['height']
        assert host.evaluate('document.documentElement.scrollHeight')<=viewport['height']
    assert host.locator('#skb-cloud-viewport').count()==1
    report['cloud_wrapper']={'original_gap':80,'fixed':True,'single_style_after_rerun':True}
    (OUT/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    browser.close()
