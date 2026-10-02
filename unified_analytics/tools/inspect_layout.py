"""Read-only browser measurements of the shared application shell."""
from pathlib import Path
import json
import sys
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'data'/'layout_check'
OUT.mkdir(parents=True, exist_ok=True)
MEASURE = """() => ({url:location.href, viewport:[innerWidth,innerHeight],
 scroll:[document.documentElement.scrollWidth,document.documentElement.scrollHeight],
 elements:[...document.querySelectorAll('html,body,#root,iframe,[data-testid="stApp"],[data-testid="stAppViewContainer"],[data-testid="stMain"],[data-testid="stMainBlockContainer"],[data-testid="stSidebar"],[data-testid="stStatusWidget"],[data-stale="true"],.stApp')].map(e=>{
 const s=getComputedStyle(e),r=e.getBoundingClientRect();return {tag:e.tagName,id:e.id,testid:e.dataset.testid,stale:e.dataset.stale,classes:e.className,
 rect:{x:r.x,y:r.y,w:r.width,h:r.height,bottom:r.bottom},scrollHeight:e.scrollHeight,clientHeight:e.clientHeight,
 style:{height:s.height,minHeight:s.minHeight,position:s.position,overflow:s.overflow,opacity:s.opacity,background:s.backgroundColor,margin:s.margin,padding:s.padding,zoom:s.zoom},html:e.outerHTML.slice(0,650)}})})"""

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1366,'height':768})
    page.goto(sys.argv[1] if len(sys.argv)>1 else 'https://skb-competitor-analytics.streamlit.app/?workspace=prices&price_section=collect',wait_until='domcontentloaded',timeout=60000)
    for _ in range(90):
        if any(f.get_by_role('heading',name='Сбор цен',exact=True).count() for f in page.frames):break
        page.wait_for_timeout(1000)
    app=next(f for f in page.frames if f.get_by_role('heading',name='Сбор цен',exact=True).count())
    # Read-only wait for the footer: never click collection controls.
    app.locator('.sources-footer').wait_for(timeout=120000)
    page.wait_for_timeout(1000)
    report={'frames':[f.evaluate(MEASURE) for f in page.frames]}
    report['updates']=[]
    for _ in range(24):
        report['updates'].append(app.evaluate("""() => ({state:document.querySelector('[data-testid=stApp]')?.dataset,
            faded:[...document.querySelectorAll('[data-testid], [data-stale]')].filter(e=>parseFloat(getComputedStyle(e).opacity)<1).slice(0,12).map(e=>({html:e.outerHTML.slice(0,450),opacity:getComputedStyle(e).opacity})),
            status:document.querySelector('[data-testid=stStatusWidget]')?.outerHTML})"""))
        page.wait_for_timeout(500)
    page.screenshot(path=str(OUT/'before.png'),full_page=True)
    for f in page.frames:
        f.evaluate("() => {document.querySelector('[data-testid=stMain]')?.scrollTo(0,100000); window.scrollTo(0,100000)}")
    page.wait_for_timeout(1500)
    report['bottom']=[f.evaluate(MEASURE) for f in page.frames]
    page.screenshot(path=str(OUT/'before_bottom.png'),full_page=True)
    (OUT/'inspection.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'frames':len(page.frames),'updates':report['updates']},ensure_ascii=False),flush=True)
    browser.close()
