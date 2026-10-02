"""Browser checks against an already running local comparison preview."""
from pathlib import Path
import json

from playwright.sync_api import sync_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data' / 'comparison_preview'


def main():
    checks = []
    with sync_playwright() as browser_api:
        browser = browser_api.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
        page.goto('http://127.0.0.1:8528/', wait_until='domcontentloaded')
        search = page.get_by_role('combobox', name='Поиск по номенклатуре или артикулу', exact=True)
        expect(search).to_be_visible(timeout=60000)
        expect(page.locator('[data-testid="stApp"]')).to_have_attribute('data-test-script-state', 'notRunning', timeout=60000)
        search.fill('И27')
        expect(page.get_by_role('option').first).to_be_visible(timeout=15000)
        options = page.get_by_role('option').all_text_contents()
        assert any('И27' in label for label in options), options
        page.screenshot(path=str(OUTPUT / 'search-autocomplete-desktop.png'))
        checks.append({'test': 'live_autocomplete_partial_cyrillic', 'visible_suggestions': len(options)})
        search.press('Escape')
        search.fill('')
        panel = page.locator('.st-key-catalog_search_selection')
        search.press('Escape')
        panel.get_by_role('button', name='Clear all', exact=True).click()
        try:
            expect(page.get_by_text('Выберите модели в поиске, вставьте список или задайте характеристики.', exact=True)).to_be_visible(timeout=10000)
        except AssertionError:
            page.screenshot(path=str(OUTPUT / 'search-browser-failure.png'))
            print(page.locator('.st-key-catalog_search_panel').inner_text(), flush=True)
            raise
        paste = page.get_by_role('textbox', name='Номенклатуры и артикулы', exact=True)
        paste.fill('LR18XBF08DPOY-E2\nИ09-NO-PNP-P(Л63)\nNO-SUCH-MODEL-999')
        paste.press('Tab')
        page.get_by_role('button', name='Разобрать список', exact=True).click()
        expect(page.get_by_role('combobox', name='Считать Δ относительно', exact=True)).to_have_count(2, timeout=60000)
        expect(page.get_by_text('Не найдено в текущей базе: NO-SUCH-MODEL-999', exact=True)).to_be_visible()
        assert page.locator('[data-testid="stException"]').count() == 0
        checks.append({'test': 'paste_two_groups_and_unknown', 'groups': 2})
        assert page.get_by_text('Настройки и загрузка сравнения', exact=True).count() == 0
        assert page.get_by_text('Найти в сравнении', exact=True).count() == 0
        page.get_by_role('tab', name='Подобрать по характеристикам', exact=True).click()
        expect(page.get_by_role('combobox', name='Тип датчика', exact=True)).to_be_visible()
        page.get_by_role('combobox', name='Диаметр корпуса, мм', exact=True).click()
        page.get_by_role('option', name='18', exact=True).click()
        page.get_by_role('combobox', name='Выходной сигнал', exact=True).click()
        page.get_by_role('option', name='PNP', exact=True).click()
        expect(page.get_by_text('Найдено моделей:', exact=False)).to_be_visible(timeout=30000)
        checks.append({'test': 'characteristics_on_real_catalog'})
        page.locator('.st-key-catalog_search_panel').scroll_into_view_if_needed()
        page.screenshot(path=str(OUTPUT / 'search-characteristics-desktop.png'))
        page.set_viewport_size({'width': 430, 'height': 900})
        search.scroll_into_view_if_needed()
        expect(search).to_be_visible()
        page.screenshot(path=str(OUTPUT / 'search-mobile.png'))
        bounds = page.locator('.st-key-catalog_search_panel').bounding_box()
        assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 431, bounds
        overflow = page.evaluate('document.documentElement.scrollWidth > window.innerWidth')
        assert not overflow
        checks.append({'test': 'mobile_430px', 'horizontal_overflow': overflow})
        browser.close()
    (OUTPUT / 'search-browser-checks.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(checks, ensure_ascii=False))


if __name__ == '__main__':
    main()
