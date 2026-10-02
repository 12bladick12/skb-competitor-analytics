"""Read-only UI recovery check after the cloud startup repair."""
from pathlib import Path
from datetime import datetime,timezone
import json
import time
from playwright.sync_api import sync_playwright

OUT=Path(__file__).resolve().parents[1]/'analysis/startup_fix_2026_10_02'
URL='https://skb-competitor-analytics.streamlit.app/?workspace=prices&price_section=compare'


def main():
    start=time.monotonic();last_reload=start;reported=False
    report={'url':URL,'verified':False}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000})
        page.goto(URL,wait_until='domcontentloaded',timeout=60000)
        while time.monotonic()-start<420:
            failed=False
            for frame in page.frames:
                if not frame.locator('[data-testid="stApp"]').count():continue
                alerts=frame.locator('[data-testid="stAlert"]').all_text_contents()
                errors=frame.locator('[data-testid="stException"]').all_text_contents()
                failed=failed or bool(errors) or any('Не удалось' in text or 'требует завершения' in text for text in alerts)
                if frame.get_by_role('heading',name='Сравнение цен',exact=True).count() and not reported:
                    print('Cloud services opened; comparison page is loading.',flush=True);reported=True
                    report['services_opened_seconds']=round(time.monotonic()-start,1)
                search=frame.get_by_role('combobox',name='Поиск по номенклатуре или артикулу',exact=True)
                state=frame.locator('[data-testid="stApp"]').get_attribute('data-test-script-state')
                if search.count() and state=='notRunning' and not failed:
                    report.update(verified=True,checked_at=datetime.now(timezone.utc).isoformat(),
                        seconds=round(time.monotonic()-start,1),
                        catalog=[text for text in frame.locator('[data-testid="stCaptionContainer"]').all_text_contents() if 'В базе поиска' in text],
                        exceptions=0)
                    search.scroll_into_view_if_needed()
                    page.screenshot(path=str(OUT/'live_recovered.png'))
                    break
            if report['verified']:break
            if time.monotonic()-last_reload>=30 and (failed or page.get_by_role('heading',name='Oh no.',exact=True).count()):
                page.reload(wait_until='domcontentloaded',timeout=60000)
                last_reload=time.monotonic()
                print('Waiting for the new cloud process...',flush=True)
            page.wait_for_timeout(1000)
        browser.close()
    (OUT/'live_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    if not report['verified']:raise RuntimeError('Cloud page has not recovered yet')


if __name__=='__main__':main()
