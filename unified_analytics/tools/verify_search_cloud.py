"""Verify the published search through its public UI; never start a collector."""
from pathlib import Path
import json
import time
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'analysis/search_release_2026_10_01'
URL='https://skb-competitor-analytics.streamlit.app/?workspace=prices&price_section=compare'


def app_frame(page):
    deadline=time.monotonic()+420
    reloaded=time.monotonic()
    while time.monotonic()<deadline:
        for frame in page.frames:
            if frame.get_by_role('combobox',name='Поиск по номенклатуре или артикулу',exact=True).count():return frame
        if time.monotonic()-reloaded>20 and page.get_by_role('heading',name='Oh no.',exact=True).count():
            page.reload(wait_until='domcontentloaded',timeout=60000)
            reloaded=time.monotonic()
            print('Waiting for cloud restart...',flush=True)
        page.wait_for_timeout(500)
    raise AssertionError('Published search did not appear')


def main():
    checks=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.goto(URL,wait_until='domcontentloaded',timeout=60000)
        frame=app_frame(page)
        expect(frame.locator('[data-testid="stApp"]')).to_have_attribute('data-test-script-state','notRunning',timeout=240000)
        if frame.locator('[data-testid="stException"]').count():
            page.screenshot(path=str(OUT/'live_search_failure.png'))
            raise AssertionError(frame.locator('[data-testid="stException"]').all_text_contents())
        catalog=next(text for text in frame.locator('[data-testid="stCaptionContainer"]').all_text_contents() if 'В базе поиска:' in text)
        checks.append({'test':'full_catalog_loaded','caption':catalog})
        print('Verified full catalog:',catalog,flush=True)
        search=frame.get_by_role('combobox',name='Поиск по номенклатуре или артикулу',exact=True)
        search.fill('И27')
        expect(frame.get_by_role('option').first).to_be_visible(timeout=15000)
        assert any('И27' in text for text in frame.get_by_role('option').all_text_contents())
        search.press('Escape');search.fill('');search.press('Escape')
        frame.locator('.st-key-catalog_search_selection').get_by_role('button',name='Clear all',exact=True).click()
        expect(frame.get_by_text('Выберите модели в поиске, вставьте список или задайте характеристики.',exact=True)).to_be_visible(timeout=240000)
        checks.append({'test':'autocomplete_and_clear'})
        print('Verified autocomplete and clear.',flush=True)
        paste=frame.get_by_role('textbox',name='Номенклатуры и артикулы',exact=True)
        paste.fill('LR18XBF08DPOY-E2\nИ09-NO-PNP-P(Л63)\nNO-SUCH-MODEL-999')
        paste.press('Tab')
        frame.get_by_role('button',name='Разобрать список',exact=True).click()
        expect(frame.get_by_role('combobox',name='Считать Δ относительно',exact=True)).to_have_count(2,timeout=240000)
        expect(frame.get_by_text('Не найдено в текущей базе: NO-SUCH-MODEL-999',exact=True)).to_be_visible()
        assert not frame.get_by_text('Настройки и загрузка сравнения',exact=True).count()
        assert not frame.get_by_text('Найти в сравнении',exact=True).count()
        assert not frame.locator('[data-testid="stException"]').count()
        checks.append({'test':'pasted_models_and_unknown','groups':2})
        own=frame.locator('[class*="st-key-group_model_"][class*="_ours_"] .comparison-price')
        expect(own.first).not_to_have_text('—',timeout=60000)
        checks.append({'test':'skb_prices_loaded','displayed_own_prices':own.all_text_contents()})
        print('Verified batch groups and SKB prices.',flush=True)
        frame.get_by_role('radiogroup',name='Вид цены',exact=True).get_by_text('Как в источнике',exact=True).click()
        expect(frame.get_by_role('combobox',name='Считать Δ относительно',exact=True)).to_have_count(2,timeout=60000)
        checks.append({'test':'price_basis_and_charts','charts':frame.locator('[data-testid="stVegaLiteChart"]').count()})
        page.screenshot(path=str(OUT/'live_search_desktop.png'))
        frame.get_by_role('tab',name='Подобрать по характеристикам',exact=True).click()
        frame.get_by_role('combobox',name='Диаметр корпуса, мм',exact=True).fill('18')
        frame.get_by_role('combobox',name='Диаметр корпуса, мм',exact=True).press('ArrowDown')
        frame.get_by_role('option',name='18',exact=True).click()
        frame.get_by_role('combobox',name='Выходной сигнал',exact=True).fill('PNP')
        frame.get_by_role('combobox',name='Выходной сигнал',exact=True).press('ArrowDown')
        frame.get_by_role('option',name='PNP',exact=True).click()
        expect(frame.get_by_text('Найдено моделей:',exact=False)).to_be_visible(timeout=60000)
        checks.append({'test':'characteristics'})
        page.set_viewport_size({'width':430,'height':900})
        search.scroll_into_view_if_needed()
        page.screenshot(path=str(OUT/'live_search_mobile.png'))
        assert not frame.evaluate('document.documentElement.scrollWidth > window.innerWidth + 1')
        checks.append({'test':'mobile_430px'})
        browser.close()
    (OUT/'live_verification.json').write_text(json.dumps({'url':URL,'checks':checks},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(checks,ensure_ascii=False))


if __name__=='__main__':main()
