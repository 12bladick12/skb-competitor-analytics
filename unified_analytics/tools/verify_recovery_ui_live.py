"""Read-only verification of collection status after the reliability rollout."""
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright

OUT=Path(__file__).resolve().parents[1]/'data'/'reliability_audit'
OUT.mkdir(exist_ok=True)
URL='https://skb-competitor-analytics.streamlit.app/?workspace=prices&price_section=collect'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1600,'height':1100})
    for attempt in range(15):
        page.goto(URL,wait_until='domcontentloaded',timeout=60000)
        for _ in range(30):
            frame=next((f for f in page.frames if f.get_by_text('Состояние обновлено:',exact=False).count()),None)
            if frame:break
            page.wait_for_timeout(500)
        if frame:
            frame.locator('.sources-footer').wait_for(timeout=90000)
            break
        print('Waiting for recovery UI',attempt+1,flush=True)
    else:raise AssertionError('Recovery UI not loaded')
    assert not frame.locator('[data-testid=stException]').count()
    frame.locator('[data-testid=stDataFrame] canvas').first.wait_for(timeout=60000)
    page.wait_for_timeout(500)
    captions=frame.locator('[data-testid=stCaptionContainer]').all_text_contents()
    assert any('осталось карточек' in text for text in captions),captions
    page.screenshot(path=str(OUT/'collection_after_audit.png'))
    report={'url':URL,'captions':captions,'warnings':frame.locator('[data-testid=stAlert]').all_text_contents(),'passed':True}
    (OUT/'ui_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    browser.close()
