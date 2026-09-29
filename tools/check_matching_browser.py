"""Visual smoke check against the isolated local preview server."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'data/matching_validation';OUT.mkdir(parents=True,exist_ok=True)

with sync_playwright() as p:
    try:browser=p.chromium.launch(headless=True)
    except Exception:browser=p.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1512,'height':1080},device_scale_factor=1)
    errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.goto('http://127.0.0.1:18512/?workspace=prices&price_section=compare',wait_until='domcontentloaded')
    page.get_by_text('Группа сопоставления',exact=True).wait_for(timeout=60000)
    page.get_by_text('TEST-DIRECT',exact=True).wait_for(timeout=45000)
    page.get_by_text('По указанному исполнению',exact=True).wait_for(timeout=30000)
    page.locator('[data-testid="stVegaLiteChart"] canvas').first.wait_for(timeout=30000)
    page.screenshot(path=str(OUT/'comparison.png'),full_page=True)
    assert page.locator('[data-testid="stException"]').count()==0
    page.get_by_text('Характеристики и варианты подбора',exact=True).first.click()
    page.get_by_text('Наша модель для сопоставления',exact=True).wait_for()
    page.screenshot(path=str(OUT/'matching_details.png'),full_page=True)
    page.goto('http://127.0.0.1:18512/?workspace=prices&price_section=sources&source_tab=algorithms',wait_until='domcontentloaded')
    page.get_by_role('tab',name='Алгоритмы подбора',exact=True).wait_for(timeout=45000)
    page.get_by_role('tab',name='Алгоритмы подбора',exact=True).click()
    page.get_by_role('button',name='Скачать блок-схему SVG').wait_for(timeout=45000)
    frame=page.frame_locator('iframe').first
    frame.locator('#drawing').wait_for(timeout=30000)
    frame.locator('#in').click();assert frame.locator('#scale').inner_text()=='125%'
    frame.locator('#fit').click();assert frame.locator('#scale').inner_text()=='100%'
    with page.expect_download() as download:page.get_by_role('button',name='Скачать блок-схему SVG').click()
    file=download.value;file.save_as(str(OUT/'downloaded_algorithm.svg'))
    page.locator('[data-testid="stTable"]').wait_for(timeout=30000)
    page.evaluate('document.querySelector("[data-testid=stMain]").scrollTo(0,0)')
    page.screenshot(path=str(OUT/'algorithms.png'),full_page=True)
    assert not errors,errors
    (OUT/'browser_check.json').write_text(json.dumps({'passed':True,'javascript_errors':errors,'svg_download':True,'zoom_controls':True},ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close()
    print('Browser passed: comparison, characteristic table, algorithm tab, zoom, SVG download.')
