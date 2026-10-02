"""Run the same collector locally; no credentials or production DB writes."""
import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.intelligence import collect_intelligence
from web.facts import month


def report(result, path):
    from cloud.intelligence_screen import explain
    states={'confirmed':'Подтверждено','needs_review':'Требует проверки','outside_period':'Вне периода',
            'duplicate':'Перепечатка','success':'Проверен','partial':'Проверка неполная','error':'Источник недоступен'}
    parts = ['<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>Внешние упоминания и судебные дела</title><style>body{max-width:1100px;margin:30px auto;padding:20px;'
        'font:16px/1.55 Arial;color:#243448;background:#f7f8fa;overflow-wrap:anywhere}article{background:white;padding:20px;'
        'border:1px solid #dae0e8;border-radius:8px;margin:16px 0}table{border-collapse:collapse;width:100%}'
        'td,th{padding:10px;border-bottom:1px solid #dae0e8;text-align:left}td:first-child,th:first-child{white-space:nowrap}'
        '.scroll{overflow:auto}small{color:#536273}</style>',
        '<h1>Внешние упоминания и судебные дела</h1>',
        '<p>Период: '+escape(result.get('period','не указан'))+' · Проверено: '+escape(result.get('checked_at','не указано'))+'</p>',
        '<p>Это результат ограниченной проверки источников. Недоступность источника не означает отсутствия событий.</p>',
        '<p>Количество публикаций не равно количеству событий: разные материалы могут описывать одно мероприятие.</p>']
    if any(c['reason']=='search_api_not_configured' for c in result['checks']):
        parts.append('<p>Широкий поисковый API не подключён. Автоматический сбор ограничен заданными и ранее найденными ссылками.</p>')
    for record in result['records']:
        if record['kind'] != 'intel_mention':
            continue
        value = record['payload']
        if value['status']!='confirmed':
            continue
        parts.append('<article><small>' + escape(value['competitor_name']) + ' · '
            + escape(value.get('published_at') or 'Дата не подтверждена') + ' · '
            + escape(states.get(value['status'],value['status'])) + '</small><h2>' + escape(value['title']) + '</h2><p>'
            + escape(value['description']) + '</p><a href="' + escape(value['publisher_url'], quote=True)
            + '" rel="noopener noreferrer">Источник</a></article>')
    parts.append('<h2>Судебные дела</h2><p>В текущей проверке: '
                 + str(sum(r['kind']=='intel_case' for r in result['records']))
                 + ' сохранённых карточек. Текущее положение дел устанавливается только по подтверждённой стадии.</p>')
    parts.append('<h2>Полнота проверки</h2><div class="scroll"><table><tr><th>Конкурент</th><th>Источник</th><th>Статус</th><th>Причина</th></tr>')
    for c in result['checks']:
        cells=(c['competitor_name'],c['url'],states.get(c['status'],c['status']),explain(c['reason']))
        parts.append('<tr>' + ''.join('<td>'+escape(str(v))+'</td>' for v in cells) + '</tr>')
    model_label='Не подключена; использованы дословные выдержки' if result['model_status']=='not_configured' else result['model_status']
    parts.append('</table></div><p>Состояние модели: ' + escape(model_label) + '</p></html>')
    path.write_text(''.join(parts), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--period')
    parser.add_argument('--output', type=Path, default=ROOT/'data'/'intelligence')
    parser.add_argument('--config', type=Path, help='Необязательный локальный TOML с секцией intelligence')
    parser.add_argument('--seeds', type=Path, default=ROOT/'cloud'/'intelligence_sources.json')
    parser.add_argument('--case-import', type=Path, help='JSON-массив проверенных карточек; требуется повторное чтение официальной страницы')
    args = parser.parse_args()
    now = datetime.now(timezone.utc).date()
    period_key, interval = month(args.period or f'{now-timedelta(days=89)}__{now}')
    args.output.mkdir(parents=True, exist_ok=True)
    state_path = args.output/'state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    config = {}
    if args.config:
        import tomllib
        config = tomllib.loads(args.config.read_text(encoding='utf-8-sig')).get('intelligence', {})
    seeds = json.loads(args.seeds.read_text(encoding='utf-8'))['seeds']
    imports = json.loads(args.case_import.read_text(encoding='utf-8')) if args.case_import else []
    result = collect_intelligence(state,interval,args.output/'evidence',
        lambda stage,source='': print(stage,source,flush=True), config, seeds=seeds, case_imports=imports)
    for record in result['records']:
        state.setdefault(record['kind'], {})[record['key']] = record['payload']
    temporary = state_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(state_path)
    export = {k:v for k,v in result.items() if k!='objects'}
    export.update(period=period_key,checked_at=datetime.now(timezone.utc).isoformat())
    (args.output/'result.json').write_text(json.dumps(export,ensure_ascii=False,indent=2),encoding='utf-8')
    report(export,args.output/'report.html')
    print(json.dumps({'status':result['status'],'confirmed_events':len(result['events']),
                      'model_status':result['model_status'],'report':str(args.output/'report.html')},ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
