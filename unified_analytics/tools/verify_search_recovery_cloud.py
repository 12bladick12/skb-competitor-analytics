"""Observe the deployed search, then open one known model; no admin operations."""
from datetime import datetime,timezone
from pathlib import Path
import json
import time
from playwright.sync_api import sync_playwright

OUT=Path(__file__).resolve().parents[1]/'analysis'/'search_load_recovery_2026_10_02'
URL='https://skb-competitor-analytics.streamlit.app/?workspace=prices&price_section=compare'

def main():
    start=time.monotonic();last_log=0;reloaded=False;chosen=False
    report={'url':URL,'started_at':datetime.now(timezone.utc).isoformat(),
            'names_ready':False,'characteristics_ready':False,'comparison_ready':False}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1050})
        page.goto(URL,wait_until='domcontentloaded',timeout=60000)
        while time.monotonic()-start<480:
            try:
                frame=next((f for f in page.frames if f.locator('[data-testid="stApp"]').count()),None)
                if frame is None:
                    page.wait_for_timeout(1000);continue
                captions=frame.locator('[data-testid="stCaptionContainer"]').all_text_contents()
                alerts=frame.locator('[data-testid="stAlert"]').all_text_contents()
                exceptions=frame.locator('[data-testid="stException"]').all_text_contents()
                names=frame.get_by_role('combobox',name='Поиск по номенклатуре или артикулу',exact=True)
                ready=names.count() and names.first.is_enabled()
                complete=any('Каталог обновлён:' in c for c in captions)
                elapsed=round(time.monotonic()-start,1)
                if ready and not report['names_ready']:
                    report.update(names_ready=True,names_seconds=elapsed)
                    print(json.dumps({'names_ready_seconds':elapsed}),flush=True)
                if complete and not report['characteristics_ready']:
                    report.update(characteristics_ready=True,characteristics_seconds=elapsed)
                    print(json.dumps({'characteristics_ready_seconds':elapsed}),flush=True)
                if ready and complete and not chosen:
                    names.first.fill('LR18XBF08DPOY-E2',timeout=5000)
                    option=frame.get_by_role('option').filter(has_text='LR18XBF08DPOY-E2').first
                    option.click(timeout=15000);chosen=True
                if chosen and frame.get_by_role('combobox',name='Считать Δ относительно',exact=True).count():
                    chart=frame.locator('[data-testid="stVegaLiteChart"]')
                    if chart.count() and not exceptions and not any('Не удалось' in a for a in alerts):
                        report.update(comparison_ready=True,comparison_seconds=elapsed,
                            visible_models=frame.get_by_role('checkbox',name='На графике:',exact=False).count())
                        page.screenshot(path=str(OUT/'live_search_recovered.png'))
                        break
                if elapsed-last_log>=30:
                    report.update(last_captions=captions,last_alerts=alerts,exceptions=exceptions,elapsed_seconds=elapsed)
                    print(json.dumps({'seconds':elapsed,'captions':captions,'alerts':alerts},ensure_ascii=False),flush=True)
                    last_log=elapsed
                    (OUT/'live_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                # A stale process may be finishing an old request during deployment.
                if elapsed>90 and not reloaded and not names.count():
                    page.reload(wait_until='domcontentloaded',timeout=60000);reloaded=True
                page.wait_for_timeout(1000)
            except Exception as exc:
                report['browser_retry']=type(exc).__name__
                page.wait_for_timeout(1000)
        browser.close()
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    report['elapsed_seconds']=round(time.monotonic()-start,1)
    (OUT/'live_verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
