"""Read-only check of the deployed shell. No clicks or collection writes."""
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

OUT=Path(__file__).resolve().parents[1]/'data'/'layout_check'
URL='https://skb-competitor-analytics.streamlit.app/'
report={'url':URL,'commit':'873fca740e827ccf683899a8d8b135f0300990a8','screens':[]}
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1366,'height':768})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    for attempt in range(24):
        page.goto(URL+'?workspace=home',wait_until='domcontentloaded',timeout=60000)
        for _ in range(20):
            if page.locator('#skb-cloud-viewport').count():break
            page.wait_for_timeout(500)
        if page.locator('#skb-cloud-viewport').count():break
        print('Waiting for deployed shell',attempt+1,flush=True)
    else:raise AssertionError('Updated shell has not reached the site')
    for workspace,section in [('home',''),('prices','collect'),('prices','products'),('prices','sources'),('news','')]:
        page.goto(URL+f'?workspace={workspace}&price_section={section}',wait_until='domcontentloaded')
        deadline=time.monotonic()+90
        app=None
        while time.monotonic()<deadline:
            app=next((f for f in page.frames if f.locator('#skb-refresh-status').count()),None)
            if app:break
            page.wait_for_timeout(500)
        assert app,'Missing refresh indicator'
        if section=='collect':
            app.locator('.sources-footer').wait_for(timeout=120000)
            samples=[]
            for _ in range(32):
                sample=app.evaluate("""() => ({state:document.querySelector('[data-testid=stApp]').dataset.testScriptState,
                    opacity:[...document.querySelectorAll('[data-stale=true]')].map(e=>getComputedStyle(e).opacity),
                    indicator:getComputedStyle(document.querySelector('#skb-refresh-status')).display})""")
                assert all(o=='1' for o in sample['opacity']),sample
                if sample['state']=='running':assert sample['indicator']=='flex',sample
                samples.append(sample)
                if sample['opacity'] and sample['indicator']=='flex':
                    page.screenshot(path=str(OUT/'live_refresh.png'))
                page.wait_for_timeout(500)
            assert any(s['opacity'] for s in samples),'No real refresh observed'
            report['refresh_samples']=samples
        assert app.locator('[data-testid=stException]').count()==0
        for viewport in ({'width':1366,'height':768},{'width':390,'height':844}):
            page.set_viewport_size(viewport);page.wait_for_timeout(300)
            app.locator('[data-testid=stMain]').evaluate('(e)=>e.scrollTo(0,e.scrollHeight)')
            host=page.evaluate('({height:innerHeight,scroll:document.documentElement.scrollHeight})')
            bounds=page.locator('iframe[title=streamlitApp]').bounding_box()
            inner=app.evaluate("({height:innerHeight,appBottom:document.querySelector('[data-testid=stApp]').getBoundingClientRect().bottom,scroll:document.documentElement.scrollHeight})")
            assert host['scroll']<=host['height']+1,(host,bounds)
            assert abs(bounds['height']-host['height'])<=1,bounds
            assert abs(inner['appBottom']-inner['height'])<=1,inner
            report['screens'].append({'workspace':workspace,'section':section,'viewport':viewport,'host':host,'inner':inner})
        page.screenshot(path=str(OUT/f'live_{workspace}_{section or "home"}.png'))
        page.set_viewport_size({'width':1366,'height':768})
        print('Verified live',workspace,section,flush=True)
    report['javascript_errors']=errors
    assert not errors,errors
    report['passed']=True
    (OUT/'live_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Live shell verification passed',flush=True)
    browser.close()
