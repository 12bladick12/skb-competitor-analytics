"""Rendered checks of the local review page at desktop and phone widths."""
from pathlib import Path
import argparse
import json
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'data/comparison_preview'

def main(refresh=False):
    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
        page.goto('http://127.0.0.1:8527/',wait_until='domcontentloaded')
        card=page.locator('[class*="st-key-group_card_"]').first
        card.wait_for(state='visible',timeout=45000)
        page.get_by_text('Скачать сравнение XLSX',exact=True).wait_for(timeout=45000)
        if refresh:
            summary=page.locator('summary').filter(has_text='Условия цен и НДС')
            summary.click()
            page.get_by_role('button',name='Обновить условия с сайтов',exact=True).click()
            spinner=page.get_by_text('Проверяем условия выбранных карточек…',exact=True)
            spinner.wait_for(state='visible',timeout=10000)
            spinner.wait_for(state='hidden',timeout=90000)
            page.get_by_role('button',name='Обновить условия с сайтов',exact=True).wait_for(timeout=15000)
            summary.click()
        page.wait_for_timeout(1200)
        card.screenshot(path=str(OUTPUT/'comparison-card-desktop.png'))
        results=[{'viewport':1440,'models':page.get_by_role('checkbox').count()}]
        page.set_viewport_size({'width':430,'height':930})
        card.scroll_into_view_if_needed()
        page.wait_for_timeout(700)
        page.screenshot(path=str(OUTPUT/'comparison-mobile.png'))
        results.append(page.evaluate("({viewport:innerWidth,body:document.body.scrollWidth,main:document.querySelector('[data-testid=stMain]').scrollWidth})"))
        assert results[-1]['body']<=430,results[-1]
        assert results[-1]['main']<=430,results[-1]
        (OUTPUT/'render_checks.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
        print(json.dumps(results))
        browser.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--refresh',action='store_true')
    main(parser.parse_args().refresh)
